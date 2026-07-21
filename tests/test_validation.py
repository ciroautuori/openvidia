"""Tests for key validation and safe file operations."""

import tempfile
from pathlib import Path

import pytest

from openvidia.key_validation import (
    sanitize_keys_for_storage,
    validate_key_syntax,
    validate_keys_batch,
)
from openvidia.safe_file import (
    cleanup_old_backups,
    create_backup,
    list_backups,
    safe_write_with_backup,
)


class TestKeyValidation:
    """Test key syntax validation."""

    def test_valid_key(self):
        """Test valid NVIDIA API key."""
        key = "nvapi-1234567890abcdef1234567890abcdef"
        is_valid, reason = validate_key_syntax(key)
        assert is_valid
        assert reason == ""

    def test_empty_key(self):
        """Test empty key rejection."""
        is_valid, reason = validate_key_syntax("")
        assert not is_valid
        assert "empty" in reason.lower()

    def test_short_key(self):
        """Test too short key rejection."""
        is_valid, reason = validate_key_syntax("short")
        assert not is_valid
        assert "too short" in reason.lower()

    def test_placeholder_detection(self):
        """Test placeholder key detection."""
        placeholders = [
            "your_api_key_here_123456789012345",
            "xxx_placeholder_key_1234567890123",
            "replace_me_with_key_1234567890123",
        ]
        for ph in placeholders:
            is_valid, reason = validate_key_syntax(ph)
            assert not is_valid
            assert (
                "placeholder" in reason.lower()
                or "your_" in reason.lower()
                or "xxx" in reason.lower()
                or "replace" in reason.lower()
            )

    def test_invalid_characters(self):
        """Test invalid character detection."""
        is_valid, reason = validate_key_syntax("key@with#invalid!chars")
        assert not is_valid
        assert "invalid characters" in reason.lower()


class TestBatchValidation:
    """Test batch key validation."""

    def test_mixed_keys(self):
        """Test validation of mixed valid/invalid keys."""
        keys = [
            "validkey12345678901234567890",
            "short",
            "another_valid_key_1234567890",
            "your_placeholder_key",
        ]
        result = validate_keys_batch(keys)

        assert len(result["valid"]) == 2
        assert len(result["invalid"]) == 2
        assert "2 valid" in result["summary"]
        assert "2 invalid" in result["summary"]


class TestSanitizeKeys:
    """Test key sanitization."""

    def test_removes_duplicates(self):
        """Test duplicate removal."""
        keys = [
            "validkey123456789012345",
            "validkey223456789012345",
            "validkey123456789012345",
            "validkey323456789012345",
            "validkey223456789012345",
        ]
        cleaned = sanitize_keys_for_storage(keys)
        assert len(cleaned) == 3

    def test_strips_whitespace(self):
        """Test whitespace stripping."""
        keys = ["  key1  ", "key2\n", "\tkey3\t"]
        cleaned = sanitize_keys_for_storage(keys)
        assert all(k == k.strip() for k in cleaned)

    def test_filters_placeholders(self):
        """Test placeholder filtering."""
        keys = [
            "valid_key_12345678901234567890",
            "your_key_here",
            "another_valid_123456789012345",
        ]
        cleaned = sanitize_keys_for_storage(keys)
        assert len(cleaned) == 2


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
