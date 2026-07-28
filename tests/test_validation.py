"""Backup rotation for files OpenVidia edits — its own and other tools'."""

from __future__ import annotations

import os
import sys

import pytest

from openvidia.safe_file import cleanup_old_backups, create_backup


def backups_of(path):
    return sorted(path.parent.glob(f"{path.stem}_backup_*{path.suffix}"))


def test_create_backup_copies_the_content(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("original content")

    backup = create_backup(f)

    assert backup is not None and backup.exists()
    assert backup.read_text() == "original content"


def test_backup_of_a_missing_file_is_none(tmp_path):
    assert create_backup(tmp_path / "does_not_exist.txt") is None


def test_every_backup_gets_its_own_file(tmp_path):
    """Second-granularity timestamps collided, so rapid backups overwrote each
    other and only one survived — while the rotation reported success."""
    f = tmp_path / "test.txt"
    f.write_text("v0")

    for i in range(4):
        f.write_text(f"v{i}")
        create_backup(f, max_backups=10)

    assert len(backups_of(f)) == 4


def test_rotation_keeps_the_most_recent_n(tmp_path):
    f = tmp_path / "test.txt"
    contents = []
    for i in range(7):
        f.write_text(f"content {i}")
        contents.append(f"content {i}")
        create_backup(f, max_backups=5)

    kept = backups_of(f)
    assert len(kept) == 5
    # The five that survive are the five newest, so the oldest content is gone.
    kept_contents = {p.read_text() for p in kept}
    assert contents[-1] in kept_contents
    assert contents[0] not in kept_contents


def test_cleanup_is_callable_on_its_own(tmp_path):
    f = tmp_path / "test.txt"
    for i in range(6):
        f.write_text(f"c{i}")
        create_backup(f, max_backups=99)

    removed = cleanup_old_backups(f, max_backups=2)

    assert removed == 4
    assert len(backups_of(f)) == 2


def test_cleanup_on_a_file_with_no_backups(tmp_path):
    f = tmp_path / "lonely.txt"
    f.write_text("x")
    assert cleanup_old_backups(f, max_backups=5) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_backups_are_not_world_readable(tmp_path):
    """A backup of a key file is still a key file."""
    f = tmp_path / "keys.json"
    f.write_text('["nvapi-secret"]')
    os.chmod(f, 0o644)  # as an older version would have left it

    backup = create_backup(f)

    assert backup.stat().st_mode & 0o077 == 0


def test_backup_does_not_match_unrelated_files(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("x")
    (tmp_path / "other_backup_20260101_000000_000000.txt").write_text("not mine")

    create_backup(f, max_backups=1)

    assert len(backups_of(f)) == 1
    assert (tmp_path / "other_backup_20260101_000000_000000.txt").exists()
