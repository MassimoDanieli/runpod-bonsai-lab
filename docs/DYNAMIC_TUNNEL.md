# Dynamic SSH tunnel (experimental)

The controller and tunnel launcher now discover the active SSH host/port using the RunPod REST API. These changes have been committed, but have **not yet been tested against a fresh Pod restart**.

## Install on a Linux controller host

Requires a running RunPod Pod, API credentials in `~/.config/runpod.env` (mode 600), `curl`, `jq`, `openssh-client` and `autossh`. The private SSH key defaults to `~/.ssh/runpod_ed25519`.

```bash
sudo apt-get install -y autossh curl jq openssh-client
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
install -m 700 bin/runpod-bonsai bin/runpod-tunnel "$HOME/.local/bin/"
install -m 644 systemd/runpod-tunnel.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
```

## Check with the Pod running

```bash
"$HOME/.local/bin/runpod-bonsai" status
"$HOME/.local/bin/runpod-bonsai" endpoint
systemctl --user start runpod-tunnel.service
systemctl --user status runpod-tunnel.service
curl --fail http://127.0.0.1:18082/health
```

`endpoint` emits the SSH IP and public port on two lines. The tunnel script rejects missing or ambiguous mappings and binds only to localhost. The model server must be started separately on the Pod before `/health` succeeds.

## Stop

```bash
systemctl --user stop runpod-tunnel.service
"$HOME/.local/bin/runpod-bonsai" stop
"$HOME/.local/bin/runpod-bonsai" status
```

Do not enable the service at boot yet: the Pod may be stopped and cloud inference does not auto-start on Pod boot. The service uses `Restart=on-failure`; monitor logs with `journalctl --user -u runpod-tunnel -e`. The RunPod SSH host key is checked using a dedicated known_hosts file with `accept-new` (trust on first use). Never disable checking or silently erase prior keys.

## Still missing

- Full lifecycle orchestration (start -> wait for RUNNING -> start inference -> tunnel -> health -> stop).
- Independent Pod-side watchdog and tested stop privileges.
- Tests for SSH host key rotation, unavailable GPU allocation and changing Pod port mapping.
