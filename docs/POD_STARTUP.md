# Bonsai inference startup on RunPod (experimental)

The tested PyTorch RunPod container does **not** automatically restart a manually launched `llama-server` when a stopped Pod resumes. The scripts in `bin/` provide a repeatable, loopback-only start; **this is not yet a boot-time guarantee**.

## Install on the Pod

Copy or clone the repository onto the **persistent** `/workspace` mount (not just the ephemeral container layer). Assuming the model and the PrismML CUDA binary are already installed under `/workspace/ai/Bonsai-demo`:

```bash
mkdir -p /workspace/ai/Bonsai-demo/bin
install -m 755 bin/bonsai-server bin/bonsai-server-start /workspace/ai/Bonsai-demo/bin/
```

Start explicitly from a RunPod terminal or authenticated SSH session:

```bash
/workspace/ai/Bonsai-demo/bin/bonsai-server-start
curl --fail http://127.0.0.1:18080/health
```

Log: `/workspace/ai/Bonsai-demo/run/server.log`. Stop with the PID **only after confirming that it is the intended process**. The launch script refuses a second start when the health endpoint responds or its PID is alive.

## Automatic launch after Pod restart

The **recommended future integration** is an explicitly configured RunPod container startup command that launches the model runner while keeping Jupyter/SSH services alive. This is template-specific: do not replace the default entrypoint blindly, since that can break SSH or the terminal. Verify the container's startup command and persistence semantics first.

Until then, start the server over authenticated SSH after the Pod reaches RUNNING. The controller on your local host can invoke `bonsai-server-start` after establishing SSH; this step has **not yet been implemented in the lifecycle CLI**. Do not rely on the server being up merely because the Pod is RUNNING.

The model API must remain on `127.0.0.1` and be accessed through the private SSH tunnel, not the public HTTP proxy. Do not commit credentials or sensitive files.

## Independent dry-run watchdog launcher

Copy the orchestration helper and watchdog next to the two server scripts on the Pod's persistent volume:

```bash
install -m 755 bin/bonsai-pod-services-start /workspace/ai/Bonsai-demo/bin/
install -m 644 bin/bonsai-watchdog.py /workspace/ai/Bonsai-demo/bin/
```

On a running Pod execute:

```bash
/workspace/ai/Bonsai-demo/bin/bonsai-pod-services-start
```

The launcher starts `llama-server` in the background, then checks that `/slots` returns a nonempty array of objects with boolean `is_processing`. Only when the endpoint works does it start an **independent Python watchdog process in dry-run mode**. If the model is still loading, retry the helper after the server becomes healthy. The watchdog remains dry-run and cannot stop the Pod via this launcher.

Inspect logs with `tail -n 50 /workspace/ai/Bonsai-demo/run/{server,watchdog}.log`. These processes are not supervised across container restarts; an automatic Pod boot hook is still pending verification of the RunPod template's startup mechanism.

**Cost safety:** dry-run does not stop GPU billing. Always stop the Pod explicitly through the controller or RunPod dashboard and confirm the stopped status. Do not modify the container entrypoint without first inspecting the template's SSH/Jupyter startup configuration.
