# Controlled Bonsai Pod migration

**Status: prepared, not yet deployed or verified on the original Pod.**

The original RunPod Pod may be unable to restart because its GPU host is full. Its 30 GB persistent `/workspace` mount is Pod-attached storage, **not a portable Network Volume**. Do not delete the Pod or assume another Pod can mount that storage. Running GPU time and storage may incur charges.

## Recovery prerequisites

To create a backup, you must first regain access to the original workspace (for example, by successfully starting the original Pod or using a provider-supported recovery route). The tools in this repository **cannot retrieve files from an inaccessible, stopped Pod**.

The main GGUF model is approximately 6.8 GB, so the backup and its transfer require substantial additional space and time. Other files may be larger. Confirm free space before starting.

## Inventory (read-only)

On the **original Pod**, after it is accessible:

```bash
du -sh /workspace/ai/Bonsai-demo
find /workspace/ai/Bonsai-demo/models -type f -printf '%P %s bytes\n'
find /workspace/ai/Bonsai-demo/bin/cuda -maxdepth 2 -type f -printf '%P %s bytes\n'
```

Review model filenames and bin layout. Check for symbolic links and any files needed at runtime; the initial allowlist intentionally excludes symlinks, hidden configuration, keys, source trees, caches, `run/`, and potentially sensitive files. Some excluded files may be necessary to rebuild or operate a different installation.

## Backup

Copy this repository to the original Pod under `/workspace`, then from its root:

```bash
python3 bin/bonsai-backup.py \
  --root /workspace/ai/Bonsai-demo \
  --output /workspace/bonsai-migration.tar.gz
```

The script produces `bonsai-migration.tar.gz` and `bonsai-migration.tar.gz.manifest.json`, listing SHA-256 for each archived file. It refuses to overwrite an existing backup. Currently selected files: the primary 27B PQ2_0 GGUF, optional mmproj at one expected name, and regular non-secret files under `bin/cuda/`. Review/adjust the allowlist for actual Pod contents before treating it as a complete migration.

**Never commit either backup or manifest to GitHub.** Treat both as private artifacts; review the manifest for potentially sensitive file paths. Transfer over authenticated SSH/SCP or an appropriately protected storage service. No upload is performed by the scripts.

## Restore to a new compatible Pod

Do not create or pay for another GPU Pod until the backup has been obtained and validated, and until the replacement's compute cost and storage location are approved. A GPU with sufficient VRAM and a compatible CUDA runtime is required.

On a replacement Pod after transferring the archive and manifest:

```bash
python3 bin/bonsai-restore.py \
  --archive /workspace/bonsai-migration.tar.gz \
  --manifest /workspace/bonsai-migration.tar.gz.manifest.json \
  --destination /workspace/ai/Bonsai-demo-restored
```

Restore refuses a nonempty destination, rejects archive links and unsafe paths, and validates every archived file's SHA-256 before writing. The restored directory contains **selected model and CUDA binary artifacts**, not the full source repository or all container dependencies. Reinstall or clone the pinned PrismML source, rebuild/reinstall any missing runtime dependencies, review executable permissions, and update launch paths if necessary. Validate the GPU runtime and `curl http://127.0.0.1:18080/health` locally before turning on the controller tunnel.

Only after successful inference through OpenCode CLOUD and complete data verification should you consider removing the old Pod. The controller's Pod ID, endpoint and SSH host identity must be updated explicitly for the replacement. No automatic provisioning or deletion is implemented.

## Offline regression test

On any Linux/macOS machine with Python 3:

```bash
python3 tests/test_migration.py
```

The test creates dummy files, checks exclusion and executable permission retention, verifies the restore and confirms the overwrite protection. It does not touch RunPod or transfer any real model data.
