#!/usr/bin/env python3
"""Archive selected, immutable Bonsai assets; never delete published outputs.

This is an artifact backup, not a secret scanner or a whole-Pod snapshot.
Only explicitly named models and runtime-shaped filenames are selected.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import tempfile

PRIMARY = "models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PQ2_0.gguf"
FILES = (
    PRIMARY,
    "models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf",
    "models/bonsai2-gguf/27B/mmproj-Bonsai-2-27B-f16.gguf",
    "models/bonsai2-gguf/27B/Bonsai-2-27B-kv-bias.gguf",
)
RUNTIME = re.compile(r"(?:llama-[A-Za-z0-9_-]+|lib[A-Za-z0-9_+.-]+\.so(?:\.[0-9]+)*)\Z")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def regular_target(root, candidate):
    """Reject symlinked parents; materialize only contained relative file links."""
    current = candidate
    for _ in range(40):
        relative = current.relative_to(root)
        parent = root
        for part in relative.parts[:-1]:
            parent /= part
            if parent.is_symlink():
                raise ValueError(f"Symlinked source directory: {parent}")
        info = current.lstat()
        if stat.S_ISREG(info.st_mode):
            return current
        if not stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Not a regular asset: {current}")
        link = os.readlink(current)
        if os.path.isabs(link):
            raise ValueError(f"Absolute asset symlink: {current}")
        current = Path(os.path.normpath(current.parent / link))
        if not current.is_relative_to(root):
            raise ValueError(f"Asset symlink escapes source root: {candidate}")
    raise ValueError(f"Too many symlink hops: {candidate}")


def selected(root):
    paths = []
    for relative in FILES:
        path = root / relative
        if os.path.lexists(path):
            regular_target(root, path)
            paths.append(path)
    folder = root / "bin/cuda"
    # Do not traverse a linked directory, including a linked bin parent.
    if (root / "bin").is_symlink() or folder.is_symlink():
        raise ValueError("Runtime directories must not be symbolic links")
    if folder.is_dir():
        for path in folder.iterdir():
            if RUNTIME.fullmatch(path.name):
                regular_target(root, path)
                paths.append(path)
    if root / PRIMARY not in paths:
        raise ValueError("Primary PQ2_0 model is missing")
    if folder / "llama-server" not in paths:
        raise ValueError("Required CUDA llama-server is missing")
    return sorted(set(paths))


class HashReader:
    """Hash precisely the stream passed into tarfile, not a separate read."""
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()

    def read(self, size=-1):
        block = self.stream.read(size)
        self.hash.update(block)
        return block


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def backup(root, output):
    root = Path(root).expanduser().resolve(strict=True)
    output = Path(output).expanduser().absolute()
    manifest_path = Path(str(output) + ".manifest.json")
    if not root.is_dir():
        raise ValueError("Source must be a directory")
    if os.path.lexists(output) or os.path.lexists(manifest_path):
        raise FileExistsError("Refusing to overwrite an archive or manifest")
    # resolve also catches a destination parent symlink back into the source.
    if output.resolve().is_relative_to(root):
        raise ValueError("Output must be outside the source tree")
    assets = selected(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"format": 1, "root": "Bonsai-demo", "files": []}
    # A uniquely owned staging directory is the only thing cleaned on failure.
    with tempfile.TemporaryDirectory(prefix=".bonsai-backup-", dir=output.parent) as temp:
        staged_archive = Path(temp) / "assets.tar.gz"
        staged_manifest = Path(temp) / "manifest.json"
        with staged_archive.open("xb") as raw:
            os.chmod(staged_archive, 0o600)
            with tarfile.open(fileobj=raw, mode="w:gz") as archive:
                for path in assets:
                    target = regular_target(root, path)
                    flags = os.O_RDONLY | os.O_NOFOLLOW
                    fd = os.open(target, flags)
                    with os.fdopen(fd, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise ValueError("Asset changed to non-regular file")
                        name = path.relative_to(root).as_posix()
                        info = tarfile.TarInfo(name)
                        info.size = before.st_size
                        info.mode = 0o755 if before.st_mode & 0o111 else 0o644
                        reader = HashReader(stream)
                        archive.addfile(info, reader)
                        if fingerprint(os.fstat(stream.fileno())) != fingerprint(before):
                            raise ValueError(f"Source changed during backup: {name}")
                        if fingerprint(target.stat()) != fingerprint(before):
                            raise ValueError(f"Source replaced during backup: {name}")
                    entry = {"path": name, "size": info.size, "sha256": reader.hash.hexdigest()}
                    if path != target:
                        entry["materialized_from"] = target.relative_to(root).as_posix()
                    manifest["files"].append(entry)
            raw.flush()
            os.fsync(raw.fileno())
        with staged_manifest.open("x", encoding="utf-8") as handle:
            os.chmod(staged_manifest, 0o600)
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link publication fails atomically if a competitor owns a name.
        # Never unlink a final pathname, even if publication of the pair fails.
        os.link(staged_archive, output, follow_symlinks=False)
        os.link(staged_manifest, manifest_path, follow_symlinks=False)
    return output, manifest_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace/ai/Bonsai-demo"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        archive, manifest = backup(args.root, args.output)
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.exit(1, f"Backup failed: {exc}\nNo published file was deleted; inspect partial pairs before retrying.\n")
    print(f"Archive: {archive}\nManifest: {manifest}")
    print(f"SHA-256 archive: {digest(archive)}")
    print("Local artifact backup only. Review the manifest; retain the source data.")


if __name__ == "__main__":
    main()
