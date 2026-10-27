#!/usr/bin/env python3
"""Verify and restore a Bonsai backup into an empty destination directory."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_name(name):
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and all(x not in (".", "..") for x in p.parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--archive", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--destination", required=True, type=Path)
    args = ap.parse_args()
    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    if data.get("format") != 1 or not isinstance(data.get("files"), list):
        ap.error("Invalid manifest format")
    expected = {}
    for item in data["files"]:
        name = item["path"]
        if not safe_name(name) or name in expected:
            ap.error("Unsafe/duplicate manifest path")
        if not isinstance(item["size"], int) or item["size"] < 0:
            ap.error("Invalid size")
        if not isinstance(item["sha256"], str) or len(item["sha256"]) != 64:
            ap.error("Invalid SHA-256")
        expected[name] = item

    dest = args.destination.expanduser().absolute()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        ap.error("Destination must be absent or empty; never overwrite existing files")
    # No tarfile.extractall: only regular files with manifest entries are accepted.
    with tarfile.open(args.archive, "r:gz") as archive:
        entries = archive.getmembers()
        if len(entries) != len(expected):
            ap.error("Archive and manifest contain different numbers of members")
        found = set()
        for member in entries:
            if not member.isfile() or not safe_name(member.name) or member.name not in expected:
                ap.error("Archive has unsafe, unexpected or non-regular member")
            if member.name in found or member.size != expected[member.name]["size"]:
                ap.error("Duplicate member or size mismatch")
            found.add(member.name)
        if found != set(expected):
            ap.error("Archive member set differs from manifest")

        # Validate every member BEFORE creating any output.
        for member in entries:
            stream = archive.extractfile(member)
            assert stream is not None
            h = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
            if h.hexdigest() != expected[member.name]["sha256"]:
                ap.error(f"Checksum mismatch: {member.name}")

        dest.mkdir(parents=True, exist_ok=True)
        for member in entries:
            target = dest.joinpath(*PurePosixPath(member.name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as src, target.open("xb") as dst:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(block)
            target.chmod(0o755 if (member.mode & 0o111) else 0o644)
            print(f"Verified and restored: {member.name}")
    print("PASS: Restored files match manifest hashes.")


if __name__ == "__main__":
    main()
