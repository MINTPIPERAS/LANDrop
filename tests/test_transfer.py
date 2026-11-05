from __future__ import annotations

import errno
import hashlib
import io
import os
from pathlib import Path

import paramiko
import pytest

from landrop.transfer import TransferCancelled, TransferError, upload_file


class RemoteFile(io.BytesIO):
    def __init__(self, data: bytes, on_close=None):
        super().__init__(data)
        self._on_close = on_close

    def close(self):
        if not self.closed and self._on_close:
            self._on_close(self.getvalue())
        super().close()


class FakeSFTP:
    def __init__(self, files=None, *, posix_rename_supported=True):
        self.files = dict(files or {})
        self.opens = []
        self.closed = False
        self.posix_rename_supported = posix_rename_supported

    def stat(self, path):
        if path not in self.files:
            raise OSError(errno.ENOENT, "No such file", path)
        return type("Stat", (), {"st_size": len(self.files[path])})()

    def open(self, path, mode):
        self.opens.append((path, mode))
        if mode == "rb":
            if path not in self.files:
                raise OSError(errno.ENOENT, "No such file", path)
            return RemoteFile(self.files[path])
        if mode == "ab":
            initial = self.files.get(path, b"")
            remote = RemoteFile(initial, lambda data: self.files.__setitem__(path, data))
            remote.seek(0, io.SEEK_END)
            return remote
        raise AssertionError(f"Unexpected mode: {mode}")

    def rename(self, old, new):
        if new in self.files:
            raise OSError(errno.EEXIST, "Target exists", new)
        self.files[new] = self.files.pop(old)

    def posix_rename(self, old, new):
        if not self.posix_rename_supported:
            raise paramiko.SSHException("extension unsupported")
        self.files[new] = self.files.pop(old)

    def close(self):
        self.closed = True


class FakeSSHClient:
    def __init__(self, sftp, *, connect_error=None):
        self.sftp = sftp
        self.connect_error = connect_error
        self.closed = False
        self.loaded_system_keys = False
        self.loaded_host_keys = []
        self.policy = None
        self.connect_args = None

    def load_system_host_keys(self):
        self.loaded_system_keys = True

    def load_host_keys(self, path):
        self.loaded_host_keys.append(path)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connect_args = kwargs
        if self.connect_error:
            raise self.connect_error

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True


def run_upload(tmp_path: Path, data: bytes, remote_files=None, **kwargs):
    local = tmp_path / "payload.bin"
    local.write_bytes(data)
    sftp = FakeSFTP(remote_files)
    client = FakeSSHClient(sftp)
    result = upload_file(
        host="192.168.1.5",
        username="alice",
        local_path=local,
        remote_dir="/home/alice/Downloads",
        client_factory=lambda: client,
        **kwargs,
    )
    return result, local, sftp, client


def test_new_upload_verifies_and_publishes(tmp_path):
    data = b"new file contents"
    result, _, sftp, client = run_upload(tmp_path, data)

    assert result.remote_path == "/home/alice/Downloads/payload.bin"
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.resumed_from == 0
    assert sftp.files == {result.remote_path: data}
    assert sftp.opens[0] == (result.remote_path + ".landrop.part", "ab")
    assert client.loaded_system_keys
    assert isinstance(client.policy, paramiko.RejectPolicy)
    assert sftp.closed and client.closed


def test_resumes_from_partial_file_and_reports_total_progress(tmp_path):
    data = b"abcdefghij"
    partial = "/home/alice/Downloads/payload.bin.landrop.part"
    result, _, sftp, _ = run_upload(tmp_path, data, {partial: b"abcd"})

    assert result.resumed_from == 4
    assert sftp.files[result.remote_path] == data
    assert sftp.opens[0] == (partial, "ab")


def test_partial_equal_to_local_is_verified_before_publish(tmp_path):
    data = b"complete already"
    partial = "/home/alice/Downloads/payload.bin.landrop.part"
    result, _, sftp, _ = run_upload(tmp_path, data, {partial: data})

    assert result.resumed_from == len(data)
    assert sftp.files[result.remote_path] == data
    assert (partial, "ab") not in sftp.opens


