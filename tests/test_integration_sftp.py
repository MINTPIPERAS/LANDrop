"""End-to-end transfer tests against a real in-process SFTP server."""

from __future__ import annotations

import hashlib
from pathlib import Path

import paramiko
import pytest

from landrop import hostkeys
from landrop.transfer import TransferError, upload_file

from sftp_server import LocalSftpServer

PASSWORD = "secret"


@pytest.fixture()
def server(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    with LocalSftpServer(remote, username="lin", password=PASSWORD) as sftp:
        yield sftp, remote


def connect_kwargs(server, source: Path, remote_dir: str, known_hosts: Path, **overrides):
    kwargs = {
        "host": "127.0.0.1",
        "port": server.port,
        "username": "lin",
        "password": PASSWORD,
        "auth_method": "password",
        "local_path": source,
        "remote_dir": remote_dir,
        "known_hosts_path": known_hosts,
        "connect_timeout": 10,
    }
    kwargs.update(overrides)
    return kwargs


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def test_real_upload_verifies_and_publishes(tmp_path, server) -> None:
    sftp, remote = server
    known_hosts = sftp.write_known_hosts(tmp_path / "known_hosts")
    source = tmp_path / "payload.bin"
    source.write_bytes(b"real transfer payload" * 1000)

    result = upload_file(**connect_kwargs(sftp, source, "/incoming", known_hosts))

    final = remote / "incoming" / "payload.bin"
    assert final.is_file()
    assert result.sha256 == sha256(source)
    assert final.read_bytes() == source.read_bytes()
    assert not (remote / "incoming" / "payload.bin.landrop.part").exists()


def test_real_upload_resumes_from_partial(tmp_path, server) -> None:
    sftp, remote = server
    known_hosts = sftp.write_known_hosts(tmp_path / "known_hosts")
    source = tmp_path / "resume.bin"
    payload = bytes(range(256)) * 4000
    source.write_bytes(payload)

    partial_dir = remote / "incoming"
    partial_dir.mkdir()
    already = 512_000
    (partial_dir / "resume.bin.landrop.part").write_bytes(payload[:already])

    progress: list[tuple[int, int]] = []
    result = upload_file(
        **connect_kwargs(sftp, source, "/incoming", known_hosts),
        progress=lambda sent, total: progress.append((sent, total)),
    )

    assert result.resumed_from == already
    assert (partial_dir / "resume.bin").read_bytes() == payload
    assert result.sha256 == sha256(source)
    assert progress and progress[0][0] >= already


def test_real_upload_rejects_unknown_host(tmp_path, server) -> None:
    sftp, remote = server
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")
    empty_known_hosts = tmp_path / "known_hosts"
    empty_known_hosts.write_text("", encoding="utf-8")

    with pytest.raises(TransferError):
        upload_file(**connect_kwargs(sftp, source, "/incoming", empty_known_hosts))

    assert not (remote / "incoming" / "payload.bin").exists()


def test_real_upload_rejects_changed_host_key(tmp_path, server) -> None:
    sftp, remote = server
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")

    impostor = paramiko.RSAKey.generate(2048)
    known_hosts = tmp_path / "known_hosts"
    keys = paramiko.HostKeys()
    keys.add(f"[127.0.0.1]:{sftp.port}", impostor.get_name(), impostor)
    keys.save(str(known_hosts))

    with pytest.raises(TransferError):
        upload_file(**connect_kwargs(sftp, source, "/incoming", known_hosts))


def test_real_upload_refuses_to_overwrite_without_flag(tmp_path, server) -> None:
    sftp, remote = server
    known_hosts = sftp.write_known_hosts(tmp_path / "known_hosts")
    source = tmp_path / "existing.bin"
    source.write_bytes(b"new content")
    target_dir = remote / "incoming"
    target_dir.mkdir()
    (target_dir / "existing.bin").write_bytes(b"old content")

    with pytest.raises(TransferError, match="已存在"):
        upload_file(**connect_kwargs(sftp, source, "/incoming", known_hosts))

    assert (target_dir / "existing.bin").read_bytes() == b"old content"


def test_real_upload_wrong_password_does_not_publish(tmp_path, server) -> None:
    sftp, remote = server
    known_hosts = sftp.write_known_hosts(tmp_path / "known_hosts")
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")

    with pytest.raises(TransferError):
        upload_file(
            **connect_kwargs(sftp, source, "/incoming", known_hosts, password="wrong")
        )

    assert not (remote / "incoming" / "payload.bin").exists()


def test_probe_and_trust_round_trip(tmp_path, server, monkeypatch) -> None:
    sftp, _ = server
    known_hosts = tmp_path / "app_known_hosts"
    monkeypatch.setattr(hostkeys, "known_hosts_path", lambda: known_hosts)

    probed = hostkeys.probe("127.0.0.1", sftp.port, timeout=10)
    assert probed.trusted is False
    assert probed.fingerprint.startswith("SHA256:")

    trusted = hostkeys.trust("127.0.0.1", sftp.port, probed.fingerprint, timeout=10)
    assert trusted.trusted is True
    assert known_hosts.is_file()

    again = hostkeys.probe("127.0.0.1", sftp.port, timeout=10)
    assert again.trusted is True


def test_trust_rejects_mismatched_fingerprint(tmp_path, server, monkeypatch) -> None:
    sftp, _ = server
    monkeypatch.setattr(hostkeys, "known_hosts_path", lambda: tmp_path / "known_hosts")

    with pytest.raises(hostkeys.HostKeyError, match="指纹已变化"):
        hostkeys.trust("127.0.0.1", sftp.port, "SHA256:not-the-real-one", timeout=10)
