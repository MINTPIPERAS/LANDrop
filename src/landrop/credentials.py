"""Password storage through the Windows Credential Manager.

Only the Windows backend is implemented for the first release. Other platforms
raise :class:`CredentialError` instead of silently writing plaintext somewhere.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

SERVICE_PREFIX = "LANDrop"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


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


def _target(username: str) -> str:
    return f"{SERVICE_PREFIX}/{username}"


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


def save_password(username: str, password: str) -> None:
    if not username:
        raise ValueError("用户名不能为空")
    if not password:
        raise ValueError("密码不能为空")
    advapi32 = _api()
    blob = password.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    credential = _Credential(
        Flags=0,
        Type=CRED_TYPE_GENERIC,
        TargetName=_target(username),
        Comment="LANDrop SSH password",
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        Persist=CRED_PERSIST_LOCAL_MACHINE,
        UserName=username,
    )
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise CredentialError(f"无法保存密码，错误码 {ctypes.get_last_error()}")


def load_password(username: str) -> str | None:
    advapi32 = _api()
    pointer = _PCREDENTIAL()
    if not advapi32.CredReadW(_target(username), CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
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


def delete_password(username: str) -> bool:
    advapi32 = _api()
    if advapi32.CredDeleteW(_target(username), CRED_TYPE_GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == ERROR_NOT_FOUND:
        return False
    raise CredentialError(f"无法删除密码，错误码 {error}")


def has_password(username: str) -> bool:
    try:
        return load_password(username) is not None
    except CredentialError:
        return False
