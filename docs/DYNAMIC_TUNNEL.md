# Dynamic SSH tunnel

The foreground `runpod-tunnel` wrapper uses the shared Python controller and
supervises one direct OpenSSH child. It refreshes the current RunPod mapping even
while SSH is connected. **It no longer delegates reconnects to autossh**, which
would otherwise keep reconnecting to its initially discovered address.

## Install on a Linux controller host

```bash
sudo apt-get install -y python3 openssh-client
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
install -m 700 bin/runpod-client.py bin/runpod-bonsai bin/runpod-tunnel bin/runpod-ssh bin/runpod-serve bin/runpod-inspect "$HOME/.local/bin/"
install -m 644 systemd/runpod-tunnel.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
```

Do not overwrite your existing private `~/.config/runpod.env`. Run
`runpod-bonsai check-config` first. Review [configuration](CONFIGURATION.md),
particularly the host-key alias change, stricter file permissions and parser.
Keep the service disabled at boot until live acceptance tests pass.

## Start with an already running Pod

```bash
runpod-bonsai endpoint
systemctl --user start runpod-tunnel.service
systemctl --user status runpod-tunnel.service --no-pager
curl --fail http://127.0.0.1:18082/health
```

Use the configured local port instead of 18082 if changed. A running SSH process
is **not** proof of model readiness: the model must be started separately. The
forward always binds `127.0.0.1` on both ends. No public inference API is created.

## Reconnection behavior

- Discover and validate exactly one `tcp` mapping with integer `private: 22`,
  routable literal IP and integer public port; refuse stale stopped-Pod mappings.
- Periodically GET the current endpoint. If it changes, stop only the directly
  owned SSH child and connect to the new endpoint with the same host-key alias.
- If SSH exits, rediscover before reconnecting. Repeated failures exhaust the
  configured budget and exit 75. Unexpected host-key changes are never bypassed.
- If discovery fails, close the managed SSH connection instead of indefinitely
  trusting stale data, then retry within the failure budget. This can interrupt
  inference during a RunPod control-plane outage; it is an explicit fail-closed choice.
- If the Pod reports EXITED, close the tunnel and exit normally. No API action is sent.
- SIGINT/SIGTERM closes the owned child. A private per-local-port flock prevents
  duplicate managed tunnels; a different existing listener is not adopted/killed.

The supplied user unit does not restart configuration errors (2) or exhausted
reconnects (75). Other failures are rate-limited. After resolving the cause:

```bash
systemctl --user reset-failed runpod-tunnel.service
systemctl --user start runpod-tunnel.service
```

Configuration changes require a service restart. No endpoint polling or SSH
supervision constitutes a provider spending limit. Pod-side dry-run safety does
not stop billing, and the controller can itself lose power/network access.

## Stop

```bash
systemctl --user stop runpod-tunnel.service
runpod-bonsai stop
runpod-bonsai status
```

Confirm EXITED. Tunnel stop alone never stops the billable Pod. No automatic
`up/down/wait` orchestration or provider provisioning is introduced by this change.

## Validation boundary

Offline tests cover endpoint changes, transient discovery/SSH errors, retry
exhaustion, lock ownership, command quoting, credential filtering and cleanup.
A network-free `ssh -G` test validates OpenSSH option parsing in CI. These are not
real RunPod reboot, host-key rotation or paid-GPU acceptance tests.
