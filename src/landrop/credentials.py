"""Password storage through the Windows Credential Manager.

Only the Windows backend is implemented for the first release. Other platforms
raise :class:`CredentialError` instead of silently writing plaintext somewhere.

Entries are keyed by ``LANDrop/<host>:<port>/<username>`` so two machines that
share a user name never overwrite each other's password. v0.1 wrote
``LANDrop/<username>``; reads fall back to that legacy key once and re-save the
value under the host-scoped key.
"""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes

SERVICE_PREFIX = "LANDrop"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168

logger = logging.getLogger(__name__)


class CredentialError(RuntimeError):
    """A credential could not be read, written or removed."""


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIAL = ctypes.POINTER(_Credential)


def available() -> bool:
    return os.name == "nt"


def _normalize(host: str, port: int, username: str) -> tuple[str, int, str]:
    """Validate and canonicalize the credential identity.

    Hosts are case-insensitive, user names on the remote are not.
    """
    if not isinstance(host, str) or not host.strip():
        raise ValueError("主机地址不能为空")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("SSH 端口必须在 1 到 65535 之间")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("用户名不能为空")
    return host.strip().lower(), port, username.strip()


def _target(host: str, port: int, username: str) -> str:
    canonical_host, canonical_port, canonical_user = _normalize(host, port, username)
    return f"{SERVICE_PREFIX}/{canonical_host}:{canonical_port}/{canonical_user}"


def _legacy_target(username: str) -> str:
    """The host-less key written by v0.1, kept readable for migration."""
    return f"{SERVICE_PREFIX}/{username.strip()}"


def _api():
    if not available():
        raise CredentialError("当前系统不支持 Windows 凭据管理器")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_PCREDENTIAL),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return advapi32


def _write(target_name: str, username: str, password: str) -> None:
    advapi32 = _api()
    blob = password.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    credential = _Credential(
        Flags=0,
        Type=CRED_TYPE_GENERIC,
        TargetName=target_name,
        Comment="LANDrop SSH password",
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        Persist=CRED_PERSIST_LOCAL_MACHINE,
        UserName=username,
    )
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise CredentialError(f"无法保存密码，错误码 {ctypes.get_last_error()}")


def _read(target_name: str) -> str | None:
    advapi32 = _api()
    pointer = _PCREDENTIAL()
    if not advapi32.CredReadW(target_name, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise CredentialError(f"无法读取密码，错误码 {error}")
    try:
        credential = pointer.contents
        size = credential.CredentialBlobSize
        if not size:
            return ""
        raw = ctypes.string_at(credential.CredentialBlob, size)
        return raw.decode("utf-16-le")
    finally:
        advapi32.CredFree(pointer)


def _delete(target_name: str) -> bool:
    advapi32 = _api()
    if advapi32.CredDeleteW(target_name, CRED_TYPE_GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == ERROR_NOT_FOUND:
        return False
    raise CredentialError(f"无法删除密码，错误码 {error}")


def save_password(host: str, port: int, username: str, password: str) -> None:
    target = _target(host, port, username)
    if not password:
        raise ValueError("密码不能为空")
    _write(target, username.strip(), password)


def load_password(host: str, port: int, username: str) -> str | None:
    target = _target(host, port, username)
    value = _read(target)
    if value is not None:
        return value

    legacy = _read(_legacy_target(username))
    if legacy is None:
        return None
    try:
        _write(target, username.strip(), legacy)
    except (CredentialError, ValueError, OSError):
        # The read still succeeds; migration is retried on the next access.
        logger.warning("旧凭据迁移失败，本次仍使用旧条目")
    return legacy


def delete_password(host: str, port: int, username: str) -> bool:
    """Remove both the host-scoped entry and the v0.1 host-less one.

    The legacy entry has to go too, otherwise the fallback in
    :func:`load_password` would answer for a credential the user just deleted.
    """
    target = _target(host, port, username)
    removed = _delete(target)
    if _delete(_legacy_target(username)):
        removed = True
    return removed


def has_password(host: str, port: int, username: str) -> bool:
    try:
        return load_password(host, port, username) is not None
    except CredentialError:
        return False
