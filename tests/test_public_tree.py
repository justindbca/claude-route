"""The working tree is publishable: no file names a person, a real machine or fleet, a private address, a date
or non-ASCII CJK text.

Every file git manages (tracked, plus untracked files that are not ignored) is scanned; without a git checkout
every file under the repository is scanned. Each banned pattern below is assembled from pieces, so this file
does not match its own list.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache'}


def _w(*parts):
    return ''.join(parts)


NUM = r'\d{1,3}'
NO_DIGIT_BEFORE = r'(?<![\d.])'
NO_DIGIT_AFTER = r'(?![\d])'
# CJK punctuation, kana, ideographs, hangul, compatibility ideographs, full-width forms (code points).
CJK_RANGES = [(0x3000, 0x303f), (0x3040, 0x30ff), (0x3400, 0x4dbf), (0x4e00, 0x9fff), (0xac00, 0xd7af),
              (0xf900, 0xfaff), (0xff00, 0xffef)]

# (label, compiled pattern). Labels are written so they do not match either.
BANNED = [
    ('user name', re.compile(_w('jus', 'tin'), re.I)),
    ('home machine name', re.compile(_w('mac', r'[\s_-]?', 'mini'), re.I)),
    ('field machine name', re.compile(_w('lap', 'top'), re.I)),
    ('home fleet name', re.compile(_w('route', '-', 'dev'), re.I)),
    ('field fleet name', re.compile(_w('remote', '-', 'dev'), re.I)),
    ('macOS home path', re.compile(_w('/', 'Users', '/'))),
    ('Linux home path', re.compile(_w('/', 'home', '/', r'[A-Za-z0-9_.-]'))),
    ('CGNAT / Tailscale address', re.compile(_w(NO_DIGIT_BEFORE, '100', r'\.', r'(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])',
                                              r'\.', NUM, r'\.', NUM, NO_DIGIT_AFTER))),
    ('private address 192.168/16', re.compile(_w(NO_DIGIT_BEFORE, '192', r'\.', '168', r'\.'))),
    ('private address 10/8', re.compile(_w(NO_DIGIT_BEFORE, '10', r'\.', NUM, r'\.', NUM, r'\.', NUM,
                                           NO_DIGIT_AFTER))),
    ('private address 172.16/12', re.compile(_w(NO_DIGIT_BEFORE, '172', r'\.', r'(?:1[6-9]|2\d|3[01])', r'\.', NUM,
                                                r'\.', NUM, NO_DIGIT_AFTER))),
    ('CJK text', re.compile('[%s]' % ''.join('%s-%s' % (chr(a), chr(b)) for a, b in CJK_RANGES))),
    ('date', re.compile(_w(r'(?<!\d)', r'\d{4}', '-', r'\d{2}', '-', r'\d{2}', r'(?!\d)'))),
    # MM-DD with a two-digit month (1 to 12) and day (1 to 31), not part of a longer number, version or range (no digit,
    # '.' or '-' on either side) and not after "line(s) " or "range ".
    ('month-day date', re.compile(_w(r'(?<![\d.-])', r'(?<!lines )(?<!line )(?<!range )(?<!Range )',
                                     '(?:0[1-9]|1[0-2])', '-', r'(?:0[1-9]|[12]\d|3[01])', r'(?![\d.-])'))),
    ('numbered dev slot name', re.compile(_w(r'\b', 'dev', '-', r'\d'), re.I)),
    ('fleet or worker name ending in dev', re.compile(_w('-', 'dev', r'\b'), re.I)),
]

# The one allowed hit: line 3 of LICENSE, the copyright line, names the copyright holder exactly as the
# fleetmux repository's LICENSE does. Only that exact text on that line of that file is exempt.
LICENSE_EXEMPT = ('LICENSE', 3, _w('Copyright (c) 2026 ', 'Jus', 'tin', ' W'))


def managed_files():
    """Repository-relative paths of every file git manages here (tracked, and untracked but not ignored)."""
    done = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                          capture_output=True)
    if done.returncode == 0:
        names = sorted({n for n in done.stdout.decode().split('\0') if n})
        return [n for n in names if (ROOT / n).is_file()]
    out = []
    for path in sorted(ROOT.rglob('*')):
        rel = path.relative_to(ROOT)
        if path.is_file() and not SKIP_DIRS & set(rel.parts):
            out.append(rel.as_posix())
    return out


def hits(rel, text):
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        if (rel, no, line) == LICENSE_EXEMPT:
            continue
        for label, pattern in BANNED:
            for m in pattern.finditer(line):
                out.append('%s:%d: %s: %r' % (rel, no, label, m.group(0)))
    return out


def test_scan_covers_every_managed_file():
    files = managed_files()
    for rel in ('workflow.yaml', 'claude_route.yaml', 'claude_route/field.py', 'teams/home.yaml',
                'tests/test_public_tree.py', '.gitignore'):
        assert rel in files, rel
    assert len(files) >= 20, files


@pytest.mark.parametrize('sample, label', [
    (_w('Jus', 'tin'), 'user name'),
    (_w('mac', 'mini-vps'), 'home machine name'),
    (_w('Mac', ' mini'), 'home machine name'),
    (_w('lap', 'top'), 'field machine name'),
    (_w('route-', 'dev'), 'home fleet name'),
    (_w('remote-', 'dev'), 'field fleet name'),
    (_w('/Us', 'ers/x'), 'macOS home path'),
    (_w('/ho', 'me/x'), 'Linux home path'),
    (_w('100.', '100.1.2'), 'CGNAT / Tailscale address'),
    (_w('192.', '168.1.1'), 'private address 192.168/16'),
    (_w('10.', '0.0.1'), 'private address 10/8'),
    (_w('172.', '20.0.1'), 'private address 172.16/12'),
    (chr(0x4e2d), 'CJK text'),
    (_w('2026', '-01-02'), 'date'),
    (_w('11', '-14'), 'month-day date'),
    (_w('measured on 11', '-14; the'), 'month-day date'),
    (_w('"', '11', '-14: no LAN'), 'month-day date'),
    (_w('dev', '-1'), 'numbered dev slot name'),
    (_w('home-', 'dev'), 'fleet or worker name ending in dev'),
])
def test_each_pattern_catches_its_sample(sample, label):
    assert label in [h.split(': ')[1] for h in hits('x', 'a %s b' % sample)], sample


@pytest.mark.parametrize('sample', ['127.0.0.1', '192.0.2.9', '100.0.0.1', '17900-17915', 'route_ports 17900-17915',
                                    '1-10000', '1-3600', 'Range 1-10', 'lines 10-12', 'line 11-14', 'range 10-20',
                                    'python 3.10-12', 'ConnectTimeout=10', 'ServerAliveInterval=15', 'v2.1.322',
                                    'lines 115-116', '1024-65535', '0-12', '13-01', '12-32', 'home-machine',
                                    'field-fleet', '/dev/null', 'device-1', 'fa6358d8723968af89e4b34549aaf383b5c00961'])
def test_allowed_values_pass(sample):
    assert hits('x', sample) == []


def test_no_managed_file_names_a_person_machine_fleet_address_date_or_cjk():
    found = []
    for rel in managed_files():
        try:
            text = (ROOT / rel).read_text(encoding='utf-8')
        except UnicodeDecodeError:
            text = (ROOT / rel).read_bytes().decode('latin-1')
        found += hits(rel, text)
    assert found == [], '\n'.join(found)


def test_the_license_exemption_is_only_that_line_of_that_file():
    _, _, text = LICENSE_EXEMPT
    assert hits('LICENSE', 'MIT License\n\n%s\n' % text) == []
    assert hits('LICENSE', '%s\n' % text) != []                       # another line of LICENSE
    assert hits('LICENSE', 'x\n\n%s and more\n' % text) != []          # other text on line 3
    assert hits('README.md', 'x\n\n%s\n' % text) != []                 # another file
    assert hits('LICENSE', 'x\n\n%s\n%s\n' % (text, text)) != []       # the same text on line 4
