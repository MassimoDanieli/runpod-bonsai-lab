# Controlled Bonsai artifact migration

Keep the original Pod/data until the restored runtime has completed real inference.
The scripts copy selected artifacts; they are not a full filesystem image, package
manager snapshot, secret scanner or guarantee of CUDA compatibility.

## Source and inventory

A backup requires access to the source filesystem. It cannot retrieve an
inaccessible stopped Pod's volume. An existing local copy can also be used, but
its runtime/model compatibility must be verified separately.

Required: primary `Ternary-Bonsai-2-27B-PQ2_0.gguf` and `bin/cuda/llama-server`.
Optional known model names include `Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf`, the
legacy f16 projector name, and `Bonsai-2-27B-kv-bias.gguf` in the 27B model directory.
Only top-level `llama-*` executable-shaped names and `lib*.so[.VERSION]` files in
`bin/cuda` are selected. Review any runtime with another directory layout.

Contained relative file symlinks are **materialized as regular files**, with the
resolved relative target recorded in the manifest. Absolute/escaping links and
symlinked source subdirectories are rejected. This retains library alias names
without restoring symlinks. Filename selection is not a content-based secret scan:
review the manifest and keep immutable assets in a trusted source directory.

## Backup

From the repository root on a machine with the source and enough free disk space:

```bash
python3 bin/bonsai-backup.py \
  --root /workspace/ai/Bonsai-demo \
  --output /workspace/bonsai-migration.tar.gz
```

Both output archive and `.manifest.json` are private (mode 600). Hashes are computed
from the same bytes passed into the archive; changed/replaced source files cause
failure. No output may be placed inside the source tree.

Creation occurs in a uniquely owned staging directory. Final paths are published
using no-clobber hard links; an existing or concurrently created output is never
removed or overwritten. The manifest is published last. Two filenames cannot be
published atomically as one operation: a crash/collision between publications can
leave an incomplete pair. Inspect it, retain the source, and retry with a NEW name.
The script deliberately never deletes a final pathname during error cleanup.
The destination filesystem must support hard links. This is not a distributed
snapshot or a guarantee against a hostile user changing trusted parent directories.

Transfer the archive AND manifest over an authenticated channel. Hashes prove
consistency, not authenticity if an attacker can replace both. Keep the expected
archive hash separately where practical. Never commit backup artifacts to GitHub.

## Restore (Linux)

Linux `renameat2(RENAME_NOREPLACE)` support is required. The destination parent
must exist, and the final destination **must not exist**, even as an empty directory.
This is an intentional compatibility change from the preliminary implementation.

```bash
mkdir -p /workspace/ai
python3 bin/bonsai-restore.py \
  --archive /workspace/bonsai-migration.tar.gz \
  --manifest /workspace/bonsai-migration.tar.gz.manifest.json \
  --destination /workspace/ai/Bonsai-demo-restored
```

Validation rejects noncanonical/aliased paths, parent symlinks, file/directory
collisions, links/sparse archive members, duplicate members, size/hash mismatches
and limits exceeding `--max-files` (10,000) or `--max-bytes` (100 GiB).
Extraction and hashing happen in a private sibling staging tree. Only after every
check succeeds is the complete directory published atomically without replacing
an existing destination. Failures clean staging, not user data. This does not use
`extractall`. On an unsupported platform/filesystem publication fails closed.

The staging tree consumes disk before publication; enforce host resource limits
for untrusted archives. The Linux tests do not certify every network filesystem,
concurrent hostile modification or sudden power failure scenario.

Reinstall missing OS dependencies, verify the exact CUDA runtime and model settings,
then check model enumeration, `/health` and actual inference. Do not delete the
original Pod simply because archive checksums pass. Update the controller Pod ID
and SSH host identity deliberately when moving to a different Pod.

## Offline validation

```bash
bash tests/run-all.sh
```

Includes round-trip smoke tests and regressions for concurrent backup writers,
source changes, unsafe links, corrupted hashes, destination races, resource limits
and simulated write failure. Tests use small synthetic files and no cloud API.
