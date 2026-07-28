"""Safe file operations with automatic backups."""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path


def create_backup(
    file_path: Path,
    backup_dir: Path | None = None,
    max_backups: int = 5,
) -> Path | None:
    """Create a timestamped backup of a file.

    Args:
        file_path: Path to the file to backup
        backup_dir: Directory for backups (default: same dir as file)
        max_backups: Maximum number of backups to keep

    Returns:
        Path to the backup file, or None if backup failed
    """
    if not file_path.exists():
        return None

    backup_dir = backup_dir or file_path.parent
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Microseconds, not seconds. At second granularity two backups taken in the
    # same second resolved to the same filename, so the second silently
    # overwrote the first — the rotation kept one file where it claimed five,
    # and the test that was supposed to catch it passed for the same reason.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_name = f"{file_path.stem}_backup_{timestamp}{file_path.suffix}"
    backup_path = backup_dir / backup_name

    try:
        shutil.copy2(file_path, backup_path)
        # copy2 preserves the source mode, which is right for a key file — but
        # a backup taken from a file an older version wrote 0644 would inherit
        # 0644. Pin it: a backup of a secret is still a secret.
        if os.name != "nt":
            os.chmod(backup_path, 0o600)

        # Cleanup old backups
        cleanup_old_backups(file_path, backup_dir, max_backups)

        return backup_path
    except OSError as e:
        print(f"Warning: Could not create backup: {e}")
        return None


def cleanup_old_backups(
    original_file: Path,
    backup_dir: Path | None = None,
    max_backups: int = 5,
) -> int:
    """Remove old backups, keeping only the most recent ones.

    Returns:
        Number of backups removed
    """
    backup_dir = backup_dir or original_file.parent

    # Find all backups for this file
    prefix = f"{original_file.stem}_backup_"
    suffix = original_file.suffix

    backups = []
    for f in backup_dir.iterdir():
        if f.is_file() and f.name.startswith(prefix) and f.name.endswith(suffix):
            backups.append(f)

    # Sort by modification time (newest first)
    backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # Remove old backups
    removed = 0
    for old_backup in backups[max_backups:]:
        try:
            old_backup.unlink()
            removed += 1
        except OSError:
            pass

    return removed


# Removed with this commit: safe_write_with_backup, restore_from_backup and
# list_backups. None had a production caller — the test suite was their only
# consumer, which made a dead API look maintained. safe_write_with_backup also
# duplicated config.atomic_write with a *different* temp-file convention
# (keys.json.tmp vs keys.tmp) and without the 0600 mode or the fsync, so the
# two would have raced each other had anything used both. restore_from_backup
# built its path from an unvalidated timestamp, so a caller that ever exposed
# it over HTTP would have handed out arbitrary file overwrite.
