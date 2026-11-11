"""SSH/SFTP session setup and remote directory helpers."""

from __future__ import annotations

import posixpath
import socket
from pathlib import Path
from typing import Callable

import paramiko

from landrop.transfer.errors import TransferError

# A stalled SFTP channel would otherwise block reads/writes forever, making the
# task impossible to cancel; 1 MiB per 120 s is far below any usable link speed.
IO_TIMEOUT = 120.0


def validate_connection(
    *,
    host: str,
    username: str,
    port: int,
    auth_method: str,
    password: str | None,
    private_key_path: str | Path | None,
) -> None:
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


def classify_connection_error(exc: BaseException) -> TransferError:
    """Map paramiko/socket failures onto the stable UI error codes."""
    if isinstance(exc, paramiko.PasswordRequiredException):
        return TransferError("私钥带有口令，LANDrop 不支持，请使用无口令私钥", code="auth_failed")
    if isinstance(exc, paramiko.AuthenticationException):
        return TransferError("认证失败：请检查用户名、密码或私钥", code="auth_failed")
    if isinstance(exc, paramiko.BadHostKeyException):
        return TransferError("远端主机密钥与已保存的不一致，请重新核对指纹", code="host_key_changed")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return TransferError("连接远端超时，请检查防火墙与网段", code="timeout")
    return TransferError(f"SFTP 传输失败: {exc}", code="network_error")


def connect_sftp(
    *,
    host: str,
    username: str,
    port: int = 22,
    auth_method: str = "key",
    password: str | None = None,
    private_key_path: str | Path | None = None,
    known_hosts_path: str | Path | None = None,
    connect_timeout: float = 15.0,
    client_factory: Callable[[], paramiko.SSHClient] | None = None,
) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
    """Open an authenticated SFTP session, rejecting unknown host keys.

    The caller owns both handles and must close them.
    """
    client = (client_factory or paramiko.SSHClient)()
    try:
        client.load_system_host_keys()
        if known_hosts_path is not None:
            known_hosts = Path(known_hosts_path).expanduser()
            if not known_hosts.is_file():
                raise TransferError(
                    f"known_hosts 文件不存在: {known_hosts}", code="host_key_changed"
                )
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
        try:
            channel = sftp.get_channel()
            if channel is not None:
                # Without this a dead link blocks the copy loop forever and the
                # cancel event is never observed.
                channel.settimeout(IO_TIMEOUT)
        except (AttributeError, OSError):
            pass
        return client, sftp
    except TransferError:
        client.close()
        raise
    except (OSError, paramiko.SSHException) as exc:
        client.close()
        raise classify_connection_error(exc) from exc
    except BaseException:
        client.close()
        raise


def _is_missing(exc: OSError) -> bool:
    return getattr(exc, "errno", None) == 2 or "no such file" in str(exc).lower()


def stat_or_none(sftp: paramiko.SFTPClient, path: str):
    """Return the stat result, or ``None`` when the path does not exist."""
    try:
        return sftp.stat(path)
    except OSError as exc:
        if _is_missing(exc):
            return None
        raise


def _candidate_dirs(path: str) -> list[str]:
    """Ancestors of ``path`` in creation order, excluding the root itself."""
    normalized = path.rstrip("/") or "/"
    if normalized == "/":
        return []
    absolute = normalized.startswith("/")
    parts = [part for part in normalized.split("/") if part]
    candidates: list[str] = []
    current = ""
    for part in parts:
        if absolute:
            current += "/" + part
        else:
            current = part if not current else posixpath.join(current, part)
        candidates.append(current)
    return candidates


def ensure_remote_dir(sftp: paramiko.SFTPClient, path: str) -> bool:
    """Create ``path`` (and missing parents). Returns True if anything was made."""
    if not path or path.strip() in {"", ".", "./", "/"}:
        return False
    if stat_or_none(sftp, path) is not None:
        return False

    created = False
    for candidate in _candidate_dirs(path.strip()):
        if stat_or_none(sftp, candidate) is not None:
            continue
        try:
            sftp.mkdir(candidate)
            created = True
        except OSError:
            # Another writer may have created it between stat and mkdir.
            if stat_or_none(sftp, candidate) is None:
                raise
    return created
