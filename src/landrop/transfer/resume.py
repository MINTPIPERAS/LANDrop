"""Verified, resumable SFTP uploads using a remote partial file."""

from __future__ import annotations

import hashlib
import logging
import posixpath
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import paramiko

from landrop.transfer.errors import TransferCancelled, TransferError
from landrop.transfer.remote import (
    classify_connection_error,
    connect_sftp,
    ensure_remote_dir,
    validate_connection,
)

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]

CHUNK_SIZE = 1024 * 1024

logger = logging.getLogger(__name__)


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


def _digest_local(
    path: Path, cancel_check: CancelCheck, progress: ProgressCallback | None = None
) -> str:
    digest = hashlib.sha256()
    done = 0
    size = path.stat().st_size
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            if cancel_check():
                raise TransferCancelled("传输已取消")
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, size)
    return digest.hexdigest()


def _digest_remote(
    sftp: paramiko.SFTPClient,
    path: str,
    cancel_check: CancelCheck,
    progress: ProgressCallback | None = None,
    total: int = 0,
) -> str:
    digest = hashlib.sha256()
    done = 0
    with sftp.open(path, "rb") as remote:
        while True:
            if cancel_check():
                raise TransferCancelled("传输已取消")
            chunk = remote.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total or done)
    return digest.hexdigest()


def _verify_pair(
    sftp: paramiko.SFTPClient,
    source_path: Path,
    remote_path: str,
    total: int,
    cancel_check: CancelCheck,
    verify_progress: ProgressCallback | None,
) -> tuple[str, str]:
    """Hash both sides, reporting one monotonic 0..total progress stream.

    Each side contributes roughly half the range so the bar does not jump back
    when the remote read starts.
    """
    if verify_progress:
        verify_progress(0, total)

    half = total / 2 if total else 0.0

    def local_progress(done: int, _total: int) -> None:
        if verify_progress:
            verify_progress(min(total, int(done / 2)), total)

    def remote_progress(done: int, _total: int) -> None:
        if verify_progress:
            verify_progress(min(total, int(half + done / 2)), total)

    source_hash = _digest_local(
        source_path, cancel_check, local_progress if verify_progress else None
    )
    remote_hash = _digest_remote(
        sftp,
        remote_path,
        cancel_check,
        remote_progress if verify_progress else None,
        total,
    )
    if verify_progress:
        verify_progress(total, total)
    return source_hash, remote_hash


def _timed_verify(
    sftp: paramiko.SFTPClient,
    source_path: Path,
    remote_path: str,
    total: int,
    cancel_check: CancelCheck,
    verify_progress: ProgressCallback | None,
) -> tuple[str, str]:
    started = time.monotonic()
    pair = _verify_pair(
        sftp, source_path, remote_path, total, cancel_check, verify_progress
    )
    elapsed = time.monotonic() - started
    if elapsed > 5:
        logger.info(
            "校验 %s：%s 字节，耗时 %.1fs", source_path.name, total, elapsed
        )
    return pair


def _check_cancel(cancel_check: CancelCheck) -> None:
    if cancel_check():
        raise TransferCancelled("传输已取消，已保留远端临时文件")


def _source_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def coerce_cancel(cancel: CancelCheck | threading.Event | None) -> CancelCheck:
    if cancel is None:
        return lambda: False
    if isinstance(cancel, threading.Event):
        return cancel.is_set
    return cancel


def _publish(sftp: paramiko.SFTPClient, partial_path: str, final_path: str, overwrite: bool) -> None:
    if not overwrite and _stat_size(sftp, final_path) is not None:
        raise TransferError("远端目标文件已存在", code="target_exists")

    if overwrite:
        # posix_rename is atomic and replaces an existing target when supported.
        try:
            sftp.posix_rename(partial_path, final_path)
            return
        except (OSError, paramiko.SSHException, AttributeError) as exc:
            raise TransferError(
                "远端不支持安全的原子覆盖改名；原文件未删除，完整临时文件已保留",
                code="publish_failed",
            ) from exc

    try:
        sftp.rename(partial_path, final_path)
    except (OSError, paramiko.SSHException) as exc:
        raise TransferError(
            "无法将临时文件改名为最终文件；临时文件已保留", code="publish_failed"
        ) from exc


