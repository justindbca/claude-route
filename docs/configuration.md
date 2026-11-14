# Configuration

`claude_route.yaml` is the only parameter file. fleetmux passes the file and a block name to each applet:

| Block | Applet | Row |
|---|---|---|
| `home` | `applet-homeproxy-1` | `teams/home.yaml` |
| `field` | `applet-tunnel-1` | `teams/field-applet.yaml` |

Rules:

1. A key that is not listed here stops the applet with exit code 78.
2. A value outside its range stops the applet with exit code 78.
3. A key that is left out takes its default. `field.machine` has no default.
4. The keys `hot` and `writeback_s` belong to the fleetmux applet SDK. The applets ignore them.
5. The file in this repository sets every key to its default, except `field.machine`, which holds an example.

See [operations.md](operations.md) for how a change takes effect.

## Block `home`

| Key | Default | Meaning | When to change |
|---|---|---|---|
| `port` | `17891` | Loopback port of the CONNECT proxy. The bind address is always `127.0.0.1`. Range 1024-65535. | Another program on the home machine uses the port. Set `field.remote_port` to the same value. |
| `allow_ports` | `[443]` | Target ports that `CONNECT` may open. Other ports get `403`. A non-empty list of ports 1-65535. | A routed client needs another target port. |
| `connect_timeout_s` | `10` | Seconds for the outbound TCP connect. A slower connect gets `502`. Range 1-120. | The home network connects slowly. |
| `idle_timeout_s` | `900` | Seconds without traffic in either direction after which a tunnel closes. Range 1-86400. | Long idle connections close too early. |
| `check_interval_s` | `10` | Seconds between self health checks. Three failures in a row exit with code 1. Range 0.1-3600. | Rarely. |

The keys `bind`, `host`, `listen` and `address` are refused with exit code 78. No parameter sets the listen
address.

## Block `field`

### Machine, ports and ssh

| Key | Default | Meaning | When to change |
|---|---|---|---|
| `machine` | none (required) | The home machine's name in the `machines` block of the fleet-superpowers inventory. Its `reach[].ssh` list is the only source of ssh targets. Letters, digits, `_`, `.` and `-`. A missing value exits with code 78. | Always. Set it to your home machine's name, as `fleetmux superpowers show --machines` prints it. |
| `local_port` | `17892` | Loopback port on the field machine that sessions connect to. Range 1024-65535. Not a refused port. Not inside `route_ports`. | Another program on the field machine uses the port. Change `session_env` in `teams/field-applet.yaml` too. |
| `remote_port` | `17891` | The home proxy's port on the home machine's loopback. Range 1024-65535. | You changed `home.port`. Use the same value. |
| `route_ports` | `[17900, 17915]` | First and last loopback port of the routes' ssh forwards. One port per route. `1024 <= first <= last <= 65535`. No refused port and not `local_port` inside the range. At start the range must hold a port for every route. | Another program uses a port in the range, or the inventory lists more routes than the range holds. |
| `ssh` | `[ssh]` | The ssh command as a non-empty list of strings. | You need another ssh binary. |
| `ssh_options` | `[]` | Extra ssh options as `Key=Value` strings. They come after the fixed options. ssh uses the first value of an option, so the fixed options win. | A route needs an option that is not in your ssh configuration. |
| `server_alive_interval_s` | `15` | ssh `ServerAliveInterval`. Integer 1-3600. | Dead connections must be detected faster or slower. |
| `server_alive_count_max` | `3` | ssh `ServerAliveCountMax`. Integer 1-3600. | Same as above. |
| `connect_timeout_s` | `10` | ssh `ConnectTimeout`, in seconds. Integer 1-3600. | A route needs more time to connect. |

The refused ports are fixed in the code: 7890, 9090, 9091, 17890, 19091, 18888, 16379, 16380, 18790, 18793 and
14001.

### Start, rounds and stop

