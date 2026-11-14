# Operations

This page covers daily use. [design.md](design.md) explains the behavior. [configuration.md](configuration.md)
lists the parameters.

## 1. Where to look

| Source | How to read it |
|---|---|
| Applet state and phase | `fleetmux applet list` on the machine. |
| One applet in detail | `fleetmux applet status applet-tunnel-1` |
| Phase and live log | The applet's tab in the host fleet window. The phase is at the top. The log tail is below it. |
| Log file | `<host run>/applets/<applet name>/applet.log`. `<host run>` is the directory of the host fleet's `config.json`. |

The `fleetmux applet` commands act on the latest fleet of the machine. Add `--config <host run>/config.json` to
pick another fleet.

## 2. See which route is in use, and why

1. Read the phase of `applet-tunnel-1`:

   ```
   up: via <target> <ms> (fail <f>/<n>, drops <d>) | switched <hh:mm:ss> from <target>: <reason> | standby: <target> <ms|down|unfit|connecting>, ...
   ```

   | Part | Meaning |
   |---|---|
   | `via <target> <ms>` | The route in use and the median of its recent good probe times. |
   | `fail <f>/<n>` | Failed probes and all probes in the score window. |
   | `drops <d>` | Drops in the score window. |
   | `switched ...` | The last switch between routes. `switched never` until the first switch. Picks at start are not switches. |
   | `standby: ...` | Every other route: its median, `down`, `unfit` (no score) or `connecting`. |

2. Read the measurement lines in the log. Each measurement writes one line:

   ```
   tunnel: measure <target> port <p> ok <k>/<n> median <ms> fail <f>/<t> drops <d> cost <ms|inf> <current|standby>[ last: <reason>]
   ```

   `cost` is the score. `inf` means the route has no score.

3. Read the switch lines in the log. Each switch writes one line:

   ```
   tunnel: switch <from> -> <to>: <reason>
   ```

   | Reason | Meaning |
   |---|---|
   | `startup` | The first route that came up at start. |
   | `startup best (cost <new> < <old>)` | The best route after every route of the start was measured. |
   | `better (cost <new> < <old> for <s> s)` | A route stayed better by the margin for `field.switch_hold_s`. |
   | `failover (<target> dropped: <why>)` | The route in use dropped. |
   | `failover (<n> failed probes on <target>: <why>)` | The route in use failed `field.failover_after_fails` probes in a row. |
   | `recovered` | The first route that came up after a time without a route. |
   | `recovered best (cost <new> < <old>)` | The best route after every route of that round was measured. |

4. Read the home proxy log on the home machine. Each tunnel writes `homeproxy: CONNECT <host>:<port>`.

## 3. Common errors

The field applet writes the reason of an exit as its last log line. The fleet shows that line as the reason of
state `config_error`.

| Log line | Cause | Action |
|---|---|---|
| `config error: machine is not set: ...` | `field.machine` is missing. | Set `field.machine` in `claude_route.yaml`. |
| `config error: no machine <name> in the machines block of <file> (machines: ...)` | `field.machine` is not an inventory name. | Use a name from `fleetmux superpowers show --machines`. |
| `config error: machines.<name>.reach in <file> lists no ssh destination` | The home machine has no reach entry. | Run `fleetmux superpowers add <name> --ssh <target> --via <kind>`. |
| `config error: cannot read the fleet-superpowers inventory <file>: ...` | The inventory file is missing or broken. | Run `fleetmux superpowers check`. Fix the file. |
| `config error: unknown key <key> (allowed: ...)` | A key in the block is misspelled or not supported. | Fix the key. See [configuration.md](configuration.md). |
| `config error: <key> must be ...` | A value is outside its range. | Fix the value. |
| `config error: local_port <p> is reserved on the field machine; avoid ...` | `field.local_port` is a refused port. | Pick another port (section 5). |
| `config error: route_ports <a>-<b> holds the reserved port(s) ...` | The range contains a refused port. | Move the range. |
| `config error: route_ports <a>-<b> has <n> port(s) for the <m> routes of machine <name>; widen route_ports` | The inventory lists more routes than the range holds. | Widen `field.route_ports`. |
| `tunnel: 127.0.0.1:<p> is already in use by another program; set field.local_port to a free port` | Another program listens on `field.local_port`. | Pick another port (section 5). |
| `tunnel: 127.0.0.1:<p> (route port of <target>) is already in use by another program; set field.route_ports to a free range` | Another program listens on a route port. | Move `field.route_ports`. |
| `tunnel: no ssh target of machine <name> connects in <n> rounds in a row (last round, tried in order: ...)` | No route reached the home proxy. | Read the reason of each target (below). Fix a route. Run `fleetmux applet start applet-tunnel-1`. |
| `tunnel: <target> failed: ssh exited with 255` | ssh could not connect or authenticate. | Run `ssh -o BatchMode=yes <target> true` and fix the ssh configuration. |
| `tunnel: <target> failed: no healthy forward within <s> s (...)` | ssh connected, but the home proxy did not answer. | Check that `applet-homeproxy-1` is `up` on the home machine and that `field.remote_port` equals `home.port`. |
| `... wrong service ...` | Another program answers on the home port. | Free `home.port` on the home machine, or change it on both sides. |
| `tunnel: route <target> skipped: no free port in route_ports` | The inventory lists more routes than the range holds. Extra routes are not used. | Widen `field.route_ports`. |
| `config error: bind: the proxy always binds 127.0.0.1; ...` | The `home` block has a listen address key. | Remove the key. |
| `homeproxy: refused CONNECT <host>:<port> (port not allowed)` | A client asked for a port outside `home.allow_ports`. | Add the port to `home.allow_ports` if it is needed. |
| `homeproxy: CONNECT <host>:<port> failed: ...` | The home machine cannot reach that host. | Check the home machine's network. |
| `homeproxy: self check failed (<n>): ...` | The proxy did not answer its own health request. After three failures it exits with code 1 and the fleet restarts it. | Check for another program on `home.port`. |

