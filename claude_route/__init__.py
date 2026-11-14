"""claude-route: route the Claude sessions of a field fleet through the home machine's network.

Two fleetmux applets share this package:

- ``claude_route.home`` (applet-homeproxy-1): a minimal HTTP CONNECT proxy on 127.0.0.1 of the home machine.
- ``claude_route.field`` (applet-tunnel-1): one ``ssh -N -L`` child on the field machine that forwards a
  loopback port to that proxy.

Both run either under a fleet (``FLEETMUX_APPLET_*`` environment, fleetmux applet SDK) or standalone for a local
test (``python -m claude_route.home --params claude_route.yaml``). Standard library plus PyYAML only.
"""
import argparse
import os
import signal
import sys
import threading
from pathlib import Path

__version__ = '0.1.0'

SERVICE = 'claude-route-homeproxy'        # identity the health endpoint answers with
HEALTH_PATH = '/__claude_route/health'    # GET on the proxy port (not a proxied request)
LOOPBACK = '127.0.0.1'                    # the only address any listener of this package binds
SDK_KEYS = ('hot', 'writeback_s')         # keys the fleetmux SDK itself reads from a parameter block
EXIT_CONFIG = 78                          # applet-interface.md section 2.4: config error, not restarted
WORKFLOW_NAME = 'claude-via-home'         # name of the fleet-superpowers workflows entry
WORKFLOW_FILE = 'workflow.yaml'           # repository file that holds that entry (docs and tests use this name)


class ConfigError(ValueError):
    """A parameter block that cannot be used."""


def read_block(path, block):
    """The mapping ``block`` of the YAML file ``path`` (an empty block is ``{}``)."""
    import yaml
    try:
        whole = yaml.safe_load(Path(path).read_text())
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError('cannot read %s: %s' % (path, str(exc).splitlines()[0][:200])) from None
    if not isinstance(whole, dict) or block not in whole:
        raise ConfigError('%s has no block %s' % (path, block))
    data = whole[block] if whole[block] is not None else {}
    if not isinstance(data, dict):
        raise ConfigError('block %s in %s is not a mapping' % (block, path))
    return data


def settings(block, defaults, refuse=None):
    """``defaults`` updated from ``block``; unknown keys are refused. ``refuse`` maps a key to the reason it is
    never accepted (for example a bind address)."""
    out = dict(defaults)
    for key, value in block.items():
        if refuse and key in refuse:
            raise ConfigError('%s: %s' % (key, refuse[key]))
        if key in SDK_KEYS:
            continue
        if key not in defaults:
            raise ConfigError('unknown key %s (allowed: %s)' % (key, ', '.join(sorted(defaults))))
        out[key] = value
    return out


def check_port(name, value):
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ConfigError('%s must be a port number 1024-65535, got %r' % (name, value))
    return value


def check_seconds(name, value, low=0.1, high=86400):
    if type(value) not in (int, float) or not low <= value <= high:
        raise ConfigError('%s must be a number of seconds %s-%s, got %r' % (name, low, high, value))
    return float(value)


def log(text):
    print(text, file=sys.stderr, flush=True)


def main(service_cls, default_block, argv=None):
    """Run ``service_cls`` (``from_block(dict)``, ``start()``, ``step() -> seconds``, ``phase``, ``close()``)
    under the fleet when ``FLEETMUX_APPLET_NAME`` is set, otherwise standalone until SIGTERM or SIGINT."""
    if os.environ.get('FLEETMUX_APPLET_NAME'):
        return _main_applet(service_cls)
    parser = argparse.ArgumentParser(prog='python -m ' + service_cls.__module__)
    parser.add_argument('--params', help='parameter file (default: no file, built-in defaults)')
    parser.add_argument('--block', default=default_block, help='block of the parameter file')
    args = parser.parse_args(argv)
    try:
        svc = service_cls.from_block(read_block(args.params, args.block) if args.params else {})
    except ConfigError as exc:
        log('config error: %s' % exc)
        sys.exit(EXIT_CONFIG)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        svc.start()
        while not stop.is_set():
            stop.wait(svc.step())
    finally:
        svc.close()
    sys.exit(0)


def _main_applet(service_cls):
    from fleetmux.applet import Applet
    app = Applet.from_env()
    try:
        svc = service_cls.from_block(app.params().values)
    except ConfigError as exc:
        log('config error: %s' % exc)  # the host shows the last stderr line as the reason
        sys.exit(EXIT_CONFIG)
    shown = [None]

    def step(_app):
        delay = svc.step()
        if svc.phase != shown[0]:
            shown[0] = svc.phase
            app.set_phase(svc.phase)
        return delay

    try:
        svc.start()
        app.run(step)  # exits 0 after SIGTERM / host.draining; the finally below runs first
    finally:
        svc.close()
