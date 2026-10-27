"""Synthetic archive regression tests. No model, network, or GPU is used."""
import importlib.util
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "bin" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup = load("bonsai-backup")
restore = load("bonsai-restore")


def race_worker(root, output, gate, queue):
    gate.wait()
    try:
        backup.backup(root, output)
        queue.put("ok")
    except OSError:
        queue.put("collision")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.model = self.root / backup.PRIMARY
        self.model.parent.mkdir(parents=True)
        self.model.write_bytes(b"synthetic model")
        self.runtime = self.root / "bin/cuda"
        self.runtime.mkdir(parents=True)
        self.server = self.runtime / "llama-server"
        self.server.write_bytes(b"synthetic binary")
        self.server.chmod(0o755)
        self.output = self.base / "copy.tar.gz"
        self.dest = self.base / "restored"

    def tearDown(self):
        self.temp.cleanup()

    def pair(self, entries):
        """Build arbitrary regular-file fixtures, including invalid trees."""
        records = []
        with tarfile.open(self.output, "w:gz") as archive:
            for name, data in entries:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
                records.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        manifest = Path(str(self.output) + ".manifest.json")
        manifest.write_text(json.dumps({"format": 1, "files": records}))
        return self.output, manifest

    def test_roundtrip_and_private_artifacts(self):
        archive, manifest = backup.backup(self.root, self.output)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        restore.restore(archive, manifest, self.dest)
        self.assertEqual((self.dest / backup.PRIMARY).read_bytes(), self.model.read_bytes())
        self.assertEqual((self.dest / "bin/cuda/llama-server").stat().st_mode & 0o777, 0o755)

    def test_runtime_required(self):
        self.server.unlink()
        with self.assertRaises(ValueError):
            backup.backup(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_real_projector_and_kv_bias_selected(self):
        names = ("Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf", "Bonsai-2-27B-kv-bias.gguf")
        for name in names:
            (self.model.parent / name).write_bytes(b"extra")
        _, manifest = backup.backup(self.root, self.output)
        found = {Path(item["path"]).name for item in json.loads(manifest.read_text())["files"]}
        self.assertTrue(set(names).issubset(found))

    def test_env_and_unexpected_files_excluded(self):
        for name in (".env", "server.env", ".env.production", "private.pem", "runpod_ed25519", "notes.txt"):
            (self.runtime / name).write_text("synthetic secret")
        _, manifest = backup.backup(self.root, self.output)
        self.assertEqual(len(json.loads(manifest.read_text())["files"]), 2)

    def test_safe_relative_library_link_materialized(self):
        (self.runtime / "libggml.so.1").write_bytes(b"library")
        (self.runtime / "libggml.so").symlink_to("libggml.so.1")
        archive, manifest = backup.backup(self.root, self.output)
        restore.restore(archive, manifest, self.dest)
        alias = self.dest / "bin/cuda/libggml.so"
        self.assertFalse(alias.is_symlink())
        self.assertEqual(alias.read_bytes(), b"library")

    def test_external_library_link_rejected(self):
        (self.base / "external").write_bytes(b"outside")
        (self.runtime / "libbad.so").symlink_to("../../../external")
        with self.assertRaises(ValueError):
            backup.backup(self.root, self.output)

    def test_symlinked_model_parent_rejected(self):
        folder = self.root / "models"
        folder.rename(self.base / "external-models")
        folder.symlink_to(self.base / "external-models", target_is_directory=True)
        with self.assertRaises(ValueError):
            backup.backup(self.root, self.output)

    def test_competitor_archive_is_never_deleted(self):
        link = os.link
        def collision(src, dest, **kwargs):
            if Path(dest) == self.output:
                self.output.write_bytes(b"other writer")
            return link(src, dest, **kwargs)
        with patch.object(backup.os, "link", side_effect=collision):
            with self.assertRaises(FileExistsError):
                backup.backup(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b"other writer")
        self.assertFalse(list(self.base.glob(".bonsai-backup-*")))

    def test_manifest_publication_failure_preserves_both_final_names(self):
        link = os.link
        manifest = Path(str(self.output) + ".manifest.json")
        def collision(src, dest, **kwargs):
            if Path(dest) == manifest:
                manifest.write_bytes(b"other manifest")
            return link(src, dest, **kwargs)
        with patch.object(backup.os, "link", side_effect=collision):
            with self.assertRaises(FileExistsError):
                backup.backup(self.root, self.output)
        self.assertTrue(self.output.exists())
        self.assertEqual(manifest.read_bytes(), b"other manifest")

    def test_two_writers_preserve_winner(self):
        ctx = multiprocessing.get_context("spawn")
        gate, queue = ctx.Event(), ctx.Queue()
        jobs = [ctx.Process(target=race_worker, args=(self.root, self.output, gate, queue)) for _ in range(2)]
        for job in jobs:
            job.start()
        gate.set()
        results = [queue.get(timeout=10) for _ in jobs]
        for job in jobs:
            job.join(timeout=10)
            self.assertEqual(job.exitcode, 0)
        self.assertCountEqual(results, ["ok", "collision"])
        restore.restore(self.output, Path(str(self.output) + ".manifest.json"), self.dest)

    def test_mutation_aborts_without_published_pair(self):
        original = backup.HashReader.read
        mutated = False
        def mutate(reader, size=-1):
            nonlocal mutated
            data = original(reader, size)
            if not mutated and data == b"synthetic model":
                mutated = True
                self.model.write_bytes(b"changed length")
            return data
        with patch.object(backup.HashReader, "read", mutate):
            with self.assertRaises(ValueError):
                backup.backup(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_hash_mismatch_does_not_publish(self):
        archive, manifest = self.pair([("one", b"one"), ("two", b"two")])
        data = json.loads(manifest.read_text())
        data["files"][1]["sha256"] = "0" * 64
        manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            restore.restore(archive, manifest, self.dest)
        self.assertFalse(self.dest.exists())
        self.assertFalse(list(self.base.glob(".bonsai-restore-*")))

    def test_file_directory_collision_does_not_publish(self):
        archive, manifest = self.pair([("a", b"one"), ("a/b", b"two")])
        with self.assertRaises(ValueError):
            restore.restore(archive, manifest, self.dest)
        self.assertFalse(self.dest.exists())

    def test_noncanonical_paths_rejected(self):
        for name in ("../x", "/abs", "a/./b", "a//b", "a/../b", "a\\b", "a/", "."):
            self.assertFalse(restore.safe_name(name), name)

    def test_bad_hash_and_boolean_size_rejected(self):
        for size, hashed in ((True, "0" * 64), (1, "g" * 64)):
            with self.assertRaises(ValueError):
                restore.manifest_entries({"format": 1, "files": [{"path": "x", "size": size, "sha256": hashed}]}, 10, 100)

    def test_symlink_destination_rejected(self):
        archive, manifest = backup.backup(self.root, self.output)
        target = self.base / "outside"
        target.mkdir()
        self.dest.symlink_to(target, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            restore.restore(archive, manifest, self.dest)
        self.assertEqual(list(target.iterdir()), [])

    def test_symlink_ancestor_rejected(self):
        archive, manifest = backup.backup(self.root, self.output)
        target = self.base / "outside"
        target.mkdir()
        self.dest.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            restore.restore(archive, manifest, self.dest / "new")

    def test_existing_empty_destination_not_replaced(self):
        archive, manifest = backup.backup(self.root, self.output)
        self.dest.mkdir()
        inode = self.dest.stat().st_ino
        with self.assertRaises(FileExistsError):
            restore.restore(archive, manifest, self.dest)
        self.assertEqual(self.dest.stat().st_ino, inode)

    def test_publication_race_preserves_competing_directory(self):
        archive, manifest = backup.backup(self.root, self.output)
        original = restore.publish_directory
        def race(staged, destination):
            destination.mkdir()
            (destination / "other").write_bytes(b"winner")
            original(staged, destination)
        with patch.object(restore, "publish_directory", side_effect=race):
            with self.assertRaises(OSError):
                restore.restore(archive, manifest, self.dest)
        self.assertEqual((self.dest / "other").read_bytes(), b"winner")
        self.assertFalse(list(self.base.glob(".bonsai-restore-*")))

    def test_write_failure_leaves_no_destination(self):
        archive, manifest = backup.backup(self.root, self.output)
        with patch.object(restore.os, "fsync", side_effect=OSError("simulated disk full")):
            with self.assertRaises(OSError):
                restore.restore(archive, manifest, self.dest)
        self.assertFalse(self.dest.exists())
        self.assertFalse(list(self.base.glob(".bonsai-restore-*")))

    def test_resource_limits(self):
        archive, manifest = backup.backup(self.root, self.output)
        for kwargs in ({"max_files": 1}, {"max_bytes": 1}):
            with self.assertRaises(ValueError):
                restore.restore(archive, manifest, self.dest, **kwargs)

    def test_archive_link_rejected(self):
        archive, manifest = self.pair([("link", b"")])
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "elsewhere"
            tar.addfile(info)
        with self.assertRaises(ValueError):
            restore.restore(archive, manifest, self.dest)
        self.assertFalse(self.dest.exists())


if __name__ == "__main__":
    unittest.main()
