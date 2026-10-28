# Controller configuration and SSH contract

This branch centralizes controller configuration, REST calls and SSH construction
in `bin/runpod-client.py`. The five executable entrypoints are thin wrappers:
`runpod-bonsai`, `runpod-ssh`, `runpod-serve`, `runpod-tunnel` and `runpod-inspect`.
Install the wrappers **and their sibling Python file together**. Linux/Python 3.11+
and OpenSSH are required; the controller no longer shells out to curl/jq/autossh.
The wider repository still uses curl/jq in other scripts and tests.

## Configuration file

Default: `~/.config/runpod.env`. Override its location using the process environment
variable `RUNPOD_BONSAI_CONFIG`. The file must be a regular file, owned by the current
user, with mode 600 or stricter and no symlink components in its path.

The file is parsed **as data**, not executed with `source`. Supported syntax:
`NAME=value`, quoted values, comments, optional `export`. Local filesystem paths
may begin with `~`, `$HOME` or `${HOME}`. Other shell expansion, command substitution,
variable references, duplicate keys and unknown keys are not supported. Existing
simple configurations from the manual setup remain valid. Review custom shell-based
configurations before migrating. Error messages do not print values.

| Key | Default / requirement |
| --- | --- |
| `RUNPOD_POD_ID` | Required existing Pod ID |
| `RUNPOD_API_KEY` | Required API credential; never forwarded to SSH children |
| `RUNPOD_LOCAL_PORT` | `18082`; used by forwarding **and** the controller health check |
| `RUNPOD_REMOTE_PORT` | `18080`; remote forward target and `BONSAI_PORT` sent to the launcher |
| `BONSAI_REMOTE_ROOT` | `/workspace/ai/Bonsai-demo`; canonical absolute POSIX path |
| `BONSAI_REMOTE_STATE_DIR` | `<BONSAI_REMOTE_ROOT>/run`; override when workspace cannot enforce mode 700. Canonical absolute POSIX path; verify privacy and persistence. |
| `RUNPOD_SSH_KEY` | `$HOME/.ssh/runpod_ed25519`; owned private regular file |
| `RUNPOD_KNOWN_HOSTS` | `$HOME/.ssh/known_hosts_runpod`; owned private regular file |
| `RUNPOD_SSH_USER` | `root`; validated Unix-style username |
| `RUNPOD_SSH_HOST_KEY_POLICY` | `accept-new`; only `accept-new` or `yes` allowed |
| `RUNPOD_SSH_TIMEOUT` | `10` seconds; range 1..120 |
| `RUNPOD_API_TIMEOUT` | `30` seconds; range 1..120 |
| `RUNPOD_TUNNEL_REFRESH_SECONDS` | `30`; range 1..300 |
| `RUNPOD_TUNNEL_RETRY_SECONDS` | `5`; range 1..60 |
| `RUNPOD_TUNNEL_MAX_FAILURES` | `6` consecutive failures; range 1..100 |
| `RUNPOD_STATE_DIR` | `$HOME/.local/state/runpod-bonsai`; private lock directory |

The old `runpod-tunnel.env.example` has been removed: it was not consumed by the
current launcher. Fixed `RUNPOD_HOST`/`RUNPOD_PORT` settings are rejected. The optional
old `RUNPOD_BONSAI_BIN` override is no longer used; wrappers use the sibling module.
The watchdog's Pod-side credential/session configuration remains separate.

## Controller commands

```bash
runpod-bonsai check-config       # local validation only; no API request
runpod-bonsai status             # read Pod status, then configured loopback health
runpod-bonsai endpoint           # current SSH IP and port, two lines
runpod-bonsai start              # explicit yes prompt; EOF means cancellation
runpod-bonsai start --yes        # deliberate consent to incur GPU/storage charges
runpod-bonsai stop               # request stop; then verify status is EXITED
```

`--yes` is accepted **only** for `start`. It bypasses the prompt, not validation,
capacity constraints or readiness checks. Starting an already RUNNING Pod is a
no-op; starting from an unrecognized/transitional state is refused. A stop request
never creates/deletes a Pod. Action POSTs are not retried automatically, including
ambiguous timeouts. After a timeout, inspect status before issuing another action.

Exit codes: `0` success/cancel/no-op; `1` API/transport/runtime error; `2` invalid
arguments/configuration; `3` confirmed `GPU_UNAVAILABLE`; `4` endpoint requested for
a non-RUNNING Pod; `75` exhausted tunnel reconnect budget. Local process stop and
SSH failure do not imply that a paid Pod stopped.

REST uses the v2 route observed in this repository's tested setup:
`GET https://api.runpod.io/v2/pods/{id}` and
`POST https://api.runpod.io/v2/pods/{id}/action` with start/stop JSON. The response is
validated against the configured Pod ID. No redirect or implicit HTTP proxy is
followed, response size is bounded, and error/action bodies are not printed.
No assumption is made about unverified session timestamps or another API version.

## SSH commands and remote paths

```bash
runpod-ssh -- uname -a
runpod-serve
```

Arguments after `--` are literal remote arguments, not unquoted shell fragments.
A POSIX quoting step is required because SSH joins a remote command into shell text.
`runpod-serve` invokes `env BONSAI_ROOT=... BONSAI_PORT=...` and the installed
`<BONSAI_REMOTE_ROOT>/bin/bonsai-server-start`. Spaces/quotes in that root are quoted;
relative/parent-traversing paths are rejected. The root/port may be changed only
when the corresponding Pod scripts and model are correctly installed there.

SSH uses a dedicated identity, batch/public-key authentication, no password fallback,
no agent/X11 forwarding, no connection multiplexing, bounded connection/keepalive
settings and no user/system SSH configuration. The latter deliberately excludes
unexpected ProxyCommand, forwards and LocalCommand settings from automation.
SSH_AUTH_SOCK is retained so an unlocked dedicated key can be used; the agent is
never forwarded to the Pod. RUNPOD_/BONSAI_ variables are removed from child env.

## Host-key migration and rotation

Both command execution and tunnels use `HostKeyAlias=runpod-<POD_ID>` and the same
private known_hosts file. This pins a learned host identity across changes in the
Pod's public IP/port rather than learning a new identity at each endpoint.

`accept-new` is trust on first use, **not independent verification**. Existing
IP:port-only entries from the manual setup are not automatically converted to the
new alias. On first use, verify the fingerprint through a trusted independent
channel where possible, or prepopulate the alias and set policy to `yes`.
A replacement Pod/rebuilt SSH daemon can legitimately have a different key; a
changed key must fail and be explicitly reviewed. Scripts never erase known_hosts,
disable verification or automatically accept a changed key.

`runpod-inspect` now prints only an allowlisted status/endpoint summary. It reports
whether custom launch arguments are present, but does not print their contents or
any `env` object. Private local file paths are configuration, not GitHub artifacts.

## Trust boundary

These checks protect against accidental expansion, stale mappings and unintentional
credential forwarding; they do not sandbox the account owner/root. PATH, the Python
runtime, installed scripts, configuration directory and RunPod account must be
trusted. API access to a live Pod and genuine SSH key rotation remain acceptance
checks to perform before deployment; this branch's tests are offline.

References: [OpenSSH remote command handling](https://man.openbsd.org/ssh.1),
[SSH host-key/alias and configuration options](https://man.openbsd.org/ssh_config.5).
