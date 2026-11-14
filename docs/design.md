# Design

This page describes how claude-route works. [configuration.md](configuration.md) lists the parameters.
[operations.md](operations.md) covers daily use.

## 1. Goals and non-goals

The premise: the field machine cannot reach the Claude API. Loading the workflow means that every Claude session
of the field fleet goes through the home machine.

Goals:

1. Send each Claude session that the field fleet starts through the home machine's network.
2. Use the ssh routes from the field machine to the home machine that the inventory already lists.
3. Pick the best working route, and move to another route when the route in use fails.
4. Hold new Claude sessions while no route works.
5. Leave nothing behind after unload.

Non-goals:

1. The applets do not test the direct route to the Claude API.
2. The applets never fall back to a direct connection.
3. The applets do not route programs that the fleet does not start.
4. The applets do not open a port to the outside on either machine.
5. The applets do not call an AI model and do not request a worker.

## 2. Components

| Part | Machine | Module | Role |
|---|---|---|---|
| `applet-homeproxy-1` | home | `claude_route.home` | HTTP CONNECT proxy on `127.0.0.1:<home.port>`. |
| `applet-tunnel-1` | field | `claude_route.field` | One ssh forward per route, and route choice. |
| Loopback relay | field | `claude_route.relay` | Listens on `127.0.0.1:<field.local_port>` inside `applet-tunnel-1`. Hands each new connection to a route. |
| `session_env` row | field | `teams/field-applet.yaml` | Makes the field fleet start Claude sessions with the proxy variables. |

### 2.1 Home proxy

1. The proxy binds `127.0.0.1` only. No parameter sets another address.
2. It accepts `CONNECT host:port` when `port` is in `home.allow_ports`. Other ports get `403`.
3. It opens the outbound TCP connection from the home machine. The far side sees the home network's address.
4. It refuses every other method with `405`. It does not forward plain HTTP.
5. It answers `GET /__claude_route/health` itself (see 5.3).
6. It logs one line per tunnel: `homeproxy: CONNECT host:port`. It never logs payload.

### 2.2 Field tunnel and relay

1. The applet reads the ssh targets of the home machine from the fleet-superpowers inventory:
   `machines.<field.machine>.reach[].ssh`, in list order.
2. The inventory file is `$FLEETMUX_SUPERPOWERS` when set, else
   `~/.fleetmux/fleet-superpowers/fleet-superpowers.yaml`. The applet reads it. It never writes it.
3. Each target is a route. Each route gets its own ssh child:

   ```
   ssh -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
     -o ServerAliveCountMax=3 -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none \
     [-o <field.ssh_options> ...] -L 127.0.0.1:<route port>:127.0.0.1:<field.remote_port> <target>
   ```

4. Route ports come from the range `field.route_ports`. Route `i` of the list gets the next free port. A route
   keeps its port while it stays in the inventory.
5. The relay listens on `127.0.0.1:<field.local_port>`. For each new connection it takes the route in use. When
   that route port refuses, it tries the next best route once.
6. Keys, jump hosts and host names stay in the user's ssh configuration.

### 2.3 How sessions attach

1. `teams/field-applet.yaml` declares `session_env` on the field applet row:

   ```yaml
   session_env:
     port: 17892
     vars:
       HTTPS_PROXY: "http://127.0.0.1:17892"
       HTTP_PROXY: "http://127.0.0.1:17892"
       NO_PROXY: "localhost,127.0.0.1,::1"
     clis: [claude]
   ```

2. The fleet removes inherited proxy variables, then sets these variables on each Claude session it starts.
3. The fleet writes the variables into the `env` of the `--settings` file it passes to Claude. An env set
   through `--settings` overrides the env in the user's `~/.claude/settings.json`, without
   `--setting-sources`. So the injected proxy variables take effect. This project edits no settings file and
   adds no launch shim.
4. The fleet starts a Claude session only while `127.0.0.1:<port>` accepts connections. While the port is closed,
   the fleet holds the session at launch. It starts the session when the port opens.
5. `session_env.port` must equal `field.local_port`.

## 3. Route choice on the field machine

### 3.1 Measurement

1. A probe is one `GET /__claude_route/health` through one route's own port.
2. A probe passes when the home proxy answers `200` with its service name within `field.probe_timeout_s`.
3. The probe time is the time from request to answer.
4. The route in use gets one probe every `field.check_interval_s`.
5. Every other route that is up gets `field.samples` probes, `field.sample_gap_s` apart, every
   `field.measure_interval_s`.
6. At each `field.measure_interval_s` the applet also reads the inventory again:
   - A new target gets a route port and starts.
   - A removed target stops.
   - A route that is down starts again.
