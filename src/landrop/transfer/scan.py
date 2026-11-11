"""Walk a local folder into the file list a directory transfer will send."""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_FILES = 50000


@dataclass(frozen=True, slots=True)
class ScannedFile:
    path: Path
    rel_path: str  # POSIX style, relative to the selected folder
    rel_dir: str  # POSIX style, "" for files directly in the root
    size: int


@dataclass(frozen=True, slots=True)
class ScanResult:
    root: Path
    files: tuple[ScannedFile, ...]
    empty_dirs: tuple[str, ...]
    skipped: tuple[dict[str, str], ...]
    total_size: int

    @property
    def total_files(self) -> int:
        return len(self.files)


def scan_directory(root: str | Path, max_files: int = DEFAULT_MAX_FILES) -> ScanResult:
    """Collect regular files under ``root`` without following symlinks.

    Symlinks and unreadable entries are reported through ``skipped`` instead of
    aborting the whole scan. Directories that would end up empty on the remote
    are returned separately so the uploader can still recreate the structure.
    """
    path = Path(root).expanduser()
    if not path.is_dir():
        raise ValueError("选择的路径不存在或不是文件夹")
    resolved = path.resolve()

    files: list[ScannedFile] = []
    directories: set[str] = set()
    skipped: list[dict[str, str]] = []

    for dirpath, dirnames, filenames in os.walk(resolved, topdown=True, followlinks=False):
        rel_dir = _relative_dir(resolved, dirpath)
        if rel_dir:
            directories.add(rel_dir)
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            child_rel = posixpath.join(rel_dir, name) if rel_dir else name
            if os.path.islink(full):
                dirnames.remove(name)
                skipped.append({"path": child_rel, "reason": "软链接目录已跳过"})
                continue
            directories.add(child_rel)
        for name in filenames:
            full = os.path.join(dirpath, name)
            child_rel = posixpath.join(rel_dir, name) if rel_dir else name
            if os.path.islink(full):
                skipped.append({"path": child_rel, "reason": "软链接文件已跳过"})
                continue
            try:
                size = os.path.getsize(full)
            except OSError as exc:
                skipped.append(
                    {"path": child_rel, "reason": f"无法读取: {exc.strerror or exc}"}
                )
                continue
            files.append(
                ScannedFile(path=Path(full), rel_path=child_rel, rel_dir=rel_dir, size=size)
            )
            if len(files) > max_files:
                raise ValueError(f"文件夹内文件数量超过上限 {max_files}，请分批发送")

    files.sort(key=lambda item: item.rel_path)
    needed: set[str] = set()
    for item in files:
        parent = item.rel_dir
        while parent:
            needed.add(parent)
            parent = posixpath.dirname(parent)

    return ScanResult(
        root=resolved,
        files=tuple(files),
        empty_dirs=tuple(sorted(directories - needed)),
        skipped=tuple(skipped),
        total_size=sum(item.size for item in files),
    )


def _relative_dir(root: Path, dirpath: str) -> str:
    relative = os.path.relpath(dirpath, root)
    if relative in (".", ""):
        return ""
    return relative.replace(os.sep, "/")
