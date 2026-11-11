"""Credential keys are scoped per host so devices cannot share a password."""

from __future__ import annotations

import pytest

from landrop import credentials


@pytest.fixture
def store(monkeypatch) -> dict[str, str]:
    """Replace the Windows credential backend with an in-memory mapping."""
    data: dict[str, str] = {}

    def fake_read(target: str) -> str | None:
        return data.get(target)

    def fake_write(target: str, username: str, password: str) -> None:
        data[target] = password

    def fake_delete(target: str) -> bool:
        return data.pop(target, None) is not None

    monkeypatch.setattr(credentials, "_read", fake_read)
    monkeypatch.setattr(credentials, "_write", fake_write)
    monkeypatch.setattr(credentials, "_delete", fake_delete)
    return data


def test_target_scopes_host_port_and_keeps_username_case() -> None:
    assert credentials._target("192.168.1.5", 22, "lin") == "LANDrop/192.168.1.5:22/lin"
    assert credentials._target(" NAS.LAN ", 2222, "Lin") == "LANDrop/nas.lan:2222/Lin"


def test_same_username_on_two_hosts_keeps_separate_passwords(store) -> None:
    credentials.save_password("192.168.1.5", 22, "lin", "first-secret")
    credentials.save_password("192.168.1.9", 22, "lin", "second-secret")

    assert credentials.load_password("192.168.1.5", 22, "lin") == "first-secret"
    assert credentials.load_password("192.168.1.9", 22, "lin") == "second-secret"
    assert len(store) == 2


def test_same_host_on_two_ports_stays_separate(store) -> None:
    credentials.save_password("nas.lan", 22, "lin", "port-22")
    credentials.save_password("nas.lan", 2222, "lin", "port-2222")

    assert credentials.load_password("nas.lan", 22, "lin") == "port-22"
    assert credentials.load_password("nas.lan", 2222, "lin") == "port-2222"


def test_legacy_entry_is_read_then_migrated(store) -> None:
    store["LANDrop/lin"] = "legacy-secret"

    assert credentials.load_password("192.168.1.5", 22, "lin") == "legacy-secret"
    assert store["LANDrop/192.168.1.5:22/lin"] == "legacy-secret"
    # The legacy entry stays readable for older builds.
    assert store["LANDrop/lin"] == "legacy-secret"


def test_host_scoped_entry_wins_over_legacy(store) -> None:
    store["LANDrop/lin"] = "legacy-secret"
    store["LANDrop/192.168.1.5:22/lin"] = "current-secret"

    assert credentials.load_password("192.168.1.5", 22, "lin") == "current-secret"


def test_legacy_migration_failure_still_returns_the_password(store, monkeypatch) -> None:
    store["LANDrop/lin"] = "legacy-secret"

    def failing_write(target: str, username: str, password: str) -> None:
        raise credentials.CredentialError("boom")

    monkeypatch.setattr(credentials, "_write", failing_write)
    assert credentials.load_password("192.168.1.5", 22, "lin") == "legacy-secret"


def test_delete_removes_legacy_copy_so_it_cannot_resurrect(store) -> None:
    store["LANDrop/lin"] = "legacy-secret"
    assert credentials.load_password("192.168.1.5", 22, "lin") == "legacy-secret"

    assert credentials.delete_password("192.168.1.5", 22, "lin") is True
    assert credentials.load_password("192.168.1.5", 22, "lin") is None
    assert credentials.has_password("192.168.1.5", 22, "lin") is False


def test_delete_only_touches_the_matching_host(store) -> None:
    credentials.save_password("192.168.1.5", 22, "lin", "first-secret")
    credentials.save_password("192.168.1.9", 22, "lin", "second-secret")

    assert credentials.delete_password("192.168.1.5", 22, "lin") is True
    assert credentials.load_password("192.168.1.5", 22, "lin") is None
    assert credentials.load_password("192.168.1.9", 22, "lin") == "second-secret"


def test_delete_reports_false_when_nothing_was_stored(store) -> None:
    assert credentials.delete_password("192.168.1.5", 22, "lin") is False


def test_invalid_identity_is_rejected(store) -> None:
    with pytest.raises(ValueError):
        credentials.load_password("", 22, "lin")
    with pytest.raises(ValueError):
        credentials.save_password("host", 0, "lin", "secret")
    with pytest.raises(ValueError):
        credentials.load_password("host", 22, "  ")
    with pytest.raises(ValueError):
        credentials.save_password("host", 22, "lin", "")