def test_larger_partial_is_rejected_and_preserved(tmp_path):
    partial = "/home/alice/Downloads/payload.bin.landrop.part"
    with pytest.raises(TransferError, match="大于本地文件"):
        run_upload(tmp_path, b"small", {partial: b"too much data"})


def test_existing_destination_is_not_overwritten_by_default(tmp_path):
    final = "/home/alice/Downloads/payload.bin"
    with pytest.raises(TransferError, match="已存在"):
        run_upload(tmp_path, b"new", {final: b"old"})


def test_overwrite_uses_atomic_posix_rename(tmp_path):
    data = b"replacement"
    final = "/home/alice/Downloads/payload.bin"
    result, _, sftp, _ = run_upload(tmp_path, data, {final: b"old"}, overwrite=True)

    assert sftp.files == {result.remote_path: data}


def test_unsupported_atomic_overwrite_keeps_old_file_and_partial(tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"replacement")
    final = "/home/alice/Downloads/payload.bin"
    partial = final + ".landrop.part"
    sftp = FakeSFTP({final: b"old"}, posix_rename_supported=False)
    client = FakeSSHClient(sftp)

    with pytest.raises(TransferError, match="不支持安全的原子覆盖"):
        upload_file(
            host="host", username="alice", local_path=local,
            remote_dir="/home/alice/Downloads", overwrite=True,
            client_factory=lambda: client,
        )

    assert sftp.files[final] == b"old"
    assert sftp.files[partial] == b"replacement"


def test_cancellation_preserves_partial_and_closes_connections(tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"cancel me")
    sftp = FakeSFTP()
    client = FakeSSHClient(sftp)

    with pytest.raises(TransferCancelled):
        upload_file(
            host="host", username="alice", local_path=local,
            remote_dir="/home/alice/Downloads",
            cancel=lambda: bool(sftp.files),
            client_factory=lambda: client,
        )

    assert sftp.files["/home/alice/Downloads/payload.bin.landrop.part"] == b"cancel me"
    assert sftp.closed and client.closed


def test_remote_hash_mismatch_does_not_publish(tmp_path):
    data = b"source data"
    partial = "/home/alice/Downloads/payload.bin.landrop.part"
    with pytest.raises(TransferError, match="校验失败"):
        run_upload(tmp_path, data, {partial: b"other data"})


def test_unknown_hosts_are_rejected_and_password_is_not_saved(tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"abc")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("", encoding="utf-8")
    sftp = FakeSFTP()
    client = FakeSSHClient(sftp)

    upload_file(
        host="host", username="alice", auth_method="password", password="secret",
        local_path=local, remote_dir="/target", known_hosts_path=known_hosts,
        client_factory=lambda: client,
    )

    assert isinstance(client.policy, paramiko.RejectPolicy)
    assert client.loaded_host_keys == [str(known_hosts)]
    assert client.connect_args["password"] == "secret"
    assert client.connect_args["allow_agent"] is False
    assert client.connect_args["look_for_keys"] is False


def test_missing_known_hosts_file_fails_closed(tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"abc")
    client = FakeSSHClient(FakeSFTP())

    with pytest.raises(TransferError, match="known_hosts 文件不存在"):
        upload_file(
            host="host", username="alice", local_path=local, remote_dir="/target",
            known_hosts_path=tmp_path / "missing_hosts", client_factory=lambda: client,
        )

    assert client.closed


def test_invalid_authentication_is_rejected(tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"abc")

    with pytest.raises(ValueError, match="需要提供密码"):
        upload_file(
            host="host", username="alice", auth_method="password", local_path=local,
            remote_dir="/target",
        )


def test_progress_starts_at_resume_offset(tmp_path):
    data = b"abcdefgh"
    partial = "/home/alice/Downloads/payload.bin.landrop.part"
    local = tmp_path / "payload.bin"
    local.write_bytes(data)
    updates = []
    sftp = FakeSFTP({partial: b"abc"})
    client = FakeSSHClient(sftp)

    upload_file(
        host="host", username="alice", local_path=local,
        remote_dir="/home/alice/Downloads", progress=lambda sent, total: updates.append((sent, total)),
        client_factory=lambda: client,
    )

    assert updates[0] == (3, len(data))
    assert updates[-1] == (len(data), len(data))
    assert all(0 <= sent <= total for sent, total in updates)
