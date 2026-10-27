#!/usr/bin/env python3
"""Create a portable Bonsai workspace archive and SHA-256 manifest.

Run on a machine that actually has the source workspace. Does not upload data.
Only explicit allowlisted artifacts are included; secrets and SSH files are never
included. Review the inventory before distributing the archive.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tarfile

FILES = (
    "models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PQ2_0.gguf",
    "models/bonsai2-gguf/27B/mmproj-Bonsai-2-27B-f16.gguf",
)
DIRS = ("bin/cuda",)
EXCLUDE_NAMES = {"run", ".git", ".ssh", ".env", "credentials", "secrets"}


def selected(root):
    files = []
    for relative in FILES:
        p = root / relative
        if p.is_file() and not p.is_symlink():
            files.append(p)
    for relative in DIRS:
        folder = root / relative
        if folder.is_dir() and not folder.is_symlink():
            for p in folder.rglob("*"):
                rel = p.relative_to(root)
                if (p.is_file() and not p.is_symlink()
                        and not any(part in EXCLUDE_NAMES for part in rel.parts)):
                    files.append(p)
    return sorted(set(files))


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path("/workspace/ai/Bonsai-demo"))
    ap.add_argument("--output", type=Path, required=True, help="Destination archive (.tar.gz)")
    args = ap.parse_args()
    root = args.root.resolve(strict=True)
    if not root.is_dir():
        ap.error("--root must be a directory")
    output = args.output.expanduser().absolute()
    manifest_path = Path(str(output) + ".manifest.json")
    if output.exists() or manifest_path.exists():
        ap.error("Refusing to overwrite an archive or manifest")
    paths = selected(root)
    required = root / FILES[0]
    if required not in paths:
        ap.error("Primary Bonsai GGUF not found: verify exact filename/path")
    # Never put output under the input tree.
    if output.is_relative_to(root) or manifest_path.is_relative_to(root):
        ap.error("Output must be outside the source tree")
    manifest = {"format": 1, "root": "Bonsai-demo", "files": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(output, "x:gz") as archive:
            for file in paths:
                relative = file.relative_to(root).as_posix()
                size = file.stat().st_size
                sha = digest(file)
                info = archive.gettarinfo(str(file), arcname=relative)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with file.open("rb") as data:
                    archive.addfile(info, data)
                manifest["files"].append({"path": relative, "size": size, "sha256": sha})
                print(f"Added {relative} ({size} bytes)")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except BaseException:
        output.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise
    print(f"Archive: {output}\nManifest: {manifest_path}")
    print(f"SHA-256 archive: {digest(output)}")
    print("WARNING: Backup is local only. Transfer separately over a secure channel.")


if __name__ == "__main__":
    main()
