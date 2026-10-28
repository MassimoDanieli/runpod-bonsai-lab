#!/usr/bin/env python3
"""Validate an artifact backup and publish a complete tree without replacement.

Linux is required for atomic RENAME_NOREPLACE publication. A missing or empty
existing destination is not treated the same: the final destination MUST NOT
exist. Validation/writes happen in a private sibling staging directory.
"""
import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile

MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def safe_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_+./-]+", name):
        return False
    p = PurePosixPath(name)
    return (not p.is_absolute() and p.as_posix() == name
            and all(part not in (".", "..", "") for part in name.split("/")))


def manifest_entries(data, max_files, max_bytes):
    if not isinstance(data, dict) or data.get("format") != 1:
        raise ValueError("Unsupported manifest format")
    files = data.get("files")
    if not isinstance(files, list) or not 0 < len(files) <= max_files:
        raise ValueError("Invalid file list or file limit exceeded")
    entries = {}
    folded = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or not safe_name(item.get("path")):
            raise ValueError("Unsafe or noncanonical manifest path")
        name = item["path"]
        if name.casefold() in folded:
            raise ValueError("Duplicate or case-aliased path")
        if type(item.get("size")) is not int or item["size"] < 0:
            raise ValueError("Invalid size")
        if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError("Invalid SHA-256")
        folded.add(name.casefold())
        entries[name] = item
        total += item["size"]
    if total > max_bytes:
        raise ValueError("Uncompressed byte limit exceeded")
    for name in entries:
        for parent in PurePosixPath(name).parents:
            if parent.as_posix().casefold() in folded:
                raise ValueError("File/directory path collision")
    return entries


def check_destination(destination):
    dest = Path(destination).expanduser().absolute()
    if ".." in dest.parts or os.path.lexists(dest):
        raise FileExistsError("Destination must not exist and must not contain '..'")
    for parent in dest.parents:
        if parent.is_symlink():
            raise ValueError("Destination ancestor must not be a symbolic link")
    if not dest.parent.is_dir():
        raise ValueError("Create the destination parent directory first")
    return dest


def publish_directory(staged, destination):
    """Atomic no-clobber publication; fail closed on unsupported filesystems."""
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "Atomic restore publication requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staged), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def restore(archive_path, manifest_path, destination, max_files=10000, max_bytes=100 * 1024**3):
    if max_files <= 0 or max_bytes <= 0:
        raise ValueError("Resource limits must be positive")
    with Path(manifest_path).open("rb") as source:
        raw = source.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest is too large")
    expected = manifest_entries(json.loads(raw), max_files, max_bytes)
    dest = check_destination(destination)
    # Only private staging receives bytes before ALL members/hashes pass.
    with tempfile.TemporaryDirectory(prefix=".bonsai-restore-", dir=dest.parent) as temp:
        staged = Path(temp) / "tree"
        staged.mkdir(mode=0o700)
        seen = set()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                if (not member.isfile() or member.sparse is not None
                        or not safe_name(member.name) or member.name not in expected
                        or member.name in seen):
                    raise ValueError("Unexpected, duplicate, sparse or non-regular member")
                if member.size != expected[member.name]["size"]:
                    raise ValueError("Archive/manifest size mismatch")
                seen.add(member.name)
                target = staged.joinpath(*PurePosixPath(member.name).parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Missing member data")
                hashed = hashlib.sha256()
                count = 0
                with stream, target.open("xb") as output:
                    os.chmod(target, 0o600)
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        count += len(block)
                        output.write(block)
                        hashed.update(block)
                    output.flush()
                    os.fsync(output.fileno())
                if count != member.size or hashed.hexdigest() != expected[member.name]["sha256"]:
                    raise ValueError(f"Checksum/length mismatch: {member.name}")
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            if seen != set(expected):
                raise ValueError("Archive member set differs from manifest")
        # Recheck parent paths before atomic no-replace rename.
        check_destination(dest)
        publish_directory(staged, dest)
    return dest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--max-files", type=int, default=10000)
    parser.add_argument("--max-bytes", type=int, default=100 * 1024**3)
    args = parser.parse_args()
    try:
        destination = restore(args.archive, args.manifest, args.destination, args.max_files, args.max_bytes)
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.exit(1, f"Restore failed: {exc}\nNo incomplete destination was published.\n")
    print(f"PASS: verified artifact tree published at {destination}")


if __name__ == "__main__":
    main()
