"""Versioned streaming AES-256-GCM archives, with authenticated metadata and no-clobber publication."""
import base64
import json
import os
import re
import stat
import struct
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"TNBK1\x00"
CHUNK = 1024 * 1024
MAX_PAYLOAD = 1024 ** 3
MAX_HEADER = 4096


class ArchiveError(Exception):
    pass


@dataclass(frozen=True, repr=False)
class ArchiveKey:
    identifier: str
    material: bytes


@contextmanager
def atomic_output(target):
    """Publish a complete private file only if the destination does not exist, including broken symlinks."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".tagihnet-backup-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            raise ArchiveError("Target sudah ada; tidak akan ditimpa.") from None
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveError("Metadata mengandung field duplikat.")
        result[key] = value
    return result


def _decode_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_json)
    except (ValueError, UnicodeError, RecursionError):
        raise ArchiveError("Format metadata tidak valid.") from None


def generate_key(path):
    key = ArchiveKey(uuid.uuid4().hex, os.urandom(32))
    values = {"format": "tagihnet-backup-key", "version": 1, "id": key.identifier,
              "key": base64.b64encode(key.material).decode("ascii")}
    with atomic_output(path) as stream:
        stream.write(json.dumps(values, sort_keys=True).encode("ascii"))
    return key.identifier


def read_key(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o777 not in {0o400, 0o600}):
            raise ArchiveError("Kunci harus file privat, dengan izin 0600 atau 0400 pada POSIX.")
        raw = stream.read(2049)
    if len(raw) > 2048:
        raise ArchiveError("File kunci terlalu besar.")
    data = _decode_json(raw)
    if (not isinstance(data, dict) or set(data) != {"format", "version", "id", "key"}
            or data["format"] != "tagihnet-backup-key" or type(data["version"]) is not int or data["version"] != 1
            or not isinstance(data["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", data["id"])):
        raise ArchiveError("Format kunci backup tidak valid. Kunci ini terpisah dari kunci sesi/MFA.")
    try:
        material = base64.b64decode(data["key"], validate=True)
    except (TypeError, ValueError):
        raise ArchiveError("Format kunci backup tidak valid.") from None
    if len(material) != 32:
        raise ArchiveError("Kunci backup harus 256-bit.")
    return ArchiveKey(data["id"], material)


def encrypt_stream(source, target, key, kind):
    if kind not in {"sqlite", "postgresql"}:
        raise ArchiveError("Jenis database tidak didukung.")
    nonce = os.urandom(12)
    header = {"version": 1, "algorithm": "AES-256-GCM", "key_id": key.identifier, "kind": kind,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("ascii")
    prefix = MAGIC + struct.pack(">I", len(encoded))
    encryptor = Cipher(algorithms.AES(key.material), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(prefix+encoded)
    size = 0
    with atomic_output(target) as destination:
        destination.write(prefix+encoded)
        while chunk := source.read(CHUNK):
            size += len(chunk)
            if size > MAX_PAYLOAD:
                raise ArchiveError("Payload backup melebihi batas 1 GiB.")
            destination.write(encryptor.update(chunk))
        destination.write(encryptor.finalize())
        destination.write(encryptor.tag)
    return header


def decrypt_stream(source, destination, key):
    """Destination must be private staging or discard; bytes are not authenticated until this returns."""
    source.seek(0, os.SEEK_END)
    size = source.tell()
    source.seek(0)
    prefix = source.read(len(MAGIC)+4)
    if len(prefix) != len(MAGIC)+4 or prefix[:len(MAGIC)] != MAGIC:
        raise ArchiveError("Bukan arsip TagihNet terenkripsi versi yang didukung.")
    length = struct.unpack(">I", prefix[len(MAGIC):])[0]
    if not 1 <= length <= MAX_HEADER or size < len(prefix)+length+16:
        raise ArchiveError("Header/trailer arsip tidak valid atau terpotong.")
    payload_size = size-len(prefix)-length-16
    if payload_size > MAX_PAYLOAD:
        raise ArchiveError("Payload backup melebihi batas 1 GiB.")
    encoded = source.read(length)
    header = _decode_json(encoded)
    if (not isinstance(header, dict) or set(header) != {"version", "algorithm", "key_id", "kind", "nonce", "created_at"}
            or type(header["version"]) is not int or header["version"] != 1 or header["algorithm"] != "AES-256-GCM"
            or not isinstance(header["kind"], str) or header["kind"] not in {"sqlite", "postgresql"}
            or header["key_id"] != key.identifier):
        raise ArchiveError("Versi/metadata/kunci arsip tidak sesuai.")
    try:
        nonce = base64.b64decode(header["nonce"], validate=True)
        created = datetime.fromisoformat(header["created_at"])
        if len(nonce) != 12 or created.utcoffset() is None or created.utcoffset().total_seconds() != 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ArchiveError("Nonce atau waktu metadata tidak valid.") from None
    payload_start = source.tell()
    source.seek(-16, os.SEEK_END)
    tag = source.read(16)
    source.seek(payload_start)
    decryptor = Cipher(algorithms.AES(key.material), modes.GCM(nonce, tag)).decryptor()
    decryptor.authenticate_additional_data(prefix+encoded)
    remaining = payload_size
    while remaining:
        chunk = source.read(min(CHUNK, remaining))
        if not chunk:
            raise ArchiveError("Payload arsip terpotong.")
        destination.write(decryptor.update(chunk))
        remaining -= len(chunk)
    try:
        destination.write(decryptor.finalize())
    except InvalidTag:
        raise ArchiveError("Autentikasi arsip gagal: kunci salah, data berubah atau arsip terpotong.") from None
    return header


class Discard:
    def write(self, chunk):
        return len(chunk)


def verify_archive(path, key):
    with Path(path).open("rb") as source:
        return decrypt_stream(source, Discard(), key)
