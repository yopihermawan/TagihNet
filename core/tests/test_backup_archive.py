import base64
import io
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase
from core.backup_archive import (MAGIC, ArchiveError, ArchiveKey, atomic_output, decrypt_stream, encrypt_stream,
    generate_key, read_key, verify_archive)
from core.backup_database import sqlite_backup, sqlite_restore

ROOT = Path(__file__).resolve().parents[2]


class BackupArchiveTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tagihnet-archive-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.key_path = self.root/"keys/backup.json"
        generate_key(self.key_path)
        self.key = read_key(self.key_path)

    def encrypted(self, data=b"Fictitious secret marker - private customer data", kind="sqlite"):
        target = self.root/("archive-"+os.urandom(4).hex()+".tnbk")
        encrypt_stream(io.BytesIO(data), target, self.key, kind)
        return target

    def database(self):
        path = self.root/"source.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO fixture VALUES (1, 'fictitious data')")
        return path

    def rewrite_header(self, archive, change):
        raw = archive.read_bytes()
        length = struct.unpack(">I", raw[len(MAGIC):len(MAGIC)+4])[0]
        header = json.loads(raw[len(MAGIC)+4:len(MAGIC)+4+length])
        header.update(change)
        encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
        archive.write_bytes(MAGIC+struct.pack(">I", len(encoded))+encoded+raw[len(MAGIC)+4+length:])

    def test_key_generation_is_private_unique_and_does_not_overwrite(self):
        self.assertEqual(self.key_path.stat().st_mode & 0o777, 0o600)
        second = self.root/"key-two.json"
        generate_key(second)
        self.assertNotEqual(self.key.identifier, read_key(second).identifier)
        self.assertNotEqual(self.key.material, read_key(second).material)
        before = self.key_path.read_bytes()
        with self.assertRaises(ArchiveError):
            generate_key(self.key_path)
        self.assertEqual(self.key_path.read_bytes(), before)
        self.assertNotIn(base64.b64encode(self.key.material).decode(), repr(self.key))

    def test_rejects_public_mfa_raw_wrong_length_and_malformed_key_files(self):
        for number, raw in enumerate([b"not-json", b"x"*2049, b"[]", b'"string"',
                json.dumps({"format": "tagihnet-backup-key", "version": 1, "id": self.key.identifier, "key": "not-base64!"}).encode(),
                json.dumps({"format": "tagihnet-backup-key", "version": 1, "id": self.key.identifier, "key": base64.b64encode(b"short").decode()}).encode()]):
            path = self.root/f"invalid-key-{number}"
            path.write_bytes(raw)
            path.chmod(0o600)
            with self.assertRaises(ArchiveError):
                read_key(path)
        public = self.root/"public-key.json"
        public.write_bytes(self.key_path.read_bytes())
        for mode in [0o644, 0o700]:
            public.chmod(mode)
            with self.assertRaises(ArchiveError):
                read_key(public)
        public.chmod(0o400)
        self.assertEqual(read_key(public).material, self.key.material)

    def test_key_symlink_and_nonregular_file_are_rejected(self):
        link = self.root/"key-link"
        link.symlink_to(self.key_path)
        with self.assertRaises(OSError):
            read_key(link)
        pipe = self.root/"not-a-key-fifo"
        os.mkfifo(pipe, 0o600)
        with self.assertRaises(ArchiveError):
            read_key(pipe)

    def test_round_trip_streams_multiple_chunks_and_header_contains_no_customer_data(self):
        data = b"private-customer-marker"+os.urandom(2*1024*1024+17)
        archive = self.encrypted(data)
        output = io.BytesIO()
        with archive.open("rb") as source:
            header = decrypt_stream(source, output, self.key)
        self.assertEqual(output.getvalue(), data)
        self.assertEqual((header["version"], header["algorithm"], header["kind"]), (1, "AES-256-GCM", "sqlite"))
        self.assertNotIn(b"private-customer-marker", archive.read_bytes())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(verify_archive(archive, self.key), header)

    def test_random_nonce_makes_identical_backups_different(self):
        first, second = self.encrypted(b"same"), self.encrypted(b"same")
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        self.assertNotEqual(verify_archive(first, self.key)["nonce"], verify_archive(second, self.key)["nonce"])

    def test_wrong_key_and_ciphertext_or_tag_tamper_fail(self):
        archive = self.encrypted()
        path = self.root/"wrong-key.json"
        generate_key(path)
        with self.assertRaises(ArchiveError):
            verify_archive(archive, read_key(path))
        raw = archive.read_bytes()
        for offset in [-1, -17]:
            altered = bytearray(raw)
            altered[offset] ^= 1
            archive.write_bytes(altered)
            with self.assertRaises(ArchiveError):
                verify_archive(archive, self.key)

    def test_authenticated_metadata_change_is_rejected(self):
        archive = self.encrypted()
        self.rewrite_header(archive, {"kind": "postgresql"})
        with self.assertRaises(ArchiveError):
            verify_archive(archive, self.key)

    def test_key_identifier_alone_does_not_authenticate_wrong_key_material(self):
        archive = self.encrypted()
        wrong = ArchiveKey(self.key.identifier, os.urandom(32))
        with self.assertRaises(ArchiveError):
            verify_archive(archive, wrong)

    def test_unknown_version_algorithm_or_invalid_types_are_rejected(self):
        for change in [{"version": 2}, {"version": True}, {"algorithm": "none"}, {"kind": []},
                {"kind": "unknown"}, {"nonce": "invalid!"}, {"created_at": "2026-11-01"}, {"extra": "value"}]:
            archive = self.encrypted()
            self.rewrite_header(archive, change)
            with self.assertRaises(ArchiveError):
                verify_archive(archive, self.key)

    def test_truncated_oversized_duplicate_json_and_wrong_magic_are_rejected(self):
        raw = self.encrypted().read_bytes()
        malformed = [raw[:3], raw[:20], raw[:-1], b"WRONG!"+raw[6:], MAGIC+struct.pack(">I", 10000)+b"x"*20]
        duplicate = b'{"version":1,"version":1}'
        malformed.append(MAGIC+struct.pack(">I", len(duplicate))+duplicate+b"x"*16)
        for data in malformed:
            path = self.root/"malformed.tnbk"
            path.write_bytes(data)
            with self.assertRaises(ArchiveError):
                verify_archive(path, self.key)

    def test_payload_size_limit_rejects_without_publishing_or_leaving_staging_files(self):
        target = self.root/"too-large.tnbk"
        with patch("core.backup_archive.MAX_PAYLOAD", 10):
            with self.assertRaises(ArchiveError):
                encrypt_stream(io.BytesIO(b"x"*11), target, self.key, "sqlite")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".tagihnet-backup-*")), [])

    def test_existing_archive_and_broken_symlink_are_not_overwritten(self):
        for target in [self.root/"existing.tnbk", self.root/"broken-link.tnbk"]:
            if target.name.startswith("broken"):
                target.symlink_to(self.root/"absent")
            else:
                target.write_bytes(b"preserve existing content")
            with self.assertRaises(ArchiveError):
                encrypt_stream(io.BytesIO(b"new"), target, self.key, "sqlite")
            self.assertTrue(os.path.lexists(target))
        self.assertEqual((self.root/"existing.tnbk").read_bytes(), b"preserve existing content")

    def test_interrupted_write_is_not_published(self):
        path = self.root/"interrupted.tnbk"
        with self.assertRaises(RuntimeError):
            with atomic_output(path) as destination:
                destination.write(b"partial")
                raise RuntimeError("fake interrupted operation")
        self.assertFalse(path.exists())
        self.assertEqual(list(self.root.glob(".tagihnet-backup-*")), [])

    def test_parallel_publish_has_one_winner_and_preserves_complete_archive(self):
        path = self.root/"parallel.tnbk"
        def publish(value):
            try:
                encrypt_stream(io.BytesIO(value), path, self.key, "sqlite")
                return True
            except ArchiveError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, [b"one", b"two"]))
        self.assertEqual(sorted(results), [False, True])
        output = io.BytesIO()
        with path.open("rb") as stream:
            decrypt_stream(stream, output, self.key)
        self.assertIn(output.getvalue(), {b"one", b"two"})

    def test_sqlite_consistent_backup_and_restore_preserves_content(self):
        source = self.database()
        archive = self.root/"database.tnbk"
        sqlite_backup(source, archive, self.key)
        target = self.root/"fresh/tagihnet.sqlite3"
        sqlite_restore(archive, target, self.key)
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute("SELECT * FROM fixture").fetchall(), [(1, "fictitious data")])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(target.parent.glob(".tagihnet-restore-*")), [])

    def test_sqlite_authentication_failure_does_not_publish_plaintext(self):
        archive = self.root/"database.tnbk"
        sqlite_backup(self.database(), archive, self.key)
        raw = bytearray(archive.read_bytes())
        raw[-1] ^= 1
        archive.write_bytes(raw)
        target = self.root/"fresh/tagihnet.sqlite3"
        with self.assertRaises(ArchiveError):
            sqlite_restore(archive, target, self.key)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_sqlite_rejects_other_backend_and_invalid_sqlite_payload(self):
        for kind in ["postgresql", "sqlite"]:
            archive = self.encrypted(b"not-a-database", kind=kind)
            target = self.root/f"fresh-{kind}/tagihnet.sqlite3"
            with self.assertRaises((ArchiveError, sqlite3.DatabaseError)):
                sqlite_restore(archive, target, self.key)
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.iterdir()), [])

    def test_restore_does_not_overwrite_existing_database_or_sidecars(self):
        archive = self.root/"database.tnbk"
        plain = self.database()
        sqlite_backup(plain, archive, self.key)
        for suffix in ["", "-wal", "-shm", "-journal"]:
            target = self.root/("destination"+str(len(suffix)))/"tagihnet.sqlite3"
            target.parent.mkdir(exist_ok=True)
            existing = Path(str(target)+suffix)
            existing.write_bytes(b"preserve")
            with self.assertRaises(ArchiveError):
                sqlite_restore(archive, target, self.key)
            self.assertEqual(existing.read_bytes(), b"preserve")
            result = subprocess.run([sys.executable, str(ROOT/"scripts/backup.py"), "restore", str(plain)],
                env={**os.environ, "TAGIHNET_DB_ENGINE": "sqlite", "TAGIHNET_DATA_DIR": str(target.parent)},
                text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(existing.read_bytes(), b"preserve")
            existing.unlink()

    def test_sqlite_backup_with_active_wal_captures_committed_data(self):
        source = self.database()
        with sqlite3.connect(source) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO fixture VALUES (2, 'committed WAL data')")
            db.commit()
            archive = self.root/"wal.tnbk"
            sqlite_backup(source, archive, self.key)
            target = self.root/"wal-restored/tagihnet.sqlite3"
            sqlite_restore(archive, target, self.key)
        with sqlite3.connect(target) as restored:
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM fixture").fetchone()[0], 2)

    def test_cli_keygen_backup_verify_restore_and_wrong_key_are_redacted(self):
        data = self.root/"cli-source"
        data.mkdir()
        self.database().rename(data/"tagihnet.sqlite3")
        archive = self.root/"cli-backup.tnbk"
        env = {**os.environ, "TAGIHNET_DATA_DIR": str(data), "TAGIHNET_DB_ENGINE": "sqlite"}
        def run(*arguments, data_dir=None):
            result = subprocess.run([sys.executable, str(ROOT/"scripts/backup.py"), *map(str, arguments)],
                env={**env, **({"TAGIHNET_DATA_DIR": str(data_dir)} if data_dir else {})}, text=True, capture_output=True)
            self.assertNotIn(base64.b64encode(self.key.material).decode(), result.stdout+result.stderr)
            return result
        self.assertEqual(run("backup", archive, "--key-file", self.key_path).returncode, 0)
        self.assertEqual(run("verify", archive, "--key-file", self.key_path).returncode, 0)
        restored = self.root/"cli-restored"
        self.assertEqual(run("restore", archive, "--key-file", self.key_path, data_dir=restored).returncode, 0)
        self.assertNotEqual(run("restore", archive, "--key-file", self.key_path, data_dir=restored).returncode, 0)
        self.assertNotEqual(run("verify", archive).returncode, 0)
        self.assertNotEqual(run("restore", archive, data_dir=self.root/"no-key").returncode, 0)
        other_key = self.root/"cli-key.json"
        self.assertEqual(run("keygen", other_key).returncode, 0)
        result = run("verify", archive, "--key-file", other_key)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        legacy = self.root/"legacy.sqlite3"
        self.assertEqual(run("backup", legacy).returncode, 0)
        self.assertEqual(run("restore", legacy, data_dir=self.root/"legacy-restored").returncode, 0)
