"""examples/host-fleet.yaml: the smallest roster fleetmux accepts for a fleet that hosts the applets."""
import sys
from pathlib import Path

import pytest
import yaml

from conftest import RELEASE, ROOT

EXAMPLE = ROOT / 'examples' / 'host-fleet.yaml'


def test_example_has_one_plain_worker_and_no_applet_rows():
    data = yaml.safe_load(EXAMPLE.read_text())
    assert data == {'workers': [{'name': 'worker-1'}]}


def test_fleetmux_accepts_the_example_and_refuses_an_empty_worker_list():
    if not (RELEASE / 'fleetmux' / 'roster.py').exists():
        pytest.skip('needs a fleetmux release at %s' % RELEASE)
    sys.path.insert(0, str(RELEASE))
    try:
        from fleetmux import roster
    finally:
        sys.path.remove(str(RELEASE))
    assert roster.parse_entries(EXAMPLE.read_text(), EXAMPLE.parent)[0] == ['worker-1']
    with pytest.raises(ValueError, match='at least one worker'):
        roster.parse_entries('workers: []\n', EXAMPLE.parent)
