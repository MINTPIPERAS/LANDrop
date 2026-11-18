"""Remote folder naming: defaults, date tokens, and path-safety validation."""

from __future__ import annotations

from datetime import date

import pytest

from irecon.transfer.naming import (
    default_remote_name,
    resolve_remote_name,
    validate_remote_name,
)

FIXED_DAY = date(2026, 11, 17)


def test_default_name_uses_date_and_local_folder() -> None:
    assert default_remote_name("C:/work/project", today=FIXED_DAY) == "2026-11-17_project"
    assert default_remote_name("C:/work/project/", today=FIXED_DAY) == "2026-11-17_project"


def test_default_name_handles_a_drive_root() -> None:
    assert default_remote_name("C:/", today=FIXED_DAY) == "2026-11-17_folder"


def test_resolve_replaces_the_date_token() -> None:
    assert (
        resolve_remote_name("{date}_backup", "C:/work/project", today=FIXED_DAY)
        == "2026-11-17_backup"
    )


def test_resolve_empty_uses_the_default() -> None:
    assert resolve_remote_name(None, "C:/work/project", today=FIXED_DAY) == "2026-11-17_project"
    assert resolve_remote_name("   ", "C:/work/project", today=FIXED_DAY) == "2026-11-17_project"


def test_validate_accepts_common_names() -> None:
    assert validate_remote_name("2026-11-17 项目备份 v1.2") == "2026-11-17 项目备份 v1.2"
    assert validate_remote_name("  trimmed  ") == "trimmed"


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "a/b",
        "a\\b",
        "a:b",
        "a*b",
        'a"b',
        "a<b",
        "a>b",
        "a|b",
        "a?b",
        "evil.",
        "CON",
        "con.txt",
        "LPT1.old",
    ],
)
def test_validate_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_remote_name(name)


def test_validate_rejects_control_characters() -> None:
    with pytest.raises(ValueError):
        validate_remote_name("a\x01b")


def test_validate_rejects_overlong_names() -> None:
    with pytest.raises(ValueError):
        validate_remote_name("a" * 256)


def test_resolved_template_is_validated_too() -> None:
    with pytest.raises(ValueError):
        resolve_remote_name("{date}/backup", "C:/work/project", today=FIXED_DAY)
