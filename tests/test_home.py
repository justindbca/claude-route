import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_route import SERVICE, ConfigError
from claude_route import home
from claude_route.home import HomeProxy, health_ok

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def refused(port):
    try:
        socket.create_connection(('127.0.0.1', port), timeout=0.5).close()
        return False
    except OSError:
        return True


def echo_server():
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen()

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    conn.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    return srv


def raw(port, data, read=4096):
    with socket.create_connection(('127.0.0.1', port), timeout=3) as s:
        s.sendall(data)
        return s.recv(read)


@pytest.fixture
def proxy():
    echo = echo_server()
    p = HomeProxy(port=0, allow_ports=[echo.getsockname()[1]])
    p.start()
    yield p, echo.getsockname()[1]
    p.close()
    echo.close()


# ---------------------------------------------------------------------- configuration
@pytest.mark.parametrize('key', ['bind', 'host', 'listen', 'address'])
def test_no_bind_address_can_be_configured(key):
    with pytest.raises(ConfigError, match='127.0.0.1'):
        HomeProxy.from_block({key: '0.0.0.0'})


def test_unknown_key_and_bad_values_are_refused():
    with pytest.raises(ConfigError, match='unknown key'):
        HomeProxy.from_block({'prot': 1})
    with pytest.raises(ConfigError, match='port'):
        HomeProxy.from_block({'port': 80})
    with pytest.raises(ConfigError, match='allow_ports'):
        HomeProxy.from_block({'allow_ports': []})


def test_project_block_loads():
    from claude_route import read_block
    p = HomeProxy.from_block(read_block(ROOT / 'claude_route.yaml', 'home'))
    assert (p.port, sorted(p.allow_ports)) == (17891, [443])


def test_server_binds_loopback_only(proxy):
    p, _ = proxy
    assert p.server.server_address[0] == '127.0.0.1'
    assert not hasattr(home.LoopbackServer.__init__, 'host')


# ---------------------------------------------------------------------- health
def test_health_answers_identity(proxy):
    p, _ = proxy
    ok, doc = health_ok(p.port)
    assert ok and doc['service'] == SERVICE and doc['pid'] == os.getpid() and doc['port'] == p.port


def test_port_that_only_answers_is_not_healthy():
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen()
    threading.Thread(target=lambda: srv.accept()[0].close(), daemon=True).start()
    ok, why = health_ok(srv.getsockname()[1], timeout=2)
    srv.close()
    assert not ok and 'no answer' in why