After the field applet exits with code 78, the fleet does not restart it. Start it again after the fix:

```sh
fleetmux applet start applet-tunnel-1
```

## 4. Apply a parameter change

The applets read `claude_route.yaml` once, at start. `superpowers load` checks the applet files against
`source.ref`.

To try a change on one machine:

1. Edit `claude_route.yaml`.
2. Restart the applet:

   ```sh
   fleetmux applet restart applet-tunnel-1
   ```

   On the home machine, restart `applet-homeproxy-1` instead.

To keep a change:

1. On the home machine, commit the change in the clone.
2. On the home machine, set `source.ref` in `workflow.yaml` to that commit. Commit `workflow.yaml`.
3. On the field machine, copy both commits:

   ```sh
   git -C ~/claude-route pull --ff-only home-lan:claude-route
   ```

4. On the field machine, save the entry again on both machines:

   ```sh
   cd ~/claude-route
   fleetmux superpowers save workflow.yaml --also home-machine
   ```

5. Unload and load the workflow again, in the order of the README.

A change to `teams/field-applet.yaml` or `teams/home.yaml` takes effect only through unload and load. `load`
copies the row into the host fleet's roster.

## 5. Change the field port

1. Pick a free port that is not a refused port and not inside `field.route_ports`.
2. Set `field.local_port` in `claude_route.yaml` to that port.
3. Set the same port in `session_env.port` and in both proxy URLs in `teams/field-applet.yaml`.
4. Apply the change through unload and load (section 4, "To keep a change").

## 6. Confirm that unload left nothing

Run these checks after both unload steps.

1. On each machine, the applet is not listed:

   ```sh
   fleetmux applet list
   ```

2. On the home machine, nothing listens on the proxy port:

   ```sh
   lsof -nP -iTCP:17891 -sTCP:LISTEN
   ```

3. On the field machine, nothing listens on the local port or the route ports:

   ```sh
   lsof -nP -iTCP:17892 -sTCP:LISTEN
   lsof -nP -iTCP:17900-17915 -sTCP:LISTEN
   ```

4. On each machine, no process of this project runs:

   ```sh
   pgrep -fl claude_route
   pgrep -fl 'ssh -N -T .*-L 127.0.0.1:'
   ```

Every command in steps 2 to 4 prints nothing. Use your own port values if you changed them.

When `unload` exits 1, it names the applet that still runs. Run the same `unload` command again. It waits again.

## 7. Run the home proxy without a fleet

Use this to check the home machine on its own.

1. Start the proxy from the repository root. Use the fleetmux release Python:

   ```sh
   <release python> -m claude_route.home --params claude_route.yaml &
   ```

2. Check its identity. The answer has `"service": "claude-route-homeproxy"`:

   ```sh
   curl -s http://127.0.0.1:17891/__claude_route/health
   ```

3. Check the exit address. The answer is the public address of the home network:

   ```sh
   curl -s -x http://127.0.0.1:17891 https://ipinfo.io/ip
   ```

4. Stop it:

   ```sh
   kill -TERM %1
   ```

5. Check that nothing listens:

   ```sh
   lsof -nP -iTCP:17891 -sTCP:LISTEN
   ```

## 8. Run the tests

1. Run the tests from the repository root:

   ```sh
   python -m pytest -q tests
   ```

2. Set `FLEETMUX_RELEASE=<release dir>` to pick the fleetmux SDK. The default is `~/fleetmux-current`.
3. The SDK tests need `redis-server`. They are skipped without it.
