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

## 2a. See it reconnect

When no route is up, the field port is closed and the applet runs rounds until a route comes up. With the
default `field.max_failed_rounds: 0` it never stops for failed rounds. Its state stays running; it does not go to
`config_error`.

1. Read the phase of `applet-tunnel-1`. While it waits for the next round:

   ```
   reconnecting: round <n> in <s> s; round <n-1>: no target connects (<target>: <why>; ...)
   ```

   While a round runs:

   ```
   reconnecting: round <n>, trying <target> (target i/n), ...; round <n-1>: no target connects (...)
   ```

   | Part | Meaning |
   |---|---|
   | `round <n>` | The number of the next or running round since the last route was up. `round <n>/<max>` only when `field.max_failed_rounds` is above 0. |
   | `in <s> s` | Seconds to the next round. The wait doubles from `field.restart_delay_s` up to `field.restart_delay_max_s`. |
   | `round <n-1>: no target connects (...)` | The reason of each target of the last round, in the order in which the targets failed. |
   | `dropped (<target> dropped: <why>)` | Shown instead before the first round after the route in use dropped. |

2. Read the round lines in the log. When the last route drops, the applet writes:

   ```
   tunnel: no route left (<target> dropped: <why>); round 1 in <s> s
   ```

   Each failed round writes the lines of its attempts (`start`, `<target> failed: <why>`, `measure`) and one line:

   ```
   tunnel: round <n>: no target connects (tried in order: <target>: <why>; ...); round <n+1> in <s> s
   ```

   With a positive `field.max_failed_rounds`, the round numbers read `<n>/<max>`.

3. Repeated results are rate limited. While every round fails with the same reasons as the round before, the
   applet writes the lines of a round at most once per 600 s and leaves out the rounds in between. When the
   reasons change, the round line of that round is written at once, and the next round is written in full. The
   first round line after rounds that were left out ends with `; <m> rounds with the same result not logged`.
4. When a route comes up, the log shows the skipped count, if any, and the switch:

   ```
   tunnel: <m> rounds with the same result not logged (rounds <a>-<b>)
   tunnel: switch none -> <target>: recovered
   ```

   The port is open again. No manual start is needed.

5. Wake-up and network change. The applet does not wait for the round timer when the machine wakes or the
   network returns (design.md section 4). On a wake it writes:

   ```
   tunnel: woke up (clock jump <n> s); probing every route now
   ```

   With no route up, a new round starts at once and the phase reads `reconnecting: round 1, trying ...`. With
   routes up, every route is probed at once with the short time limit `field.wake_probe_timeout_s` (1 s by
   default). A route that does not answer within it is stopped and started again:

   ```
   tunnel: <target> dropped: no answer after wake-up (...)
   ```

   When the route in use fails this check and no other route answered, a round starts without a wait:
   `tunnel: no route left (<target> dropped: no answer after wake-up (...)); round 1 in 0.0 s`.

   While no route is up, the applet checks the network every `field.net_check_interval_s` s. It sends nothing.
   When a network appears where there was none, it writes:

   ```
   tunnel: network changed (none -> <address>[/<address>]); round now
   ```

   and a round starts at once. Other changes, such as a new address on a network that was already up, write no
   line and start no round; the rounds keep their wait.

6. What a lid-open looks like. After a night with the lid closed, the log shows short wakes: a `woke up` line, a
   few failed rounds, then nothing until the next wake. The round number stays low and the wait does not grow,
   because each wake starts over at round 1. With a positive `field.max_failed_rounds`, rounds that fail within
   60 s of a wake are not counted, so short wakes never end the applet. When the lid opens with a working network, the log shows `woke up`
   or `network changed`, then `switch none -> <target>: recovered`, within about 2 s plus the ssh handshake.
   If the phase still shows `reconnecting` long after the network is back, read the reasons in it.

## 3. Common errors

The field applet writes the reason of an exit as its last log line. The fleet shows that line as the reason of
state `config_error`. Exit code 78 comes from a configuration error. It comes from failed rounds only when
`field.max_failed_rounds` is above 0.

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
| `tunnel: round <n>: no target connects (...)` | No route came up in this round. The applet keeps trying (section 2a). | Nothing while the machine sleeps or has no network. If it goes on after the network is back, read the reason of each target (below) and fix a route. |
| `tunnel: no ssh target of machine <name> connects in <n> rounds in a row (last round, tried in order: ...)` | `field.max_failed_rounds` is above 0, and that many rounds in a row failed. | Set `field.max_failed_rounds` to `0` or remove it, so the applet keeps reconnecting. Fix a route. Run `fleetmux applet start applet-tunnel-1`. |
| `tunnel: ssh pid <pid> of <target> does not exit; port <p> stays with it` | An old ssh child of the applet did not end, even after SIGKILL. It keeps its route port. | Nothing. The applet waits for it and does not exit. If it stays, check the process with `ps -p <pid>`. A stop of the applet kills it again. |
| `tunnel: <target> failed: old ssh pid <pid> still holds port <p>` | A start of that route found its port held by that old child. It counts as one failed attempt. | Nothing. The route starts again after the old process ends. |
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

A parameter file from an older version may set `max_failed_rounds: 10` in the `field` block. With that line the
field applet still stops after 10 failed rounds. Set it to `0` or remove the line, then apply the change as
above.

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
