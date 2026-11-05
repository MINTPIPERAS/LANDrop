"""Verified, resumable SFTP uploads using a remote partial file."""

from __future__ import annotations

import hashlib
import os
import posixpath
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import paramiko

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]


class TransferError(RuntimeError):
    """An upload failed without publishing a partial file as complete."""


class TransferCancelled(TransferError):
    """The caller requested cancellation; the remote partial is preserved."""


@dataclass(frozen=True, slots=True)
class TransferResult:
    local_path: Path
    remote_path: str
    size: int
    sha256: str
    resumed_from: int


def _stat_size(sftp: paramiko.SFTPClient, path: str) -> int | None:
    try:
        return sftp.stat(path).st_size
    except OSError as exc:
        if getattr(exc, "errno", None) == 2 or "no such file" in str(exc).lower():
            return None
        raise


def _digest_local(path: Path, cancel_check: CancelCheck) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            if cancel_check():
                raise TransferCancelled("传输已取消")
            digest.update(chunk)
    return digest.hexdigest()


def _digest_remote(sftp: paramiko.SFTPClient, path: str, cancel_check: CancelCheck) -> str:
    digest = hashlib.sha256()
    with sftp.open(path, "rb") as remote:
        while True:
            if cancel_check():
                raise TransferCancelled("传输已取消")
            chunk = remote.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _check_cancel(cancel_check: CancelCheck) -> None:
    if cancel_check():
        raise TransferCancelled("传输已取消，已保留远端临时文件")


def _source_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _publish(sftp: paramiko.SFTPClient, partial_path: str, final_path: str, overwrite: bool) -> None:
    if not overwrite and _stat_size(sftp, final_path) is not None:
        raise TransferError("远端目标文件已存在")

    if overwrite:
        # posix_rename is atomic and replaces an existing target when supported.
        try:
            sftp.posix_rename(partial_path, final_path)
            return
        except (OSError, paramiko.SSHException, AttributeError) as exc:
            raise TransferError(
                "远端不支持安全的原子覆盖改名；原文件未删除，完整临时文件已保留"
            ) from exc

    try:
        sftp.rename(partial_path, final_path)
    except (OSError, paramiko.SSHException) as exc:
        raise TransferError("无法将临时文件改名为最终文件；临时文件已保留") from exc


def upload_file(
    *,
    host: str,
    username: str,
    local_path: str | Path,
    remote_dir: str,
    port: int = 22,
    auth_method: str = "key",
    password: str | None = None,
    private_key_path: str | Path | None = None,
    known_hosts_path: str | Path | None = None,
    connect_timeout: float = 15.0,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | threading.Event | None = None,
    overwrite: bool = False,
    client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
) -> TransferResult:
    """Upload one file, resuming ``.landrop.part`` and verifying SHA-256.

    Unknown host keys are rejected. Use ``known_hosts_path`` to specify the
    trust database; provisioning a new host key requires a separate explicit
    user-confirmation flow and is intentionally not performed here.
    """
    if not host.strip() or not username.strip():
        raise ValueError("远端主机地址和用户名不能为空")
    if not 1 <= port <= 65535:
        raise ValueError("SSH 端口必须在 1 到 65535 之间")
    if auth_method not in {"key", "password"}:
        raise ValueError("认证方式必须是 key 或 password")
    if auth_method == "password" and password is None:
        raise ValueError("密码认证需要提供密码")
    if auth_method == "key" and private_key_path is not None:
        if not Path(private_key_path).expanduser().is_file():
            raise ValueError("指定的 SSH 私钥文件不存在")

    source_path = Path(local_path).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError("本地文件不存在或不是普通文件")
    if not remote_dir.strip():
        raise ValueError("远端目录不能为空")

    if cancel is None:
        cancel_check: CancelCheck = lambda: False
    elif isinstance(cancel, threading.Event):
        cancel_check = cancel.is_set
    else:
        cancel_check = cancel

    final_path = posixpath.join(remote_dir.rstrip("/"), source_path.name)
    partial_path = final_path + ".landrop.part"
    initial_signature = _source_signature(source_path)
    total = initial_signature[0]

    client = client_factory()
    sftp: paramiko.SFTPClient | None = None
    try:
        client.load_system_host_keys()
        if known_hosts_path is not None:
            known_hosts = Path(known_hosts_path).expanduser()
            if not known_hosts.is_file():
                raise TransferError(f"known_hosts 文件不存在: {known_hosts}")
            client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        connect_args: dict[str, object] = {
            "hostname": host,
            "port": port,
            "username": username,
            "timeout": connect_timeout,
            "banner_timeout": connect_timeout,
            "auth_timeout": connect_timeout,
            "allow_agent": auth_method == "key",
            "look_for_keys": auth_method == "key",
        }
        if auth_method == "password":
            connect_args["password"] = password
            connect_args["allow_agent"] = False
            connect_args["look_for_keys"] = False
        elif private_key_path:
            connect_args["key_filename"] = str(Path(private_key_path).expanduser())
        client.connect(**connect_args)
        sftp = client.open_sftp()

        _check_cancel(cancel_check)
        if _stat_size(sftp, final_path) is not None and not overwrite:
            raise TransferError("远端目标文件已存在")

        offset = _stat_size(sftp, partial_path) or 0
        if offset > total:
            raise TransferError("远端临时文件大于本地文件，状态冲突；未自动覆盖或截断")
        if offset == total:
            if _source_signature(source_path) != initial_signature:
                raise TransferError("本地文件在校验前发生变化，请重新选择文件")
            source_hash = _digest_local(source_path, cancel_check)
            remote_hash = _digest_remote(sftp, partial_path, cancel_check)
            if source_hash != remote_hash:
                raise TransferError("远端临时文件校验失败；请清理临时文件后重新发送")
        else:
            with source_path.open("rb") as source:
                source.seek(offset)
                with sftp.open(partial_path, "ab") as remote:
                    sent = offset
                    if progress:
                        progress(sent, total)
                    while chunk := source.read(1024 * 1024):
                        _check_cancel(cancel_check)
                        remote.write(chunk)
                        sent += len(chunk)
                        if progress:
                            progress(sent, total)

            _check_cancel(cancel_check)
            if _source_signature(source_path) != initial_signature:
                raise TransferError("本地文件在传输期间发生变化；已保留远端临时文件")
            remote_size = _stat_size(sftp, partial_path)
            if remote_size != total:
                raise TransferError(
                    f"远端临时文件长度不匹配：期望 {total} 字节，实际 {remote_size} 字节"
                )
            source_hash = _digest_local(source_path, cancel_check)
            remote_hash = _digest_remote(sftp, partial_path, cancel_check)
            if source_hash != remote_hash:
                raise TransferError("SHA-256 校验失败；已保留远端临时文件")

        _check_cancel(cancel_check)
        if _source_signature(source_path) != initial_signature:
            raise TransferError("本地文件在提交前发生变化；已保留远端临时文件")
        _publish(sftp, partial_path, final_path, overwrite)
        if progress:
            progress(total, total)
        return TransferResult(source_path, final_path, total, source_hash, offset)
    except TransferCancelled:
        raise
    except (TransferError, ValueError):
        raise
    except (OSError, paramiko.SSHException) as exc:
        raise TransferError(f"SFTP 传输失败: {exc}") from exc
    finally:
        if sftp is not None:
            sftp.close()
        client.close()
