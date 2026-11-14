# claude-route

claude-route is a fleetmux workflow named `claude-via-home`. It has two applets. Together they send the
Claude sessions of a fleet on one machine through the network of another machine.

## Overview

- The field machine is the machine that cannot reach the Claude API.
- The home machine is a machine that can reach the Claude API.
- The home applet `applet-homeproxy-1` runs an HTTP CONNECT proxy on the home machine's loopback.
- The field applet `applet-tunnel-1` opens ssh forwards from the field machine to that proxy.
- The field fleet starts its Claude sessions with `HTTPS_PROXY` set to the field applet's loopback port.
- No port is opened to the outside. The route is the ssh connection between the two machines.
- The project changes the network path only.

## When to use it

Use it when all of these hold:

1. The field machine cannot reach the Claude API from its current network.
2. The home machine can reach the Claude API.
3. The field machine can reach the home machine over ssh without a prompt.

Loading the workflow sends every Claude session of the field fleet through the home machine. The applets never
test the direct route. They never fall back to it.

## How it works

```
field-machine                                                   home-machine
+------------------------------------------+                    +------------------------------------+
| Claude session                           |                    |                                    |
|   | HTTPS_PROXY=http://127.0.0.1:17892   |                    |                                    |
|   v                                      |                    |                                    |
| applet-tunnel-1 relay 127.0.0.1:17892    |                    |                                    |
|   | picks one route per new connection   |                    |                                    |
|   v                                      |   ssh -N -L        |                                    |
| route port 127.0.0.1:17900 ... 17915 ====+====================+=> applet-homeproxy-1               |
|   (one ssh child per route)              |                    |   127.0.0.1:17891 --CONNECT--> API |
+------------------------------------------+                    +------------------------------------+
```

The field applet reads the home machine's ssh targets from the fleet-superpowers inventory. It starts one
`ssh -N -L` child per target. Each child forwards one loopback route port to the home proxy port. The field
applet listens on `127.0.0.1:<field.local_port>`. It measures every route and hands each new connection to the
best one. The home proxy accepts `CONNECT` to port 443 only and opens the outbound connection from the home
machine. The field fleet holds a new Claude session at launch while the field port is closed.

Neither applet calls an AI model. Neither applet requests a worker. The fleet that hosts an applet only needs a
plain supervisor. Its worker rows may hold any other work.

See [docs/design.md](docs/design.md) for the design.

## Requirements

| Item | Requirement | Check |
|---|---|---|
| fleetmux | Requires a fleetmux release with applet support, including applet tabs and `session_env`. | `fleetmux applet --help` exits 0. |
| Machines | Two machines, each in the fleet-superpowers inventory of both machines. | `fleetmux superpowers show --machines` lists both. |
| Host fleet | One root service fleet on each machine. | `fleetmux status` on each machine. |
| ssh | The field machine reaches the home machine with key authentication and no prompt. | `ssh -o BatchMode=yes home-lan true` exits 0. |
| Python | The fleetmux release Python with PyYAML. The applets use the standard library, PyYAML and the fleetmux applet SDK only. | The fleetmux release provides it. |
| Repository | A clone of this repository at the same path on both machines. | `git -C ~/claude-route log -1` on both machines. |

## Install

`superpowers load` runs the applet files at the commit `source.ref` of `workflow.yaml`. Both clones must
contain that commit. Make your edits on the home machine, then copy the commit to the field machine.

On the home machine:

1. Clone the repository:

   ```sh
   git clone <repository URL> ~/claude-route
   cd ~/claude-route
   ```

2. Set the home machine's inventory name in `claude_route.yaml`. The key `field.machine` has no default:

   ```yaml
   field:
     machine: home-machine   # the home machine's name in the fleet-superpowers inventory
   ```

3. Commit the parameter file and print the commit:

   ```sh
   git commit -am "chore: set field.machine"
   git rev-parse HEAD
   ```

4. Edit `workflow.yaml`. Replace every example value with your own:

   | Key | Example value | Set it to |
   |---|---|---|
   | `source.repo` | `~/claude-route` | The clone path. It must be the same on both machines. |
   | `source.ref` | a commit | The commit from step 3. |
   | `source.from` | `home-machine` | The machine that has that commit. |
   | `applets[0].machine`, `applets[0].fleet` | `home-machine`, `home-fleet` | Your home machine and its root service fleet. |
   | `applets[1].machine`, `applets[1].fleet` | `field-machine`, `field-fleet` | Your field machine and its root service fleet. |
   | `saved_on` | `[home-machine, field-machine]` | Both machine names. |

