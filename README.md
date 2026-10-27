# RunPod Bonsai Lab

Hybrid local/cloud inference for **Bonsai 2 27B**, with **OpenCode** on a home Ubuntu server and optional **RunPod RTX 5090** acceleration.

**Status (27 October 2026):** End-to-end manual cloud inference has been validated: RunPod REST start → dedicated SSH → private tunnel → Bonsai health → OpenCode CLOUD response → RunPod REST stop. Automation of tunnel recovery, Pod inference startup, and Pod-side shutdown protection is **work in progress**.

## Architecture

```text
Mac (browser) -- Tailscale HTTPS --> nero / OpenCode (Docker host network)
                                      |-- LOCAL: localhost:18080 (RTX 3060)
                                      +-- CLOUD: localhost:18082
                                            |
                                      private SSH tunnel
                                            |
                                    RunPod localhost:18080 (RTX 5090)
```

The LOCAL provider works even while the cloud Pod is stopped. The RunPod inference server must **not** be exposed using RunPod's public HTTP proxy.

## Project files

- `bin/runpod-bonsai`: REST-based controller for an **existing** Pod (`status`, `start`, `stop`). Start requires an explicit `yes`.
- `bin/runpod-tunnel` and `systemd/runpod-tunnel.service`: **draft** autossh tunnel implementation, not yet deployed/tested.
- `bin/bonsai-watchdog.py`: **draft**, dry-run-by-default watchdog; production shutdown not yet validated.
- `systemd/bonsai-watchdog.service`: example only; typical RunPod template containers do not run systemd.
- `config/*.example`: non-secret configuration examples.
- `docs/TECHNICAL_REPORT.md`: validated setup, measurements, operational issues and remaining work.

## REST controller installation on nero

Requires `bash`, `curl`, `jq`. Inference itself is unaffected by these operations.

```bash
sudo apt-get install -y curl jq autossh
mkdir -p ~/.local/bin ~/.config
install -m 700 bin/runpod-bonsai ~/.local/bin/runpod-bonsai
cp config/runpod-bonsai.env.example ~/.config/runpod.env
chmod 600 ~/.config/runpod.env
vim ~/.config/runpod.env   # Set the existing Pod ID and API key; NEVER commit this file
~/.local/bin/runpod-bonsai status
~/.local/bin/runpod-bonsai start
~/.local/bin/runpod-bonsai stop
```

The Pod's SSH address and port can change after every start. At present, start/stop do **not** automatically create the tunnel or start the inference server. Follow the verified manual steps in `docs/TECHNICAL_REPORT.md` until automated recovery has been tested.

**API access:** a restricted RunPod API key initially returned HTTP 403 in this setup. Grant only required permissions when possible; a full-access key should be treated as **temporary and high-risk**.

## SSH security

Use a dedicated private key (`~/.ssh/runpod_ed25519`) on `nero` with its public key authorized in the RunPod Pod. Keep inference on `127.0.0.1:18080` on the Pod; use direct exposed-TCP SSH forwarding to `127.0.0.1:18082` on `nero`. Do not disable host-key checking. A dedicated known_hosts file with `StrictHostKeyChecking=accept-new` is trust-on-first-use and should be reviewed on endpoint changes.

## Cost and reliability

RunPod's running GPU is billed; stopped Pod storage may also incur charges. The Pod-side watchdog is not currently active. Do not depend on unverified automatic shutdown for cost control. Always confirm `EXITED` after a stop.

Keep all keys, API tokens, passwords, model weights, and real/private participant or government information out of this repository.

See [technical report](docs/TECHNICAL_REPORT.md) for benchmark data and work still required.