| Key | Default | Meaning | When to change |
|---|---|---|---|
| `attempt_timeout_s` | `30` | Seconds from one route's ssh start to its first good probe. Later, that start has failed. Range 0.5-600. | A route takes longer to come up. |
| `restart_delay_s` | `5` | Seconds to wait when no route is left, before the first round. Also the first wait between failed rounds; the wait doubles after each failed round in a row, up to `restart_delay_max_s`. A route that comes up resets the wait to this value. Range 0.1-3600. | Rarely. |
| `restart_delay_max_s` | `60` | Upper limit of the wait between failed rounds, in seconds. The wait after failed round `k` in a row is `min(restart_delay_max_s, restart_delay_s * 2 ** (k - 1))`. Range 0.1-3600, and at least `restart_delay_s`. | Raise it to try less often during long times without network. Lower it to reconnect sooner after the network returns. |
| `max_failed_rounds` | `0` | `0`: failed rounds never stop the applet; it keeps the port closed and goes on until a route comes up. A positive number: that many rounds in a row in which no route comes up end the applet with exit code 78, which the fleet does not restart. A wake and a route that comes up set the count to 0. A round that fails within 60 s of a wake is not counted, so short wakes never reach the limit. Integer 0-10000. | Rarely. Set a positive number only if you want the applet to stop after that many failed rounds. |
| `wake_jump_s` | `10` | Seconds of clock jump between two passes of the loop that count as a wake from sleep: the wall clock moved this much more than the monotonic clock, or a pass came this much later than planned. A wake probes every up route at once, or starts a round at once when no route is up, and resets the round number and the `max_failed_rounds` count. Range 1-86400. | Raise it if a busy machine reports wakes that were not sleep. Lower it to catch shorter sleeps. |
| `net_check_interval_s` | `1` | Seconds between light network checks while no route is up, and the longest pause of the loop. A check reads the local source address for a documentation address; it sends nothing and never contacts the API. Only the change from no network to a network starts a round at once. Range 0.2-60. | Rarely. |
| `wake_probe_timeout_s` | `1` | Time limit of the probe of each up route right after a wake. A route that does not answer within it is stopped and started again at once. Probes in flight from before the sleep are dropped. Other probes use `probe_timeout_s`. Range 0.1-60. | A route answers slowly right after a wake and is restarted for no reason. Raise it. |
| `check_interval_s` | `10` | Seconds between probes of the route in use. Range 0.1-3600. | Failures of the route in use must be found faster or slower. |
| `unhealthy_restart_s` | `60` | Seconds of failing probes after which an up route counts as a drop. Range 1-86400. | Rarely. |
| `stop_wait_s` | `5` | On SIGTERM: seconds for each ssh stop and for the confirmation that the listeners are gone. Range 0.1-60. | Rarely. Keep the row's `stop_grace_s` larger than the stop takes. |

An older parameter file may set `max_failed_rounds: 10`. That file keeps the old stop after 10 failed rounds. Set
the value to `0` or remove the line to keep the applet reconnecting.

### Measurement and score

| Key | Default | Meaning | When to change |
|---|---|---|---|
| `probe_timeout_s` | `5` | Time limit of one probe. A slower answer is a failed probe. Range 0.1-600. | All routes are slow but usable. |
| `samples` | `5` | Probes per measurement. The speed is the median of the last this many good probes. Integer 1-100. | Scores change too much between measurements. |
| `sample_gap_s` | `1` | Seconds between the probes of one measurement. Range 0-3600. | Rarely. |
| `measure_interval_s` | `60` | Seconds between measurements of the routes not in use. The inventory is read again at this time. Range 1-86400. | Standby routes must be measured more or less often. |
| `window_s` | `600` | Seconds of history the score uses. Range 1-604800. | Old failures must count for a shorter or longer time. |
| `fail_penalty_ms` | `5000` | Score added at a failed-probe share of 1. Range 0-10000000. | Routes with failed probes must count as worse or better. |
| `drop_penalty_ms` | `2000` | Score added for each drop in the window. Range 0-10000000. | Routes that drop must count as worse or better. |

### Switching

| Key | Default | Meaning | When to change |
|---|---|---|---|
| `switch_margin_ratio` | `0.3` | A better route must score at most `(1 - ratio)` times the route in use. Range 0 or more, below 1. | Switches happen too often or too rarely. |
| `switch_margin_ms` | `50` | A better route must score at least this many ms lower. Range 0-10000000. | Same as above. |
| `switch_hold_s` | `120` | Seconds a better route must stay better without a break. Range 0-86400. | Same as above. |
| `switch_cooldown_s` | `300` | Seconds after a switch between routes during which no switch to a better route happens. Range 0-86400. | Same as above. |
| `failover_after_fails` | `3` | Failed probes in a row on the route in use before an immediate switch to the best other route. Integer 1-1000. | Failover must happen sooner or later. |

## Other files with values

| File | Value | Must equal |
|---|---|---|
| `teams/field-applet.yaml` | `session_env.port` and the port in `HTTPS_PROXY` and `HTTP_PROXY` | `field.local_port` |
| `workflow.yaml` | `applets[0].machine` | `field.machine` |
