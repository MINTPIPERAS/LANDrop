"""Send a whole folder, one resumable file at a time over a single session."""

from __future__ import annotations

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
from landrop.transfer.resume import CancelCheck, ProgressCallback, coerce_cancel, upload_one
from landrop.transfer.scan import DEFAULT_MAX_FILES, scan_directory


@dataclass(frozen=True, slots=True)
class TreeFileResult:
    rel_path: str
    remote_path: str
    size: int
    ok: bool
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class TreeResult:
    local_root: Path
    remote_root: str
    files: tuple[TreeFileResult, ...]
    ok_count: int
    failed_count: int
    skipped: tuple[dict[str, str], ...]
    size: int


# Called after each file with (relative path, files done, files total).
FileProgress = Callable[[str, int, int], None]

logger = logging.getLogger(__name__)


def upload_tree(
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
    file_progress: FileProgress | None = None,
    cancel: CancelCheck | threading.Event | None = None,
    overwrite: bool = False,
    client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
    max_files: int = DEFAULT_MAX_FILES,
) -> TreeResult:
    """Upload every file under ``local_path`` into ``remote_dir``.

    Each file goes through :func:`upload_one`, so resuming and SHA-256 checks
    behave exactly like a single-file send. A failing file is recorded and the
    batch continues; cancellation aborts immediately.
    """
    started = time.monotonic()
    validate_connection(
        host=host,
        username=username,
        port=port,
        auth_method=auth_method,
        password=password,
        private_key_path=private_key_path,
    )
    source_root = Path(local_path).expanduser()
    if not source_root.is_dir():
        raise ValueError("本地文件夹不存在或不是目录")
    if not remote_dir.strip():
        raise ValueError("远端目录不能为空")

    cancel_check = coerce_cancel(cancel)
    scan = scan_directory(source_root, max_files=max_files)
    remote_root = remote_dir.rstrip("/") or "/"
    total = scan.total_size
    files_total = scan.total_files

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

    results: list[TreeFileResult] = []
    skipped = list(scan.skipped)
    highest = 0
    ok_count = 0
    try:
        ensure_remote_dir(sftp, remote_root)
        ensured = {remote_root}
        for rel_dir in scan.empty_dirs:
            target_dir = posixpath.join(remote_root, rel_dir)
            if target_dir in ensured:
                continue
            try:
                ensure_remote_dir(sftp, target_dir)
                ensured.add(target_dir)
            except (TransferError, OSError, paramiko.SSHException) as exc:
                skipped.append({"path": rel_dir, "reason": f"无法创建目录: {exc}"})

        sent_base = 0
        for index, item in enumerate(scan.files, start=1):
            if cancel_check():
                raise TransferCancelled("传输已取消，已保留远端临时文件")
            target_dir = (
                posixpath.join(remote_root, item.rel_dir) if item.rel_dir else remote_root
            )
            if target_dir not in ensured:
                ensure_remote_dir(sftp, target_dir)
                ensured.add(target_dir)
            if file_progress:
                file_progress(item.rel_path, index - 1, files_total)

            def report(sent_in_file: int, _file_total: int, base: int = sent_base) -> None:
                nonlocal highest
                if progress is None:
                    return
                value = base + sent_in_file
                if value > highest:
                    highest = value
                progress(highest, total)

            def report_verify(
                done_in_file: int, _file_total: int, base: int = sent_base
            ) -> None:
                if verify_progress:
                    verify_progress(min(base + done_in_file, total), total)

            try:
                uploaded = upload_one(
                    sftp,
                    item.path,
                    target_dir,
                    progress=report,
                    cancel_check=cancel_check,
                    overwrite=overwrite,
                    verify_progress=report_verify if verify_progress else None,
                )
            except TransferCancelled:
                raise
            except (TransferError, ValueError) as exc:
                results.append(
                    TreeFileResult(
                        rel_path=item.rel_path,
                        remote_path=posixpath.join(
                            target_dir.rstrip("/") or "/", item.path.name
                        ),
                        size=item.size,
                        ok=False,
                        error=str(exc),
                        error_code=getattr(exc, "code", "transfer_failed"),
                    )
                )
            else:
                ok_count += 1
                sent_base += item.size
                results.append(
                    TreeFileResult(
                        rel_path=item.rel_path,
                        remote_path=uploaded.remote_path,
                        size=item.size,
                        ok=True,
                    )
                )
            if file_progress:
                file_progress(item.rel_path, index, files_total)
    except (TransferError, ValueError):
        raise
    except (OSError, paramiko.SSHException) as exc:
        raise classify_connection_error(exc) from exc
    finally:
        sftp.close()
        client.close()

    logger.info(
        "文件夹传输结束 %s：成功 %s / 失败 %s / 跳过 %s，共 %s 字节，耗时 %.1fs",
        scan.root,
        ok_count,
        len(results) - ok_count,
        len(skipped),
        total,
        time.monotonic() - started,
    )
    return TreeResult(
        local_root=scan.root,
        remote_root=remote_root,
        files=tuple(results),
        ok_count=ok_count,
        failed_count=len(results) - ok_count,
        skipped=tuple(skipped),
        size=total,
    )
