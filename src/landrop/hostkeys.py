"""Host key probing and trust-on-first-use storage.

The transfer core rejects unknown hosts, so provisioning a new server key is an
explicit user action: probe the fingerprint, show it, then append it to this
application's own known_hosts file.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import paramiko

DEFAULT_TIMEOUT = 15.0


class HostKeyError(RuntimeError):
    """The remote host key could not be read or stored."""


@dataclass(frozen=True, slots=True)
class HostKeyInfo:
    host: str
    port: int
    key_type: str
    fingerprint: str
    trusted: bool


def known_hosts_path() -> Path:
    """Return the app-owned known_hosts file (never the user's ~/.ssh file)."""
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "LANDrop" / "known_hosts"
    return Path.home() / ".config" / "landrop" / "known_hosts"


def _host_entry(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class _CapturePolicy(paramiko.MissingHostKeyPolicy):
    """Record the offered key instead of trusting it."""

    def __init__(self) -> None:
        self.key: paramiko.PKey | None = None

    def missing_host_key(self, client, hostname, key) -> None:  # type: ignore[no-untyped-def]
        self.key = key
        raise HostKeyError("已获取主机密钥，等待用户确认")


def _load_keys(path: Path) -> paramiko.HostKeys:
    keys = paramiko.HostKeys()
    if path.is_file():
        try:
            keys.load(str(path))
        except (OSError, paramiko.SSHException) as exc:
            raise HostKeyError(f"known_hosts 文件无法读取: {exc}") from exc
    return keys


def is_trusted(host: str, port: int, key: paramiko.PKey, path: Path | None = None) -> bool:
    keys = _load_keys(path or known_hosts_path())
    entry = keys.lookup(_host_entry(host, port))
    if not entry:
        return False
    return any(stored.asbytes() == key.asbytes() for stored in entry.values())


def _fetch_key(host: str, port: int, timeout: float) -> paramiko.PKey:
    """Connect far enough to read the server key, without authenticating."""
    if not host.strip():
        raise ValueError("远端主机地址不能为空")
    if not 1 <= port <= 65535:
        raise ValueError("SSH 端口必须在 1 到 65535 之间")

    policy = _CapturePolicy()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(policy)
    try:
        client.connect(
            hostname=host,
            port=port,
            username="landrop-probe",
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
    except HostKeyError:
        pass
    except (OSError, paramiko.SSHException) as exc:
        raise HostKeyError(f"无法读取远端主机密钥: {exc}") from exc
    finally:
        client.close()

    if policy.key is None:
        raise HostKeyError("远端未提供主机密钥")
    return policy.key


def probe(host: str, port: int = 22, timeout: float = DEFAULT_TIMEOUT) -> HostKeyInfo:
    key = _fetch_key(host, port, timeout)
    return HostKeyInfo(
        host=host,
        port=port,
        key_type=key.get_name(),
        fingerprint=fingerprint(key),
        trusted=is_trusted(host, port, key),
    )


def trust(host: str, port: int, expected_fingerprint: str,
          timeout: float = DEFAULT_TIMEOUT) -> HostKeyInfo:
    """Store the key only if the fingerprint still matches what the user saw."""
    key = _fetch_key(host, port, timeout)
    actual = fingerprint(key)
    if actual != expected_fingerprint:
        raise HostKeyError("主机指纹已变化，已拒绝保存；请重新确认服务器身份")
    if is_trusted(host, port, key):
        return HostKeyInfo(host, port, key.get_name(), actual, True)

    path = known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = _load_keys(path)
    keys.add(_host_entry(host, port), key.get_name(), key)
    try:
        keys.save(str(path))
    except OSError as exc:
        raise HostKeyError(f"无法保存 known_hosts: {exc}") from exc
    return HostKeyInfo(host, port, key.get_name(), actual, True)
