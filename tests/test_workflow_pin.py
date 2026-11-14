"""The workflows entry pins source.ref to a commit that `fleetmux superpowers load` (9e950635) accepts.

The rule: source.ref is an ancestor of HEAD, the applet files (the roster files the entry names and what their
`applets:` rows run) are the same at source.ref and at HEAD, and the working tree has no uncommitted change to
them. A later commit that changes only other files (README, tests, the entry file) needs no new pin.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from claude_route import WORKFLOW_FILE, WORKFLOW_NAME
from claude_route.field import RESERVED_PORTS

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.run(['git', '-C', str(ROOT)] + list(args), capture_output=True, text=True)


def entry_file():
    """WORKFLOW_FILE: the one repository file whose only top-level key is the entry name."""
    holders = [p for p in sorted(ROOT.glob('*.yaml')) if list(yaml.safe_load(p.read_text()) or {}) == [WORKFLOW_NAME]]
    assert holders == [ROOT / WORKFLOW_FILE], holders
    return holders[0]


def entry():
    return yaml.safe_load(entry_file().read_text())[WORKFLOW_NAME]


def applet_files():
    """Repository-relative applet files of every machine of the entry: each roster file (roster_rows) and, from
    its `applets:` rows, the top package of `-m pkg.mod` under cwd and params.file."""
    out = set()
    for item in entry()['applets']:
        roster = ROOT / item['roster_rows']
        out.add(roster)
        for row in (yaml.safe_load(roster.read_text()) or {}).get('applets', []):
            cwd = roster.parent / row.get('cwd', '.')
            command = row['command']
            for before, arg in zip([None] + command, command):
                if before == '-m':
                    out.add(cwd / arg.split('.')[0])
            if row.get('params', {}).get('file'):
                out.add(roster.parent / row['params']['file'])
    return sorted(str(p.resolve().relative_to(ROOT)) for p in out)


@pytest.fixture
def ref():
    if git('rev-parse', '--git-dir').returncode != 0:
        pytest.skip('not a git checkout')
    value = entry()['source']['ref']
    assert isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value), value
    return value


def test_workflow_file_is_workflow_yaml():
    assert WORKFLOW_FILE == 'workflow.yaml' and (ROOT / WORKFLOW_FILE).is_file()
    assert git('ls-files', '--error-unmatch', WORKFLOW_FILE).returncode == 0
    assert not (ROOT / ('r\x65cipe' + '.yaml')).exists()


def test_applet_files_are_the_package_the_params_and_the_rosters():
    assert applet_files() == ['claude_route', 'claude_route.yaml', 'teams/field-applet.yaml', 'teams/home.yaml']


def test_ref_is_a_full_sha_and_an_ancestor_of_head(ref):
    assert git('cat-file', '-e', ref + '^{commit}').returncode == 0, ref
    assert git('merge-base', '--is-ancestor', ref, 'HEAD').returncode == 0, ref


def test_no_applet_file_changed_since_the_pinned_commit(ref):
    diff = git('diff', '--name-only', ref, 'HEAD', '--', *applet_files())
    assert diff.returncode == 0 and diff.stdout.split() == [], diff.stdout + diff.stderr


def test_no_uncommitted_change_to_an_applet_file():
    if git('rev-parse', '--git-dir').returncode != 0:
        pytest.skip('not a git checkout')
    diff = git('diff', '--name-only', 'HEAD', '--', *applet_files())
    assert diff.returncode == 0 and diff.stdout.split() == [], diff.stdout + diff.stderr


def test_fleetmux_load_accepts_this_checkout_on_both_machines(ref):
    """The release's own source check (superpowers_peer.check_source) passes for every machine of the entry."""
    from conftest import RELEASE
    if not (RELEASE / 'fleetmux' / 'superpowers_peer.py').exists():
        pytest.skip('needs a fleetmux release at %s' % RELEASE)
    sys.path.insert(0, str(RELEASE))
    try:
        import importlib
        peer = importlib.import_module('fleetmux.superpowers_peer')
    finally:
        sys.path.remove(str(RELEASE))
    workflow = dict(entry(), source=dict(entry()['source'], repo=str(ROOT)))
    for item in workflow['applets']:
        assert peer.pinned_paths(workflow, item['machine'], ROOT), item['machine']
        peer.check_source(workflow, item['machine'])  # raises InventoryError when load would refuse


def test_field_block_at_the_pinned_commit_uses_the_inventory_and_a_free_port(ref):
    shown = git('show', '%s:claude_route.yaml' % ref)
    assert shown.returncode == 0, shown.stderr
    field = yaml.safe_load(shown.stdout)['field']
    assert 'machine' in field and 'dest' not in field, sorted(field)
    assert field['local_port'] not in RESERVED_PORTS, field['local_port']
