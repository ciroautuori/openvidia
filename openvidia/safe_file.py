"""Safe file operations with automatic backups."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional


def create_backup(
    file_path: Path,
    backup_dir: Optional[Path] = None,
    max_backups: int = 5,
) -> Optional[Path]:
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
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{file_path.stem}_backup_{timestamp}{file_path.suffix}"
    backup_path = backup_dir / backup_name
    
    try:
        shutil.copy2(file_path, backup_path)
        
        # Cleanup old backups
        cleanup_old_backups(file_path, backup_dir, max_backups)
        
        return backup_path
    except (OSError, IOError) as e:
        print(f"Warning: Could not create backup: {e}")
        return None


def cleanup_old_backups(
    original_file: Path,
    backup_dir: Optional[Path] = None,
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


def safe_write_with_backup(
    file_path: Path,
    content: str,
    create_backup_flag: bool = True,
    max_backups: int = 5,
) -> bool:
    """Write content to file with optional automatic backup.
    
    Uses atomic write (temp file + rename) for crash safety.
    
    Args:
        file_path: Path to write to
        content: Content to write
        create_backup_flag: Whether to create a backup first
        max_backups: Maximum backups to keep
    
    Returns:
        True if successful, False otherwise
    """
    try:
        # Create backup if requested and file exists
        if create_backup_flag and file_path.exists():
            create_backup(file_path, max_backups=max_backups)
        
        # Atomic write
        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        tmp_path.write_text(content)
        tmp_path.rename(file_path)
        
        return True
    except (OSError, IOError) as e:
        print(f"Error writing file {file_path}: {e}")
        return False


def restore_from_backup(
    file_path: Path,
    backup_dir: Optional[Path] = None,
    use_latest: bool = True,
    backup_timestamp: Optional[str] = None,
) -> Optional[Path]:
    """Restore a file from backup.
    
    Args:
        file_path: Path to restore
        backup_dir: Directory containing backups
        use_latest: Use the most recent backup if True
        backup_timestamp: Specific timestamp to restore (YYYYMMDD_HHMMSS)
    
    Returns:
        Path to restored backup, or None if restoration failed
    """
    backup_dir = backup_dir or file_path.parent
    
    # Find all backups
    prefix = f"{file_path.stem}_backup_"
    suffix = file_path.suffix
    
    backups = []
    for f in backup_dir.iterdir():
        if f.is_file() and f.name.startswith(prefix) and f.name.endswith(suffix):
            backups.append(f)
    
    if not backups:
        return None
    
    # Select backup to restore
    if use_latest:
        backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        selected_backup = backups[0]
    elif backup_timestamp:
        target_name = f"{prefix}{backup_timestamp}{suffix}"
        selected_backup = backup_dir / target_name
        if not selected_backup.exists():
            return None
    else:
        return None
    
    # Restore
    try:
        shutil.copy2(selected_backup, file_path)
        return selected_backup
    except (OSError, IOError) as e:
        print(f"Error restoring from backup: {e}")
        return None


def list_backups(file_path: Path, backup_dir: Optional[Path] = None) -> list[dict]:
    """List all available backups for a file.
    
    Returns:
        List of dicts with backup info: path, timestamp, size
    """
    backup_dir = backup_dir or file_path.parent
    
    prefix = f"{file_path.stem}_backup_"
    suffix = file_path.suffix
    
    backups = []
    for f in backup_dir.iterdir():
        if f.is_file() and f.name.startswith(prefix) and f.name.endswith(suffix):
            stat = f.stat()
            backups.append({
                "path": str(f),
                "timestamp": f.name.replace(prefix, "").replace(suffix, ""),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    
    # Sort by timestamp (newest first)
    backups.sort(key=lambda b: b["timestamp"], reverse=True)
    
    return backups