def upload_one(
    sftp: paramiko.SFTPClient,
    source_path: Path,
    remote_dir: str,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    overwrite: bool = False,
    verify_progress: ProgressCallback | None = None,
) -> TransferResult:
    """Upload one local file over an existing session.

    ``remote_dir`` must already exist; the directory transfer creates parents
    once and then reuses the session for every file.
    """
    check = cancel_check or (lambda: False)
    base_dir = remote_dir.rstrip("/") or "/"
    final_path = posixpath.join(base_dir, source_path.name)
    partial_path = final_path + ".landrop.part"
    initial_signature = _source_signature(source_path)
    total = initial_signature[0]

    _check_cancel(check)
    if _stat_size(sftp, final_path) is not None and not overwrite:
        raise TransferError("远端目标文件已存在", code="target_exists")

    offset = _stat_size(sftp, partial_path) or 0
    if offset > total:
        raise TransferError(
            "远端临时文件大于本地文件，状态冲突；未自动覆盖或截断",
            code="state_conflict",
        )
    if offset == total:
        if _source_signature(source_path) != initial_signature:
            raise TransferError(
                "本地文件在校验前发生变化，请重新选择文件", code="local_changed"
            )
        source_hash, remote_hash = _timed_verify(
            sftp, source_path, partial_path, total, check, verify_progress
        )
        if source_hash != remote_hash:
            raise TransferError(
                "远端临时文件校验失败；请清理临时文件后重新发送",
                code="verify_failed",
            )
    else:
        started = time.monotonic()
        with source_path.open("rb") as source:
            source.seek(offset)
            with sftp.open(partial_path, "ab") as remote:
                sent = offset
                if progress:
                    progress(sent, total)
                while chunk := source.read(CHUNK_SIZE):
                    _check_cancel(check)
                    remote.write(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, total)
        elapsed = time.monotonic() - started
        if elapsed > 5:
            logger.info("传输 %s：%s 字节，耗时 %.1fs", source_path.name, total, elapsed)

        _check_cancel(check)
        if _source_signature(source_path) != initial_signature:
            raise TransferError(
                "本地文件在传输期间发生变化；已保留远端临时文件",
                code="local_changed",
            )
        remote_size = _stat_size(sftp, partial_path)
        if remote_size != total:
            raise TransferError(
                f"远端临时文件长度不匹配：期望 {total} 字节，实际 {remote_size} 字节",
                code="verify_failed",
            )
        source_hash, remote_hash = _timed_verify(
            sftp, source_path, partial_path, total, check, verify_progress
        )
        if source_hash != remote_hash:
            raise TransferError(
                "SHA-256 校验失败；已保留远端临时文件", code="verify_failed"
            )

    _check_cancel(check)
    if _source_signature(source_path) != initial_signature:
        raise TransferError(
            "本地文件在提交前发生变化；已保留远端临时文件", code="local_changed"
        )
    _publish(sftp, partial_path, final_path, overwrite)
    if progress:
        progress(total, total)
    return TransferResult(source_path, final_path, total, source_hash, offset)


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
    verify_progress: ProgressCallback | None = None,
    cancel: CancelCheck | threading.Event | None = None,
    overwrite: bool = False,
    client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
) -> TransferResult:
    """Upload one file, resuming ``.landrop.part`` and verifying SHA-256.

    Unknown host keys are rejected. Use ``known_hosts_path`` to specify the
    trust database; provisioning a new host key requires a separate explicit
    user-confirmation flow and is intentionally not performed here.
    """
    validate_connection(
        host=host,
        username=username,
        port=port,
        auth_method=auth_method,
        password=password,
        private_key_path=private_key_path,
    )
    source_path = Path(local_path).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError("本地文件不存在或不是普通文件")
    if not remote_dir.strip():
        raise ValueError("远端目录不能为空")

    cancel_check = coerce_cancel(cancel)
    client, sftp = connect_sftp(
        host=host,
        username=username,
        port=port,
        auth_method=auth_method,
        password=password,
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
        connect_timeout=connect_timeout,
        client_factory=client_factory,
    )
    try:
        ensure_remote_dir(sftp, remote_dir)
        return upload_one(
            sftp,
            source_path,
            remote_dir,
            progress=progress,
            cancel_check=cancel_check,
            overwrite=overwrite,
            verify_progress=verify_progress,
        )
    except TransferCancelled:
        raise
    except (TransferError, ValueError):
        raise
    except (OSError, paramiko.SSHException) as exc:
        raise classify_connection_error(exc) from exc
    finally:
        sftp.close()
        client.close()
