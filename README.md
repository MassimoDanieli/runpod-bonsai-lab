# RunPod Bonsai Lab

Reproducible **development draft** of a hybrid Bonsai 2 27B setup: OpenCode on local Ubuntu host `nero` and optional RTX 5090 inference on RunPod via private SSH forwarding.

**Status:** scaffold generated, syntax checked, not yet deployed on `nero` or RunPod. Do not publish credentials or government/participant data. Local Bonsai is never reconfigured or stopped by these scripts.

## Files

- `bin/runpod-bonsai`: local CLI; manages existing Pod using `runpodctl pod start/get/stop`, tunnel separately.
- `bin/runpod-tunnel`: validated endpoint configuration and `autossh` tunnel (binds `127.0.0.1:18082`).
- `systemd/runpod-tunnel.service`: `nero` unit.
- `bin/bonsai-watchdog.py`: independent watchdog for the RunPod container. **Dry-run default**.
- `systemd/bonsai-watchdog.service`: optional Pod unit (requires service installation and container systemd, often **not available** in Docker-based Pods; otherwise use supervisor/nohup).
- `config/*.example`: credential-free sample configuration.
- `docs/TECHNICAL_REPORT.md`: architecture, measurements, operational caveats.

## Install on `nero` (review before executing)

```bash
sudo apt-get install -y autossh curl
# Install RunPod CLI and authenticate separately following current official instructions.
sudo install -m 755 bin/runpod-bonsai bin/runpod-tunnel /usr/local/bin/
sudo install -m 644 systemd/runpod-tunnel.service /etc/systemd/system/
mkdir -p ~/.config ~/.ssh
chmod 700 ~/.ssh
cp config/runpod-bonsai.env.example ~/.config/runpod-bonsai.env
cp config/runpod-tunnel.env.example ~/.config/runpod-tunnel.env
chmod 600 ~/.config/runpod-bonsai.env ~/.config/runpod-tunnel.env
# Edit with vim; fill in the Pod ID, then the direct TCP SSH host/port AFTER starting the Pod.
vim ~/.config/runpod-bonsai.env
vim ~/.config/runpod-tunnel.env
sudo systemctl daemon-reload
# Create a dedicated key if absent:
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_runpod -C nero-to-runpod
# Register only ~/.ssh/id_ed25519_runpod.pub in RunPod.
```

Use `runpod-bonsai start`, inspect the current SSH endpoint in `runpodctl pod get POD_ID` / RunPod Connect, update tunnel config in vim, then `runpod-bonsai connect`; `runpod-bonsai status` checks server reachability, `runpod-bonsai stop` stops the tunnel and existing Pod.

**Caveats:** This draft does **not yet automatically parse the changing SSH endpoint** and does **not automatically start llama-server** when the Pod resumes. Those are explicit remaining integration tasks; no false promise of one-command fully ready inference. Pod starts may fail because the original GPU is unavailable. `runpodctl` must be installed and authenticated in the execution context used.

## Watchdog (RunPod Pod)

Copy `bin/bonsai-watchdog.py` to `/workspace/ai/Bonsai-demo/`. Its default mode only logs intended shutdowns. When ready and after testing `/slots` response and authorization for `runpodctl pod stop`, launch it as an independent process (e.g. container supervisor). On typical RunPod Docker templates, systemd PID 1 is unavailable; the example unit is for systemd-capable environments only.

```bash
python3 /workspace/ai/Bonsai-demo/bonsai-watchdog.py --pod-id POD_ID --idle-minutes 45 --unhealthy-minutes 90 --max-hours 8
# Enable actual stopping ONLY after validating runpodctl auth and desired fail-safe behavior:
python3 /workspace/ai/Bonsai-demo/bonsai-watchdog.py --pod-id POD_ID --execute-stop
```

The watchdog observes `/slots`: processing keeps idle timer reset; prolonged unhealthiness after 90 min triggers stop; independent 8-hour session ceiling triggers stop **even during a generation**. This is a deliberate hard-cost ceiling; adjust to protect critical jobs. A network gap is indistinguishable from server failure with this signal alone. Consider adding `ps` and `/health` checks before production.

## Security

`llama-server` on RunPod listens on `127.0.0.1:18080`. Use only direct-TCP SSH forwarding. No RunPod HTTP proxy for model API. SSH host authenticity with `StrictHostKeyChecking=accept-new` is trust-on-first-use; a fresh Pod's identity should be independently verified where possible. **Do not delete/reset known_hosts automatically on every reconnect**: doing so would hide key changes. Use a new dedicated file/identity only when deliberately provisioning a different Pod. RunPod Pod env secrets and the API key must never be committed.

## References

- RunPod Pod CLI: https://docs.runpod.io/runpodctl/reference/runpodctl-remove-pods
- RunPod GPU allocation: https://docs.runpod.io/pods/troubleshooting/zero-gpus
- llama.cpp slot API: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
