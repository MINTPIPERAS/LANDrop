"""Local filesystem browsing for the custom file/folder picker."""

from __future__ import annotations

import ctypes
import os
import stat as stat_module
from pathlib import Path
from typing import Any

DEFAULT_LIMIT = 20000
_HIDDEN_ATTRIBUTES = getattr(stat_module, "FILE_ATTRIBUTE_HIDDEN", 0) | getattr(
    stat_module, "FILE_ATTRIBUTE_SYSTEM", 0
)


def _normalize_path(path: str) -> str:
    text = (path or "").strip()
    if not text:
        raise ValueError("目录路径不能为空")
    if any(ord(character) < 32 for character in text):
        raise ValueError("目录路径不能包含控制字符")
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        raise ValueError("目录路径必须是绝对路径")
    return os.path.abspath(expanded)


def _entry_times(stats: os.stat_result) -> tuple[int, int]:
    modified = int(stats.st_mtime)
    created = int(getattr(stats, "st_birthtime", None) or stats.st_ctime)
    return modified, created


def _is_hidden(name: str, stats: os.stat_result) -> bool:
    if os.name == "nt":
        return bool(getattr(stats, "st_file_attributes", 0) & _HIDDEN_ATTRIBUTES)
    return name.startswith(".")


def list_local_directory(path: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Return one directory's direct entries with metadata for the picker."""
    root = _normalize_path(path)
    bounded = max(1, min(int(limit), DEFAULT_LIMIT))
    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    total = 0
    truncated = False

    try:
        scanner = os.scandir(root)
    except FileNotFoundError as exc:
        raise ValueError("目录不存在") from exc
    except NotADirectoryError as exc:
        raise ValueError("选择的路径不是文件夹") from exc
    except PermissionError as exc:
        raise ValueError("没有权限读取该目录") from exc
    except OSError as exc:
        raise ValueError(f"无法读取目录: {exc.strerror or exc}") from exc

    try:
        with scanner:
            for entry in scanner:
                total += 1
                if len(entries) >= bounded:
                    truncated = True
                    continue
                try:
                    stats = entry.stat(follow_symlinks=False)
                    is_dir = entry.is_dir(follow_symlinks=True)
                    is_symlink = entry.is_symlink()
                except OSError as exc:
                    skipped.append(
                        {
                            "path": entry.path,
                            "reason": f"无法读取: {exc.strerror or exc}",
                        }
                    )
                    continue
                modified, created = _entry_times(stats)
                entries.append(
                    {
                        "name": entry.name,
                        "path": os.path.abspath(entry.path),
                        "is_dir": is_dir,
                        "is_symlink": is_symlink,
                        "hidden": _is_hidden(entry.name, stats),
                        "size": None if is_dir else int(stats.st_size),
                        "modified": modified,
                        "created": created,
                    }
                )
    except OSError as exc:
        raise ValueError(f"无法读取目录: {exc.strerror or exc}") from exc

    entries.sort(key=lambda item: item["name"].casefold())
    parent = os.path.dirname(root)
    if os.path.normcase(parent) == os.path.normcase(root):
        parent = None
    return {
        "path": root,
        "parent": parent,
        "entries": entries,
        "total": total,
        "truncated": truncated,
        "skipped": skipped,
    }


def _windows_drives() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    drives: list[dict[str, str]] = []
    if hasattr(os, "listdrives"):
        try:
            for path in os.listdrives():
                label = path.rstrip("\\/") or path
                drives.append({"label": label, "path": path, "kind": "drive"})
            return drives
        except OSError:
            drives.clear()
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return drives
    for index in range(26):
        if mask & (1 << index):
            letter = chr(ord("A") + index)
            drives.append({"label": f"{letter}:", "path": f"{letter}:\\", "kind": "drive"})
    return drives


def list_local_roots() -> dict[str, Any]:
    """Return home/desktop/downloads and drive roots for the picker."""
    home = Path.home()
    roots: list[dict[str, str]] = [
        {"label": "主目录", "path": str(home), "kind": "home"}
    ]
    candidates = [
        ("桌面", home / "Desktop", "desktop"),
        ("下载", home / "Downloads", "downloads"),
        ("OneDrive 桌面", home / "OneDrive" / "Desktop", "desktop"),
        ("OneDrive 下载", home / "OneDrive" / "Downloads", "downloads"),
    ]
    for label, candidate, kind in candidates:
        try:
            if candidate.is_dir():
                roots.append({"label": label, "path": str(candidate), "kind": kind})
        except OSError:
            continue
    roots.extend(_windows_drives())
    if os.name != "nt":
        roots.append({"label": "根目录", "path": "/", "kind": "root"})

    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for root in roots:
        key = os.path.normcase(root["path"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return {"roots": unique}
