"""Tests for safe file operations."""

import tempfile
from pathlib import Path

import pytest

from openvidia.safe_file import (
    cleanup_old_backups,
    create_backup,
    list_backups,
    safe_write_with_backup,
)


class TestSafeFileBackup:
    """Test safe file operations with backups."""

    def test_create_backup(self):
        """Test backup creation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("original content")

            backup_path = create_backup(test_file)

            assert backup_path is not None
            assert backup_path.exists()
            assert backup_path.read_text() == "original content"

    def test_backup_nonexistent_file(self):
        """Test backup of nonexistent file returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "does_not_exist.txt"
            backup_path = create_backup(nonexistent)
            assert backup_path is None

    def test_cleanup_old_backups(self):
        """Test old backup cleanup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")

            # Create multiple backups
            for i in range(7):
                test_file.write_text(f"content {i}")
                create_backup(test_file, max_backups=5)

            # Should have at most 5 backups
            cleanup_old_backups(test_file, max_backups=5)
            backups = list(Path(tmpdir).glob("test_backup_*.txt"))
            assert len(backups) <= 5

    def test_safe_write_with_backup(self):
        """Test safe write creates backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.json"
            test_file.write_text("original")

            success = safe_write_with_backup(test_file, "new content", create_backup_flag=True)

            assert success
            assert test_file.read_text() == "new content"

            # Check backup was created
            backups = list(Path(tmpdir).glob("test_backup_*.json"))
            assert len(backups) >= 1

    def test_list_backups(self):
        """Test listing backups."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")

            # Create a backup
            create_backup(test_file)

            backups = list_backups(test_file)

            assert len(backups) >= 1
            assert "path" in backups[0]
            assert "timestamp" in backups[0]
            assert "size" in backups[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
