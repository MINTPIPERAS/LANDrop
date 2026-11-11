from __future__ import annotations

import hashlib
from pathlib import Path

import paramiko
import pytest

from landrop.transfer import TransferCancelled, TransferError, upload_file

from fake_sftp import FakeSFTP, FakeSSHClient


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


def test_missing_remote_directories_are_created(tmp_path):
    _, _, sftp, _ = run_upload(tmp_path, b"data")

    assert sftp.dirs == {"/home", "/home/alice", "/home/alice/Downloads"}
    assert sftp.files["/home/alice/Downloads/payload.bin"] == b"data"


def test_existing_remote_directory_is_left_untouched(tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"data")
    sftp = FakeSFTP()
    sftp.dirs.add("/home/alice/Downloads")
    client = FakeSSHClient(sftp)

    upload_file(
        host="host", username="alice", local_path=local,
        remote_dir="/home/alice/Downloads", client_factory=lambda: client,
    )

    assert sftp.dirs == {"/home/alice/Downloads"}


def test_target_exists_carries_a_stable_error_code(tmp_path):
    final = "/home/alice/Downloads/payload.bin"

    with pytest.raises(TransferError) as excinfo:
        run_upload(tmp_path, b"new", {final: b"old"})

    assert excinfo.value.code == "target_exists"


def test_hash_mismatch_carries_a_stable_error_code(tmp_path):
    partial = "/home/alice/Downloads/payload.bin.landrop.part"

    with pytest.raises(TransferError) as excinfo:
        run_upload(tmp_path, b"source data", {partial: b"other data"})

    assert excinfo.value.code == "verify_failed"


def test_authentication_failure_is_classified(tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"abc")
    client = FakeSSHClient(
        FakeSFTP(), connect_error=paramiko.AuthenticationException("denied")
    )

    with pytest.raises(TransferError) as excinfo:
        upload_file(
            host="host", username="alice", auth_method="password", password="secret",
            local_path=local, remote_dir="/target", client_factory=lambda: client,
        )

    assert excinfo.value.code == "auth_failed"
    assert client.closed


def test_timeout_is_classified(tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"abc")
    client = FakeSSHClient(FakeSFTP(), connect_error=TimeoutError("timed out"))

    with pytest.raises(TransferError) as excinfo:
        upload_file(
            host="host", username="alice", local_path=local,
            remote_dir="/target", client_factory=lambda: client,
        )

    assert excinfo.value.code == "timeout"


def test_verify_progress_is_monotonic(tmp_path):
    data = b"x" * 3_000_000
    updates: list[tuple[int, int]] = []

    run_upload(
        tmp_path,
        data,
        verify_progress=lambda done, total: updates.append((done, total)),
    )

    assert updates[0] == (0, len(data))
    assert updates[-1] == (len(data), len(data))
    assert all(total == len(data) for _, total in updates)
    values = [done for done, _ in updates]
    assert values == sorted(values)


def test_complete_partial_reports_verify_progress(tmp_path):
    data = b"abcdefghij"
    partial = "/home/alice/Downloads/payload.bin.landrop.part"
    updates: list[tuple[int, int]] = []

    run_upload(
        tmp_path,
        data,
        {partial: data},
        verify_progress=lambda done, total: updates.append((done, total)),
    )

    # The "checking resume" state now reports real progress instead of freezing.
    assert updates
    assert updates[-1] == (len(data), len(data))


def test_stalled_channel_is_reported_as_timeout(tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"data")

    class StallingSFTP(FakeSFTP):
        def open(self, path, mode):
            if mode == "ab":
                raise TimeoutError("channel stalled")
            return super().open(path, mode)

    client = FakeSSHClient(StallingSFTP())

    with pytest.raises(TransferError) as excinfo:
        upload_file(
            host="host", username="alice", local_path=local,
            remote_dir="/target", client_factory=lambda: client,
        )

    assert excinfo.value.code == "timeout"
