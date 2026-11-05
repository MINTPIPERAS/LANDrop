"""An in-process SSH/SFTP server for integration tests.

Paramiko can act as a server, which lets the transfer core be exercised over a
real socket: real host key verification, real channel writes, real rename.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import paramiko


class _Server(paramiko.ServerInterface):
    def __init__(self, username: str, password: str, root: Path) -> None:
        self.username = username
        self.password = password
        self.root = root

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_subsystem_request(self, channel, name):  # type: ignore[no-untyped-def]
        if name != "sftp":
            return False
        handler = paramiko.SFTPServer(
            channel,
            name,
            self,
            sftp_si=_make_interface(self.root),
        )
        handler.start()
        return True


def _make_interface(root: Path):
    class _SFTPHandle(paramiko.SFTPServerInterface):
        """Map remote paths onto a temporary local directory."""

        def _path(self, path: str) -> Path:
            cleaned = path.replace("\\", "/").lstrip("/")
            target = (root / cleaned).resolve()
            if not str(target).startswith(str(root.resolve())):
                raise paramiko.SFTPError(paramiko.SFTP_PERMISSION_DENIED, "outside root")
            return target

        def list_folder(self, path):  # type: ignore[no-untyped-def]
            target = self._path(path)
            try:
                return [
                    paramiko.SFTPAttributes.from_stat(entry.stat())
                    for entry in sorted(target.iterdir())
                ]
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def stat(self, path):  # type: ignore[no-untyped-def]
            try:
                return paramiko.SFTPAttributes.from_stat(self._path(path).stat())
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        lstat = stat

        def open(self, path, flags, attr):  # type: ignore[no-untyped-def]
            target = self._path(path)
            if flags & os.O_WRONLY and flags & os.O_APPEND:
                mode = "ab"
            elif flags & os.O_WRONLY:
                mode = "wb"
            elif flags & os.O_RDWR:
                mode = "r+b"
            else:
                mode = "rb"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = target.open(mode)
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)
            return _FileHandle(handle)

        def remove(self, path):  # type: ignore[no-untyped-def]
            try:
                self._path(path).unlink()
                return paramiko.SFTP_OK
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def rename(self, oldpath, newpath):  # type: ignore[no-untyped-def]
            target = self._path(newpath)
            if target.exists():
                return paramiko.SFTP_FAILURE
            try:
                self._path(oldpath).rename(target)
                return paramiko.SFTP_OK
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def mkdir(self, path, attr):  # type: ignore[no-untyped-def]
            try:
                self._path(path).mkdir(parents=True, exist_ok=True)
                return paramiko.SFTP_OK
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

    return _SFTPHandle


class _FileHandle(paramiko.SFTPHandle):
    def __init__(self, fileobj) -> None:  # type: ignore[no-untyped-def]
        super().__init__(flags=0)
        self.readfile = fileobj
        self.writefile = fileobj

    def close(self) -> None:
        try:
            self.readfile.close()
        finally:
            super().close()


class LocalSftpServer:
    """A password-protected SFTP endpoint bound to loopback."""

    def __init__(self, root: Path, username: str = "lin", password: str = "secret") -> None:
        self.root = root
        self.username = username
        self.password = password
        self.host_key = paramiko.RSAKey.generate(2048)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> LocalSftpServer:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        transport = None
        try:
            transport = paramiko.Transport(client)
            transport.add_server_key(self.host_key)
            transport.start_server(server=_Server(self.username, self.password, self.root))
            while transport.is_active() and not self._stop.is_set():
                channel = transport.accept(timeout=0.5)
                if channel is not None:
                    # SFTPServer owns the channel now; keep accepting others.
                    threading.Thread(
                        target=self._wait_channel, args=(channel,), daemon=True
                    ).start()
        except Exception:
            pass
        finally:
            if transport is not None:
                transport.close()

    def _wait_channel(self, channel: paramiko.Channel) -> None:
        try:
            channel.join()
        except Exception:
            pass

    def write_known_hosts(self, path: Path) -> Path:
        """Store this server's key the way the app does after user confirmation."""
        path.parent.mkdir(parents=True, exist_ok=True)
        keys = paramiko.HostKeys()
        keys.add(f"[127.0.0.1]:{self.port}", self.host_key.get_name(), self.host_key)
        keys.save(str(path))
        return path