def test_other_service_is_not_healthy():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Other(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({'service': 'something-else'}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(('127.0.0.1', 0), Other)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ok, why = health_ok(srv.server_address[1])
    srv.shutdown()
    srv.server_close()
    assert not ok and 'wrong service' in why


def test_step_crashes_after_repeated_failed_self_checks(proxy):
    p, _ = proxy
    assert p.step() == p.check_interval_s and p.fails == 0
    p.port = free_port()  # pretend the listener vanished
    for _ in range(home.FAILS_BEFORE_CRASH - 1):
        p.step()
    with pytest.raises(RuntimeError):
        p.step()


# ---------------------------------------------------------------------- proxying
def test_connect_tunnels_bytes(proxy):
    p, echo_port = proxy
    with socket.create_connection(('127.0.0.1', p.port), timeout=3) as s:
        s.sendall(b'CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: x\r\n\r\n' % echo_port)
        assert s.recv(4096).startswith(b'HTTP/1.1 200')
        s.sendall(b'ping')
        assert s.recv(4096) == b'ping'
    assert p.tunnels == 1


def test_connect_to_port_not_allowed_is_refused(proxy):
    p, _ = proxy
    assert raw(p.port, b'CONNECT example.com:22 HTTP/1.1\r\n\r\n').startswith(b'HTTP/1.1 403')


def test_connect_failure_is_502(proxy):
    p, _ = proxy
    p.allow_ports = frozenset([free_port()])
    port = next(iter(p.allow_ports))
    assert raw(p.port, b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % port).startswith(b'HTTP/1.1 502')


def test_plain_requests_are_refused(proxy):
    p, _ = proxy
    assert raw(p.port, b'GET http://example.com/ HTTP/1.1\r\n\r\n').startswith(b'HTTP/1.1 405')
    assert raw(p.port, b'garbage\r\n\r\n').startswith(b'HTTP/1.1 400')


def test_close_releases_listener_and_tunnels(proxy):
    p, echo_port = proxy
    s = socket.create_connection(('127.0.0.1', p.port), timeout=3)
    s.sendall(b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % echo_port)
    assert s.recv(4096).startswith(b'HTTP/1.1 200')
    port = p.port
    p.close()
    assert refused(port)
    assert s.recv(4096) == b''  # the open tunnel was shut
    s.close()


# ---------------------------------------------------------------------- process
def test_standalone_process_serves_and_exits_0_on_sigterm(tmp_path):
    port = free_port()
    params = tmp_path / 'p.yaml'
    params.write_text('home:\n  port: %d\n  check_interval_s: 0.2\n' % port)
    proc = subprocess.Popen([sys.executable, '-m', 'claude_route.home', '--params', str(params)], cwd=ROOT,
                            stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 10
        while not health_ok(port)[0]:
            assert time.time() < deadline and proc.poll() is None
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(10) == 0
        assert refused(port)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_standalone_bind_key_exits_78(tmp_path):
    params = tmp_path / 'p.yaml'
    params.write_text('home:\n  bind: 0.0.0.0\n')
    proc = subprocess.run([sys.executable, '-m', 'claude_route.home', '--params', str(params)], cwd=ROOT,
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 78 and 'bind' in proc.stderr


# ---------------------------------------------------------------------- workflows entry
MY_FILES = ('README.md', 'claude_route/__init__.py', 'claude_route/home.py', 'tests/test_home.py',
            'tests/test_home_applet.py', 'teams/home.yaml')
CJK_WORD = '\u914d\u65b9'


@pytest.fixture
def workflow_file(tmp_path):
    """The workflows entry WORKFLOW_FILE, copied into tmp_path. It is the one repository file whose only
    top-level key is the entry name."""
    import shutil
    import yaml
    from claude_route import WORKFLOW_FILE, WORKFLOW_NAME
    holders = [p for p in sorted(ROOT.glob('*.yaml')) if list(yaml.safe_load(p.read_text()) or {}) == [WORKFLOW_NAME]]
    assert holders == [ROOT / WORKFLOW_FILE], holders
    dst = tmp_path / WORKFLOW_FILE
    shutil.copyfile(holders[0], dst)
    return dst


def entry_of(path):
    import yaml
    from claude_route import WORKFLOW_NAME
    return yaml.safe_load(path.read_text())[WORKFLOW_NAME]


def fleetmux_superpowers():
    import importlib
    from conftest import RELEASE
    if not (RELEASE / 'fleetmux' / 'fleet_superpowers.py').exists():
        pytest.skip('needs a fleetmux release at %s' % RELEASE)
    sys.path.insert(0, str(RELEASE))
    try:
        return importlib.import_module('fleetmux.fleet_superpowers')
    finally:
        sys.path.remove(str(RELEASE))


APPLET_MODULES = {'home', 'field'}     # the only package modules the entry and the README may name


def package_modules(text):
    """Names X of every ``claude_route.X`` module or ``claude_route/X.py`` path mentioned in ``text``. Upper-case
    constants, the parameter file and the health URL path are not modules."""
    import re
    return set(re.findall(r'(?<!\w)claude_route[./]([a-z_]\w*)(?:\.py)?', text)) - {'yaml'}


def test_package_modules_finds_any_module_reference():
    assert package_modules('python -m claude_route.zzz; see claude_route/home.py') == {'zzz', 'home'}
    assert package_modules('claude_route.yaml, claude_route.WORKFLOW_FILE, GET /__claude_route/health') == set()


def test_workflow_entry_has_no_applicability_check(workflow_file):
    """Loading the workflow means: always go through home. There is no check of the direct route."""
    assert 'check' not in entry_of(workflow_file)
    assert package_modules(workflow_file.read_text()) <= APPLET_MODULES


def test_workflow_entry_passes_the_fleetmux_schema(workflow_file):
    """The entry validates, and it is of the kind fleetmux actually checks (a broken copy is refused)."""
    from claude_route import WORKFLOW_NAME
    fs = fleetmux_superpowers()
    entry = entry_of(workflow_file)
    assert fs.validate({'workflows': {WORKFLOW_NAME: entry}}) == []
    broken = dict(entry, summary='')
    assert fs.validate({'workflows': {WORKFLOW_NAME: broken}}) != []


OLD_KIND_WORD = 'r\x65cipe'   # the entry type fleetmux 9e950635 refuses; spelled so this file does not contain it


def test_workflow_entry_kind_is_workflow(workflow_file):
    """fleetmux 9e950635 loads only `kind: workflow` entries."""
    assert entry_of(workflow_file)['kind'] == 'workflow'
    assert workflow_file.read_text().count('kind: workflow') == 1


HOME = ('home-machine', 'home-fleet')         # example (machine, fleet) of the home machine in the template
FIELD = ('field-machine', 'field-fleet')      # example (machine, fleet) of the field machine in the template


def test_workflow_entry_names_the_example_machines_and_fleets(workflow_file):
    entry = entry_of(workflow_file)
    rows = {item['roster_rows']: (item['machine'], item['fleet']) for item in entry['applets']}
    assert rows == {'teams/home.yaml': HOME, 'teams/field-applet.yaml': FIELD}
    assert entry['source']['from'] == HOME[0]
    assert entry['saved_on'] == [HOME[0], FIELD[0]]


@pytest.mark.parametrize('rel, names', [
    ('workflow.yaml', HOME + FIELD),
    ('claude_route.yaml', HOME + FIELD),
    ('teams/home.yaml', HOME),
    ('teams/field-applet.yaml', FIELD),
])
def test_comments_use_the_example_names_not_placeholders(rel, names):
    comments = '\n'.join(line.split('#', 1)[1] for line in (ROOT / rel).read_text().splitlines() if '#' in line)
    for name in names:
        assert name in comments, (rel, name)
    for placeholder in ('<other machine>', '<field>', 'to be confirmed', 'home-lan'):
        assert placeholder not in comments, (rel, placeholder)


def test_old_kind_word_appears_nowhere(workflow_file):
    """The entry file and this project's own files say workflow, never the old entry type."""
    text = workflow_file.read_text()
    assert OLD_KIND_WORD not in text.lower() and CJK_WORD not in text
    for rel in MY_FILES:
        body = (ROOT / rel).read_text()
        assert OLD_KIND_WORD not in body.lower() and CJK_WORD not in body, rel


def test_readme_names_the_workflow_file_and_is_ascii():
    from claude_route import WORKFLOW_FILE
    text = (ROOT / 'README.md').read_text()
    assert text.isascii()
    assert '`%s`' % WORKFLOW_FILE in text
    assert package_modules(text) <= APPLET_MODULES and 'applies_when' not in text


def docs_text(*names):
    """README.md and the pages under docs/ (or only ``names``), joined."""
    paths = [ROOT / n for n in names] if names else [ROOT / 'README.md'] + sorted((ROOT / 'docs').glob('*.md'))
    return '\n'.join(p.read_text() for p in paths)


def config_rows(block):
    """{key: default cell} of the tables under ``## Block `<block>` `` in docs/configuration.md."""
    import re
    text = (ROOT / 'docs' / 'configuration.md').read_text()
    begin = text.index('\n## Block `%s`\n' % block)
    end = text.find('\n## ', begin + 1)
    section = text[begin:end if end != -1 else len(text)]
    return dict(re.findall(r'^\| `([a-z_]+)` \| ([^|]+?) \|', section, re.M))


def shown(value):
    """A code default as the configuration page writes it."""
    if value is None:
        return 'none (required)'
    if isinstance(value, list):
        return '`[%s]`' % ', '.join(str(v) for v in value)
    return '`%s`' % value


@pytest.mark.parametrize('block', ['home', 'field'])
def test_configuration_page_lists_every_key_with_the_code_default(block):
    from claude_route import field
    defaults = home.DEFAULTS if block == 'home' else field.DEFAULTS
    rows = config_rows(block)
    assert sorted(rows) == sorted(defaults), block
    for key, value in defaults.items():
        assert rows[key] == shown(value), (block, key, rows[key])


def design_sections(first, last):
    """docs/design.md from the heading ``### <first>`` up to the heading after ``### <last>``."""
    text = (ROOT / 'docs' / 'design.md').read_text()
    begin = text.index('\n### %s ' % first)
    after = text.index('\n### %s ' % last)
    end = min(i for i in (text.find('\n## ', after + 1), text.find('\n### ', after + 1)) if i != -1)
    return text[begin:end]


def test_design_field_sections_match_the_field_code():
    """docs/design.md sections 2.2-2.3 state the facts of claude_route.field at HEAD: the inventory source, the
    ssh argv, local_port and the session variables of teams/field-applet.yaml. The reserved ports and the keys
    are on the configuration page; exit 78 is in design.md section 3.8."""
    import yaml
    from claude_route import field
    section = design_sections('2.2', '2.3')
    assert 'not written yet' not in section
    assert 'field.local_port' in section and 'field.machine' in section
    assert 'reach[].ssh' in section and '$%s' % field.INVENTORY_ENV in section
    assert str(field.INVENTORY_DEFAULT) in section
    argv = field.Tunnel('m', ['t']).argv('t', 17900)
    for i, arg in enumerate(argv):
        if arg == '-o':
            assert '-o ' + argv[i + 1] in section, argv[i + 1]
    assert '-N -T' in section and '-L 127.0.0.1:<route port>:127.0.0.1:<field.remote_port>' in section
    row = yaml.safe_load((ROOT / 'teams' / 'field-applet.yaml').read_text())['applets'][0]
    shown_env = yaml.safe_load(section.split('```yaml', 1)[1].split('```', 1)[0])['session_env']
    assert shown_env == row['session_env']
    assert shown_env['port'] == field.DEFAULTS['local_port'] and '`%d`' % field.DEFAULTS['local_port'] in \
        docs_text('docs/configuration.md')
    assert 'localhost,127.0.0.1,::1' in section
    config = docs_text('docs/configuration.md')
    for port in field.RESERVED_PORTS:
        assert str(port) in config, port
    rows = config_rows('field')
    for key in ('machine', 'attempt_timeout_s', 'restart_delay_s', 'unhealthy_restart_s'):
        assert key in rows and key in field.DEFAULTS, key
    assert '78' in design_sections('3.8', '3.8')



def test_docs_name_no_machine_address_or_user_path():
    """Only the loopback address and the documentation range 192.0.2.0/24 appear."""
    import re
    text = docs_text()
    found = set(re.findall(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', text))
    assert all(a == '127.0.0.1' or a.startswith('192.0.2.') for a in found), found
    assert '/' + 'Users/' not in text and '/home/' not in text


def test_design_states_the_verified_settings_precedence():
    """Verified on claude 2.1.322: an env set through --settings overrides the env in ~/.claude/settings.json
    without --setting-sources. docs/design.md section 2.3 states that fact; the per-machine check before loading
    is gone from README and docs."""
    section = ' '.join(design_sections('2.2', '2.3').split())
    assert ('An env set through `--settings` overrides the env in the user\'s `~/.claude/settings.json`, '
            'without `--setting-sources`.') in section
    assert 'the injected proxy variables take effect' in section
    assert 'edits no settings file' in section
    text = ' '.join(docs_text().split())
    for gone in ('check settings precedence', 'Before the first load', 'N2 > N1'):  # design.md keeps 'Known limitations'
        assert gone not in text, gone



def readme_section(title):
    text = (ROOT / 'README.md').read_text()
    begin = text.index('\n## %s\n' % title)
    end = text.find('\n## ', begin + 1)
    return text[begin:end if end != -1 else len(text)]


def test_readme_load_and_unload_name_the_workflow_positionally_per_machine():
    """fleetmux 9e950635: `load` / `unload` take the workflow as a positional argument and `--machine`;
    `--workflow` exists only on `show`. Load home first, unload field first."""
    import re
    section = readme_section('Steps')
    cmds = re.findall(r'(fleetmux superpowers (?:load|unload) [^`\n]*?)\s*(?:`|$)', section, re.M)
    assert cmds == ['fleetmux superpowers load claude-via-home --machine home-machine',
                    'fleetmux superpowers load claude-via-home --machine field-machine',
                    'fleetmux superpowers unload claude-via-home --machine field-machine',
                    'fleetmux superpowers unload claude-via-home --machine home-machine']
    assert '--workflow' not in section
