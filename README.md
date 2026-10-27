# RunPod Bonsai Lab

Hybrid local/cloud inference for **Bonsai 2 27B**, using a self-hosted OpenAI-compatible client (such as OpenCode) and an optional **RunPod GPU** connected over a private SSH tunnel.

**Project status:** The basic end-to-end workflow has been tested on one Ubuntu host: start an existing RunPod Pod through the REST API, establish an SSH tunnel, access Bonsai from OpenCode, and stop the Pod. Automatic endpoint discovery, tunnel lifecycle management, model-server startup, and Pod-side watchdog shutdown are **not production-ready yet**.

## Architecture

```text
Client / browser
       |
       v
Local Linux host + OpenCode (or another API client)
       |                              |
       |-- LOCAL model (optional)     +-- CLOUD provider: 127.0.0.1:18082
       |                                        |
       |                                 SSH port forwarding
       |                                        |
       +-----------------------------> RunPod Pod: 127.0.0.1:18080
                                                  |
                                             GPU / Bonsai 2
```

The local model, if configured, is independent of RunPod. The cloud model API should remain bound to loopback; **do not expose it via a public RunPod HTTP proxy**.

## Requirements

On the **controller host** (any compatible Linux PC/server):

- Linux, Bash, `curl`, `jq`, and SSH client
- An existing RunPod Pod and a RunPod API key with Pod read/start/stop permissions
- A dedicated SSH key whose public key is authorized on the Pod
- Optional: `autossh` and systemd for the **experimental** managed tunnel

On the **RunPod Pod**:

- A compatible NVIDIA GPU, CUDA runtime, the PrismML Bonsai-enabled llama.cpp build, and Bonsai GGUF model
- `llama-server` bound to `127.0.0.1:18080`
- Direct TCP SSH access (port 22 forwarded by RunPod)

The tested environment used an RTX 5090; the scripts are **not tied to that GPU model**.

## Repository contents

- `bin/runpod-bonsai` — REST controller for an **existing** Pod (`status`, `start`, `stop`). Starting requires explicit confirmation.
- `bin/runpod-tunnel` and `systemd/runpod-tunnel.service` — **draft** SSH tunnel implementation; not yet tested as a managed service. Some paths are still tailored to the original test host and must be adapted before deployment.
- `bin/bonsai-watchdog.py` — **draft**, dry-run-by-default Pod-side safety watchdog.
- `systemd/bonsai-watchdog.service` — illustrative unit for environments where systemd is available; most container Pods do not run systemd as PID 1.
- `config/*.example` — credential-free configuration samples.
- `docs/TECHNICAL_REPORT.md` — details of the original test lab and measured results.

## Install the REST controller (Linux)

Clone this repository on the machine that will manage RunPod:

```bash
git clone https://github.com/MassimoDanieli/runpod-bonsai-lab.git
cd runpod-bonsai-lab
```

On Debian/Ubuntu, install dependencies:

```bash
sudo apt-get update
sudo apt-get install -y curl jq openssh-client
```

On other Linux distributions, install the same tools with the distribution's package manager.

Install the controller for the **current user**:

```bash
mkdir -p "$HOME/.local/bin" "$HOME/.config"
install -m 700 bin/runpod-bonsai "$HOME/.local/bin/runpod-bonsai"
install -m 600 config/runpod-bonsai.env.example "$HOME/.config/runpod.env"
vim "$HOME/.config/runpod.env"
```

Set `RUNPOD_POD_ID` to your **existing** Pod ID and `RUNPOD_API_KEY` to your API key. Keep the configuration local and private (`chmod 600 ~/.config/runpod.env`). **Never commit real credentials.**

Run commands using the explicit path, or add `~/.local/bin` to your `PATH`:

```bash
"$HOME/.local/bin/runpod-bonsai" status
"$HOME/.local/bin/runpod-bonsai" start
"$HOME/.local/bin/runpod-bonsai" stop
```

The CLI reports API results; **start does not mean that inference is ready**. After starting, verify the Pod status, its current direct-TCP SSH endpoint, and the Bonsai health endpoint. After stopping, verify that the Pod reaches `EXITED`.

### Manual SSH tunnel (until automation is completed)

After the Pod starts, obtain its **current** direct TCP SSH host and public port from RunPod. Then, from the controller host:

```bash
ssh -i "$HOME/.ssh/runpod_ed25519" \
  -p <SSH_PUBLIC_PORT> \
  -o IdentitiesOnly=yes -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=accept-new \
  -o "UserKnownHostsFile=$HOME/.ssh/known_hosts_runpod" \
  -N -L 127.0.0.1:18082:127.0.0.1:18080 \
  root@<SSH_PUBLIC_IP>
```

Replace the bracketed placeholders with your Pod's actual values. Generate the key with `ssh-keygen -t ed25519 -f ~/.ssh/runpod_ed25519` if needed and register **only the public key** in RunPod. Do not disable SSH host-key checking. `accept-new` is trust-on-first-use; independently verify host identity where possible.

Once the model server is running inside the Pod, test from the controller:

```bash
curl --fail http://127.0.0.1:18082/health
```

Configure your OpenAI-compatible client to use `http://127.0.0.1:18082/v1`. A client running in Docker must be able to reach the host loopback endpoint (for example using host networking); bridge-network containers do **not** share the host's `127.0.0.1`.

## Security and billing

- Keep API keys, SSH private keys, passwords, model files and confidential data out of Git.
- The reference deployment required temporarily broad API access after restricted permissions returned HTTP 403; **reduce API access to the smallest working scope** when supported.
- Running GPU time incurs charges. Stopped Pods can still incur storage charges, and GPU capacity may not be available when restarting.
- The watchdog is **not currently active**. Do not rely on automatic shutdown until it has been tested with valid stop credentials inside the Pod.
- Do not send confidential or regulated workloads to third-party compute without appropriate authorization.

For the original host-specific setup, tests, performance numbers and remaining engineering tasks, see [Technical Report](docs/TECHNICAL_REPORT.md).