7. A relayed connection that a route port refuses counts as a failed probe of that route.

### 3.2 Score

Lower is better. The score of a route uses the probes of the last `field.window_s` seconds:

```
score = median(last field.samples good probe times, ms)
      + field.fail_penalty_ms * (failed probes / all probes)
      + field.drop_penalty_ms * (drops)
```

- A route without a good probe in the window has no score. It is not used.
- Scores are compared in whole ms. Equal scores go to the route listed first.
- A slow relayed route with frequent drops scores worse than a direct fast route. It is not picked.

### 3.3 Start

1. The applet starts every route at once, in list order.
2. A route is up when a probe passes within `field.attempt_timeout_s` of its ssh start.
3. The first route that comes up is used at once. The local port opens.
4. Each route that comes up gets `field.samples` probes.
5. When every route is measured or has failed, the applet takes the best route if it beats the route in use by
   the margin (3.4). Hold and cooldown do not apply to this pick.

### 3.4 Switch to a better route

The applet takes a better route only when all of these hold:

1. Its score is at most `(1 - field.switch_margin_ratio)` times the score in use.
2. Its score is at least `field.switch_margin_ms` lower.
3. Rules 1 and 2 hold for `field.switch_hold_s` without a break.
4. The last switch between routes was at least `field.switch_cooldown_s` ago.
5. Its last probe passed.

### 3.5 Failover

1. A drop of the route in use moves new connections at once to the best other route.
2. `field.failover_after_fails` failed probes in a row on the route in use do the same.
3. Hold and cooldown do not apply. A failover starts a cooldown.
4. A drop is an ssh exit after the route was up, or failing probes for `field.unhealthy_restart_s`.
5. A few failed probes are not a drop. They count in the score and towards `field.failover_after_fails`. The
   port stays open.

### 3.6 Existing connections

1. A switch moves new connections only.
2. A connection stays on the route it started on until it ends.
3. When that route's ssh exits, the connection closes.

### 3.7 No route

1. The route in use drops and no other route has a score. The local port closes.
2. The fleet holds new Claude sessions at launch.
3. After `field.restart_delay_s` a round starts every route again.
4. A round fails when every route of the round is down. The applet waits `field.restart_delay_s` and starts the
   next round.
5. A route that comes up reopens the port and resets the round count.
6. When every route of that round is measured, the applet takes the best route if it beats the one in use by
   the margin. Hold and cooldown do not apply.
7. After `field.max_failed_rounds` failed rounds in a row, the applet stops every ssh child, confirms that the
   port is closed and exits with code 78.
8. Its last stderr line names every target of the last round and why each failed. The fleet shows this line as
   the reason. The fleet does not restart the applet.

### 3.8 Exit codes

| Applet | Code | When | Fleet action |
|---|---|---|---|
| both | 0 | SIGTERM, after a clean stop. | None. |
| both | 78 | A parameter is invalid or missing. | State `config_error`. Not restarted. |
| `applet-homeproxy-1` | 1 | Three self checks in a row failed. | Restarted. |
| `applet-tunnel-1` | 78 | The inventory cannot be read, or has no such machine, or no reach entry. | State `config_error`. Not restarted. |
| `applet-tunnel-1` | 78 | `field.local_port` or a route port is in use by another program. | State `config_error`. Not restarted. |
| `applet-tunnel-1` | 78 | `field.max_failed_rounds` rounds in a row without a route. | State `config_error`. Not restarted. |

## 4. Reconnection after sleep or network loss

1. During sleep or network loss, the ssh children lose their connection.
2. ssh detects a dead connection by `ServerAliveInterval` and `ServerAliveCountMax`, and exits. That exit is a
   drop.
3. Failing probes for `field.unhealthy_restart_s` are also a drop. The applet stops that ssh child.
4. When another route still has a score, new connections move to it (3.5).
5. When no route is left, the applet runs rounds (3.7). The field port is closed during the rounds.
6. A round starts every route. The first route that comes up reopens the port.
7. With the defaults, the applet tries 10 rounds. Each round waits 5 s, then tries for at most 30 s.
8. A Claude turn that runs while the port closes ends with an error. The fleet holds that session and delivers
   the turn again after the port opens.

## 5. Security boundary

### 5.1 Listeners

1. Every listener binds `127.0.0.1`. The home proxy and the relay check the bound address after bind.
   Each ssh forward names `127.0.0.1` as its listen address.
2. The home proxy refuses the keys `bind`, `host`, `listen` and `address` with exit code 78.
3. No port is opened to the network on either machine.

### 5.2 Home proxy limits

