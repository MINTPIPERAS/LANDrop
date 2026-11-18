"""Local directory browsing: metadata, limits, hidden files and roots."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

import pytest

from irecon import browse


def test_list_directory_returns_metadata(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "file.txt").write_bytes(b"hello")

    result = browse.list_local_directory(str(root))

    assert result["path"] == os.path.abspath(str(root))
    assert result["parent"] == os.path.abspath(str(root.parent))
    assert result["total"] == 2
    assert result["truncated"] is False
    entries = {entry["name"]: entry for entry in result["entries"]}
    assert entries["file.txt"]["is_dir"] is False
    assert entries["file.txt"]["size"] == 5
    assert isinstance(entries["file.txt"]["modified"], int)
    assert isinstance(entries["file.txt"]["created"], int)
    assert entries["sub"]["is_dir"] is True
    assert entries["sub"]["size"] is None


def test_list_directory_truncates_with_total_count(tmp_path) -> None:
    for index in range(5):
        (tmp_path / f"{index}.txt").write_text("x", encoding="utf-8")

    result = browse.list_local_directory(str(tmp_path), limit=2)

    assert len(result["entries"]) == 2
    assert result["truncated"] is True
    assert result["total"] == 5


@pytest.mark.parametrize("path", ["", "relative/path", "bad\x01path"])
def test_list_directory_rejects_invalid_paths(path: str) -> None:
    with pytest.raises(ValueError):
        browse.list_local_directory(path)


def test_list_directory_rejects_files_and_missing_paths(tmp_path) -> None:
    file_path = tmp_path / "one.txt"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="不是文件夹"):
        browse.list_local_directory(str(file_path))
    with pytest.raises(ValueError, match="不存在"):
        browse.list_local_directory(str(tmp_path / "missing"))


def test_hidden_attribute_is_reported(tmp_path) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific hidden attribute")
    hidden = tmp_path / "hidden.txt"
    hidden.write_text("x", encoding="utf-8")
    if not ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2):
        pytest.skip("无法设置隐藏属性")

    result = browse.list_local_directory(str(tmp_path))
    entry = next(item for item in result["entries"] if item["name"] == "hidden.txt")

    assert entry["hidden"] is True


def test_symlink_is_reported_without_following(tmp_path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建软链接")

    result = browse.list_local_directory(str(tmp_path))
    entry = next(item for item in result["entries"] if item["name"] == "link.txt")

    assert entry["is_symlink"] is True
    assert entry["is_dir"] is False


def test_roots_include_home_and_kinds() -> None:
    result = browse.list_local_roots()
    roots = result["roots"]

    assert any(item["path"] == str(Path.home()) for item in roots)
    assert all(item["kind"] for item in roots)
    assert len({item["path"].lower() for item in roots}) == len(roots)
