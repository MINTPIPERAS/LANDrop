"""Directory scans: relative paths, empty dirs, symlink policy and limits."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from landrop.transfer.scan import scan_directory


def make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "images" / "raw").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "notes.txt").write_bytes(b"hello")
    (root / "images" / "a.png").write_bytes(b"x" * 10)
    (root / "images" / "raw" / "b.png").write_bytes(b"y" * 20)
    return root


def test_scan_returns_sorted_relative_paths(tmp_path) -> None:
    result = scan_directory(make_tree(tmp_path))

    assert [item.rel_path for item in result.files] == [
        "images/a.png",
        "images/raw/b.png",
        "notes.txt",
    ]
    assert [item.rel_dir for item in result.files] == ["images", "images/raw", ""]
    assert result.total_files == 3
    assert result.total_size == 5 + 10 + 20


def test_scan_reports_directories_that_stay_empty(tmp_path) -> None:
    result = scan_directory(make_tree(tmp_path))

    # images/ and images/raw/ receive files, the "empty" folder does not.
    assert result.empty_dirs == ("empty",)


def test_scan_treats_a_completely_empty_folder(tmp_path) -> None:
    root = tmp_path / "nothing"
    (root / "a" / "b").mkdir(parents=True)

    result = scan_directory(root)

    assert result.files == ()
    assert result.empty_dirs == ("a", "a/b")
    assert result.total_size == 0


def test_scan_rejects_a_regular_file(tmp_path) -> None:
    path = tmp_path / "one.bin"
    path.write_bytes(b"x")

    with pytest.raises(ValueError, match="不是文件夹"):
        scan_directory(path)


def test_scan_rejects_a_missing_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="不是文件夹"):
        scan_directory(tmp_path / "missing")


def test_scan_enforces_the_file_limit(tmp_path) -> None:
    root = tmp_path / "many"
    root.mkdir()
    for index in range(3):
        (root / f"{index}.txt").write_bytes(b"x")

    with pytest.raises(ValueError, match="上限"):
        scan_directory(root, max_files=2)


def test_scan_skips_symlinks_instead_of_following_them(tmp_path) -> None:
    root = make_tree(tmp_path)
    link = root / "link.txt"
    try:
        os.symlink(root / "notes.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    result = scan_directory(root)

    assert all(item.rel_path != "link.txt" for item in result.files)
    assert any("软链接" in item["reason"] for item in result.skipped)
