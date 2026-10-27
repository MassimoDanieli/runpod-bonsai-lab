#!/usr/bin/env python3
"""Offline archive/restore smoke test using dummy files only."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as temp:
    base = Path(temp)
    src = base / "Bonsai-demo"
    model = src / "models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PQ2_0.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"FAKE MODEL FOR OFFLINE TEST ONLY")
    binary = src / "bin/cuda/llama-server"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"FAKE LLAMA BINARY")
    binary.chmod(0o755)
    secret = src / "bin/cuda/.env"
    secret.write_text("SECRET=must_not_backup")
    archive = base / "backup.tar.gz"
    manifest = Path(str(archive) + ".manifest.json")
    subprocess.run([sys.executable, str(repo / "bin/bonsai-backup.py"),
                    "--root", str(src), "--output", str(archive)], check=True)
    data = json.loads(manifest.read_text())
    names = [item["path"] for item in data["files"]]
    assert "bin/cuda/.env" not in names, "Secret file included!"
    dest = base / "restored"
    subprocess.run([sys.executable, str(repo / "bin/bonsai-restore.py"),
                    "--archive", str(archive), "--manifest", str(manifest),
                    "--destination", str(dest)], check=True)
    assert (dest / "bin/cuda/llama-server").read_bytes() == b"FAKE LLAMA BINARY"
    assert (dest / "bin/cuda/llama-server").stat().st_mode & 0o111
    assert not (dest / "bin/cuda/.env").exists()
    assert (dest / "models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PQ2_0.gguf").read_bytes() == model.read_bytes()
    second = subprocess.run([sys.executable, str(repo / "bin/bonsai-restore.py"),
                             "--archive", str(archive), "--manifest", str(manifest),
                             "--destination", str(dest)], capture_output=True)
    assert second.returncode != 0, "Restore unexpectedly overwrote existing files"
print("PASS: backup/restore hashes, executable bit, exclusions, and no-overwrite guard")