| Check | Rule |
|---|---|
| Method | `CONNECT` only, plus the health request. Other methods get `405`. |
| Target port | Must be in `home.allow_ports` (default `[443]`). Other ports get `403`. |
| Target host | Any host name or address. The proxy does not filter hosts. |
| Request head | At most 16384 bytes, else `431`. Sent within 10 s. |
| Outbound connect | Within `home.connect_timeout_s`, else `502`. |
| Idle tunnel | Closed after `home.idle_timeout_s` without traffic. |

### 5.3 Health endpoint

1. `GET /__claude_route/health` on the proxy port answers `200` with JSON:
   `service`, `version`, `pid`, `port`, `started_at`, `tunnels_served`.
2. `service` is `claude-route-homeproxy`.
3. A health check passes only when `service` matches. A port that only accepts connections does not pass.
4. The home applet runs this check against itself every `home.check_interval_s`.
5. The field applet runs this check through each route as its probe.

### 5.4 Files and system state

1. The applets write nothing outside the fleet.
2. They edit no shell profile, CLI settings file, system proxy setting or state file of another program.
3. They create no launchd job, systemd unit or crontab entry.
4. Both rows set `durable_state: false`. The applets write nothing to their state directory.
5. Every process the applets start is a child of the applet. A stop ends all of them.

## 6. Processes and fleetmux

### 6.1 Applet rows

| Row | File | `command` | `params` | `stop_grace_s` | `lease` |
|---|---|---|---|---|---|
| `applet-homeproxy-1` | `teams/home.yaml` | `["{python}", "-m", "claude_route.home"]` | `{file: ../claude_route.yaml, block: home}` | 10 | `{max_active: 1, hot_add_max: 0}` |
| `applet-tunnel-1` | `teams/field-applet.yaml` | `["{python}", "-m", "claude_route.field"]` | `{file: ../claude_route.yaml, block: field}` | 15 | `{max_active: 1, hot_add_max: 0}` |

- `superpowers load` adds the row to the roster of the host fleet's run. `superpowers unload` restores that roster.
- `hot_add_max: 0` means the host never adds a worker for these applets. The applets never request one.
- The host fleet therefore needs only a plain supervisor. fleetmux requires at least one worker row in a roster.
  `examples/host-fleet.yaml` has one. The applets do not use it.

### 6.2 Phase

Each applet publishes a phase. The fleet shows it at the top of the applet's tab and in `fleetmux applet list`.

| Applet | Phase | Meaning |
|---|---|---|
| `applet-homeproxy-1` | `serving 127.0.0.1:<port>, <n> tunnels open` | Healthy. |
| `applet-homeproxy-1` | `self check failed (<n>): <reason>` | The last self checks failed. |
| `applet-tunnel-1` | `up: via <target> <ms> (fail <f>/<n>, drops <d>) \| switched <hh:mm:ss> from <target>: <reason> \| standby: <target> <ms\|down\|unfit\|connecting>, ...` | A route is in use. `switched never` until the first switch. |
| `applet-tunnel-1` | `connecting: round <n>/<max>, trying <target> (target i/n), ...` | First start, no route up yet. |
| `applet-tunnel-1` | `reconnecting: round <n>/<max>, trying <target> (target i/n), ...` | No route; a round runs. |
| `applet-tunnel-1` | `reconnecting: round <n>/<max> in <s> s; <note>` | No route; waiting for the next round. |
| `applet-tunnel-1` | `no route: <reason>` | The applet is exiting with code 78. |

### 6.3 What load and unload leave

| Item | After load | After unload |
|---|---|---|
| Applet rows | In the host fleet's run roster. | Removed. The roster equals its state before load. |
| Applet processes and ssh children | Running. | Ended. |
| Listeners `home.port`, `field.local_port`, route ports | Open. | Closed. |
| `<host run>/applets/<name>/applet.log` | Written. | Kept with the run. Archived with the run. |
| Inventory entries | Unchanged by the applets. | Unchanged. |
| Files outside the fleet | None. | None. |

## 7. Known limitations

1. Only Claude sessions that the field fleet starts use the route. Other programs on the field machine do not.
2. The home proxy accepts any target host on an allowed port. Any local process on the home machine can use it.
3. Any local process on the field machine can connect to `field.local_port`.
4. A probe measures the path to the home proxy. It does not measure the path from the home machine to the API.
5. A switch does not move existing connections. A long connection on a degraded route stays there until it ends.
6. The number of routes is limited by the size of `field.route_ports`. Extra targets are skipped and logged once.
7. `field.local_port` and `session_env.port` are set in two files. They must be changed together.
8. After exit code 78, the field applet stays stopped until `fleetmux applet start applet-tunnel-1`.
9. The list of refused ports on the field machine is fixed in the code.
