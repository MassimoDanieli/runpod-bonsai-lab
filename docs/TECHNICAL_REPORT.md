# Technical report — Bonsai local/cloud lab

**Date:** 27 October 2026. **Status:** local stack working, cloud GPU benchmarked; automated cloud integration not yet deployed.

## Existing topology

- `nero` — Ubuntu 26.10, Ryzen 5 5600G, RTX 3060 12GB, 32GB RAM; on-prem Bonsai 2 27B in PrismML ternary `PQ2_0`, llama-server on loopback `127.0.0.1:18080`, systemd `bonsai.service`; model context 65,536 and KV4 configuration. GPU power limit service at 145W.
- Docker `opencode-lab` — custom OpenCode 1.19.4 image, Python, document-generation packages and LibreOffice. Docker uses `network_mode: host`; web port 4097 on loopback, Tailscale Serve private HTTPS on port 8443. Document agent can autonomously write project files and execute shell commands within container constraints.
- `obm` — Ubuntu laptop, separate portable/backup OpenCode environment; not required for this phase.
- RunPod — RTX 5090 32GB, Ubuntu 24.04/PyTorch 2.8/CUDA toolkit 12.8, NVIDIA driver 595.91.07 (CUDA 13.2 reported by nvidia-smi), 60GB RAM and 15 vCPU; 30GB persistent Pod disk observed in df. Pod can be stopped to stop GPU metering, storage may continue to cost money.

## Proven cloud test

Bonsai-demo Git commit `294035e` (PrismML-Eng/Bonsai-demo); PrismML CUDA llama.cpp build 10787 commit `73bfe6cc4`. Model `Ternary-Bonsai-2-27B-PQ2_0.gguf` ~6.8 GB and `mmproj-Q8_0.gguf` ~601MB. CUDA device enumeration and `/health` succeeded. Text generation for a Kubernetes prompt: 1000 completion tokens in 7415.899 ms, **134.7106 tokens/s**, 42 prompt tokens. Local reference measurements 34.6 and 36.8 tokens/s under different conditions; indicative ratios 3.89x and 3.66x, **not controlled benchmarks**.

## Intended cloud plumbing

Mac → private Tailscale → `nero` OpenCode (`network_mode:host`) → `127.0.0.1:18082` autossh local forward → RunPod `127.0.0.1:18080` llama-server. OpenCode provider `bonsai-cloud` exists but tunnel not yet live. Confirm exact `/v1/models` model ID upon integration.

## Operating and safety policy

- Local model and its services remain independent if cloud is stopped/unreachable.
- SSH via direct exposed TCP, not public HTTP proxy. API key only if deliberate public model API use.
- RunPod SSH IP/port may change after stop/start; fetch current direct-TCP endpoint, never assume fixed address.
- `StrictHostKeyChecking=accept-new` dedicated known_hosts; deliberate host identity review on replacement.
- Pod-side watchdog separate from llama-server, monitors `/slots`, does not stop during active generation based on idle timer; stops on prolonged `/slots` failure and enforces session maximum. Actual shutdown disabled by default until verified.
- Stop means GPU billing ends but storage may remain billable; prepayment/autorecharge settings require verification in account.
- Do not place internal government, personal or regulated data in unapproved external inference service.

## Remaining work

1. RunPod authenticated CLI on nero; generate/register dedicated SSH key.
2. Confirm SSH over direct TCP access for already-running Pod and host identity handling.
3. Configure tunnel endpoint; test `/health` and OpenCode model selector, including model ID.
4. Configure llama-server as persistent process on the RunPod container; current manual launch does not survive container restart.
5. Configure Pod-side watchdog with separate supervision, reliable stop API credentials and tests against active / idle / server down states.
6. Add robust CLI endpoint discovery, health retries, session maximum, and alerting; test real stop/resume and GPU availability behavior.
7. Perform apples-to-apples `llama-bench -p 512 -n 128` on both GPUs with same model/runtime and record power limit, KV/cache and driver.
8. Review every file before pushing to GitHub, and ensure no secrets, keys, real participant data or identifying logs.
