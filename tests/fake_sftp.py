"""In-memory SFTP doubles shared by the transfer tests."""

from __future__ import annotations

import errno
import io

import paramiko


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
        self.dirs: set[str] = set()
        self.opens = []
        self.closed = False
        self.posix_rename_supported = posix_rename_supported

    def stat(self, path):
        if path in self.dirs:
            return type("Stat", (), {"st_size": 0})()
        if path not in self.files:
            raise OSError(errno.ENOENT, "No such file", path)
        return type("Stat", (), {"st_size": len(self.files[path])})()

    def mkdir(self, path):
        self.dirs.add(path)

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