5. Commit `workflow.yaml`:

   ```sh
   git commit -am "chore: set workflow machines"
   ```

On the field machine:

6. Clone the repository from the home machine:

   ```sh
   git clone home-lan:claude-route ~/claude-route
   cd ~/claude-route
   ```

7. Register each ssh route to the home machine in the inventory:

   ```sh
   fleetmux superpowers add home-machine --ssh home-lan --via lan
   fleetmux superpowers add home-machine --ssh home-vpn --via overlay
   fleetmux superpowers add home-machine --ssh home-relay --via tunnel
   ```

   The field applet tries the routes in this list order. A target is a `Host` alias of your ssh configuration
   or `user@host`, for example `user@192.0.2.9`.

8. Check each route by hand. Each command exits 0 without a prompt:

   ```sh
   ssh -o BatchMode=yes home-lan true
   ```

9. Validate the inventory:

   ```sh
   fleetmux superpowers check
   ```

10. Save the workflow entry on both machines:

    ```sh
    fleetmux superpowers save workflow.yaml --also home-machine
    fleetmux superpowers show --workflow claude-via-home
    ```

## Steps

### Start the host fleets

Each machine needs one root service fleet. Skip this step on a machine that already runs one.

1. Start the home fleet on the home machine:

   ```sh
   fleetmux start --lifecycle service --mode service --backend fleetmux --allow-development-v2 \
     --name home-fleet --roster examples/host-fleet.yaml
   ```

2. Start the field fleet on the field machine:

   ```sh
   fleetmux start --lifecycle service --mode service --backend fleetmux --allow-development-v2 \
     --name field-fleet --roster examples/host-fleet.yaml
   ```

   The field machine cannot reach Claude yet. Name a supervisor CLI and model that it can reach. Add:

   ```sh
   --supervisor-cli pi --supervisor-pi-model <provider/model> \
   --worker-cli pi --pi-model <provider/model> --allow-unsandboxed-pi
   ```

`examples/host-fleet.yaml` is the smallest roster fleetmux accepts. The applets do not use its worker.

### Load

Run the commands on the field machine. Load the home side first.

1. Load the home side:

   ```sh
   fleetmux superpowers load claude-via-home --machine home-machine
   ```

2. Load the field side:

   ```sh
   fleetmux superpowers load claude-via-home --machine field-machine
   ```

`load` returns when the applet reports `up`.

### Check the status

1. List the applets on each machine:

   ```sh
   fleetmux applet list
   ```

2. Read the phase of `applet-tunnel-1` at the top of its tab in the field fleet window. It names the route in
   use.

See [docs/operations.md](docs/operations.md) for the full status and log format.

### Unload

Run the commands on the field machine. Unload the field side first.

1. Unload the field side:

   ```sh
   fleetmux superpowers unload claude-via-home --machine field-machine
   ```

2. Unload the home side:

   ```sh
   fleetmux superpowers unload claude-via-home --machine home-machine
   ```

After both steps no applet process and no listener of this project remains on either machine.

## Configuration

`claude_route.yaml` is the only parameter file. It has a `home` block and a `field` block. An unknown key stops
the applet with exit code 78.

- [docs/configuration.md](docs/configuration.md) lists every key, its default and when to change it.
- `teams/home.yaml` and `teams/field-applet.yaml` hold the `applets:` rows that `load` adds to each host fleet.
- `workflow.yaml` holds the workflow entry `claude-via-home`.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `applet-tunnel-1` is in state `config_error`. | A parameter is wrong, or no route connected. | Read the last line of its log. See [docs/operations.md](docs/operations.md). |
| `config error: ... machine ...` | `field.machine` is missing or is not in the inventory. | Set `field.machine` to the home machine's inventory name. |
| `... is already in use by another program` | Another program listens on `field.local_port` or a route port. | Change the port. See [docs/operations.md](docs/operations.md). |
| `no ssh target of machine ... connects` | No ssh route reached the home proxy. | Fix a route, then run `fleetmux applet start applet-tunnel-1`. |
| Claude sessions on the field machine do not start. | The field port is closed. The fleet holds sessions until it opens. | Check the phase of `applet-tunnel-1`. |
| `load` refuses the checkout. | The applet files differ from `source.ref`, or are not committed. | Commit them, and set `source.ref` to that commit. Save the entry again. |

## License

MIT. See [LICENSE](LICENSE).
