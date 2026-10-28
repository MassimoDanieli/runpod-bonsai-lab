# Dynamic SSH tunnel

The shared client discovers the current direct TCP SSH endpoint, validates it,
and supervises one OpenSSH child. It refreshes the endpoint while connected and
before retrying; it does not use autossh with a cached endpoint. Host-key checking
is never disabled and known_hosts is never automatically cleared.

## Lifecycle-managed tunnel

For combined startup/shutdown, use the [up/down/wait guide](LIFECYCLE.md).
It creates a dedicated transient systemd user unit after the remote supervisor is
ready. Do not run it alongside the standalone/manual tunnel on the same port.
`down` stops its matching transient unit, then requests and verifies Pod stop.

## Standalone alternative

The standalone foreground command remains available and never starts/stops a Pod:

```bash
~/.local/bin/runpod-tunnel
```

Install all wrappers and their Python modules together. Configuration is
`~/.config/runpod.env`; see [Configuration](CONFIGURATION.md). The default private
key is `~/.ssh/runpod_ed25519`. The model must listen on remote loopback.

The existing `systemd/runpod-tunnel.service` can still be installed as a user unit
for explicitly managed standalone use. It is **not** the transient lifecycle unit:

```bash
mkdir -p ~/.config/systemd/user
install -m 644 systemd/runpod-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start runpod-tunnel.service
systemctl --user status runpod-tunnel.service
```

Stop this standalone unit explicitly before switching to lifecycle commands:

```bash
systemctl --user stop runpod-tunnel.service
```

The standalone unit and foreground tunnel only manage local forwarding. Stopping
them does not end cloud billing. Keep startup disabled at boot until the live
provider/SSH/user-manager lifecycle has been tested. Reconnects are bounded;
configuration/host-identity errors require operator attention. No script removes
existing host keys to silence a mismatch.
