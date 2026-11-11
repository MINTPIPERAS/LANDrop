"""Directory uploads: structure, empty dirs, aggregate progress, failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from landrop.transfer import TransferCancelled
from landrop.transfer.tree import upload_tree

from fake_sftp import FakeSFTP, FakeSSHClient


def make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "images" / "raw").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "notes.txt").write_bytes(b"hello")
    (root / "images" / "a.png").write_bytes(b"x" * 10)
    (root / "images" / "raw" / "b.png").write_bytes(b"y" * 20)
    return root


def run_tree(root: Path, remote_dir: str = "/out", sftp: FakeSFTP | None = None, **kwargs):
    sftp = sftp or FakeSFTP()
    client = FakeSSHClient(sftp)
    result = upload_tree(
        host="host",
        username="alice",
        local_path=root,
        remote_dir=remote_dir,
        client_factory=lambda: client,
        **kwargs,
    )
    return result, sftp, client


def test_uploads_every_file_into_its_relative_directory(tmp_path) -> None:
    result, sftp, client = run_tree(make_tree(tmp_path))

    assert set(sftp.files) == {
        "/out/notes.txt",
        "/out/images/a.png",
        "/out/images/raw/b.png",
    }
    assert result.ok_count == 3
    assert result.failed_count == 0
    assert result.size == 35
    assert {item.rel_path for item in result.files} == {
        "notes.txt",
        "images/a.png",
        "images/raw/b.png",
    }
    assert sftp.closed and client.closed


def test_creates_the_remote_root_and_empty_directories(tmp_path) -> None:
    _, sftp, _ = run_tree(make_tree(tmp_path))

    assert "/out" in sftp.dirs
    assert "/out/empty" in sftp.dirs
    assert "/out/images/raw" in sftp.dirs


def test_aggregate_progress_covers_the_whole_tree(tmp_path) -> None:
    updates: list[tuple[int, int]] = []
    run_tree(make_tree(tmp_path), progress=lambda sent, total: updates.append((sent, total)))

    assert updates
    assert all(total == 35 for _, total in updates)
    assert updates[-1] == (35, 35)
    # Aggregate progress must never walk backwards.
    assert all(later >= earlier for earlier, later in zip(updates, updates[1:]))


def test_reports_the_current_file_and_file_counts(tmp_path) -> None:
    seen: list[tuple[str, int, int]] = []
    run_tree(
        make_tree(tmp_path),
        file_progress=lambda rel, done, total: seen.append((rel, done, total)),
    )

    assert seen[-1][1:] == (3, 3)
    assert {item[0] for item in seen} == {"notes.txt", "images/a.png", "images/raw/b.png"}


def test_a_failing_file_does_not_stop_the_batch(tmp_path) -> None:
    sftp = FakeSFTP({"/out/images/a.png": b"already here"})
    result, sftp, _ = run_tree(make_tree(tmp_path), sftp=sftp)

    assert result.failed_count == 1
    assert result.ok_count == 2
    failed = next(item for item in result.files if not item.ok)
    assert failed.rel_path == "images/a.png"
    assert failed.error_code == "target_exists"
    # The remaining files were still sent.
    assert sftp.files["/out/images/raw/b.png"] == b"y" * 20
    assert sftp.files["/out/notes.txt"] == b"hello"


def test_cancellation_stops_between_files(tmp_path) -> None:
    state = {"done": 0}

    def file_progress(_rel: str, done: int, _total: int) -> None:
        if done >= 1:
            state["done"] = done

    with pytest.raises(TransferCancelled):
        run_tree(
            make_tree(tmp_path),
            file_progress=file_progress,
            cancel=lambda: state["done"] >= 1,
        )


def test_overwrite_replaces_existing_files(tmp_path) -> None:
    sftp = FakeSFTP({"/out/notes.txt": b"old"})
    result, sftp, _ = run_tree(make_tree(tmp_path), sftp=sftp, overwrite=True)

    assert result.failed_count == 0
    assert sftp.files["/out/notes.txt"] == b"hello"


def test_empty_folder_still_creates_the_remote_root(tmp_path) -> None:
    root = tmp_path / "blank"
    (root / "nested" / "deeper").mkdir(parents=True)

    result, sftp, _ = run_tree(root)

    assert result.ok_count == 0
    assert result.size == 0
    assert "/out" in sftp.dirs
    assert "/out/nested" in sftp.dirs
    assert "/out/nested/deeper" in sftp.dirs


def test_aggregate_verify_progress_is_monotonic(tmp_path) -> None:
    updates: list[tuple[int, int]] = []
    run_tree(
        make_tree(tmp_path),
        verify_progress=lambda done, total: updates.append((done, total)),
    )

    assert updates
    assert all(total == 35 for _, total in updates)
    assert updates[-1] == (35, 35)
    values = [done for done, _ in updates]
    assert values == sorted(values)
