"""Key validation: syntax, checksum, and liveness probing."""

from __future__ import annotations

import re
from typing import Optional


def validate_key_syntax(key: str) -> tuple[bool, str]:
    """Validate NVIDIA API key syntax.
    
    NVIDIA keys are typically 40-64 character hex/base64 strings.
    Returns (is_valid, error_message).
    """
    if not key or not isinstance(key, str):
        return False, "Key is empty or not a string"
    
    # Strip whitespace
    key = key.strip()
    
    if len(key) < 20:
        return False, f"Key too short ({len(key)} chars, min 20)"
    
    if len(key) > 128:
        return False, f"Key too long ({len(key)} chars, max 128)"
    
    # Check for valid characters (alphanumeric + common base64 chars)
    if not re.match(r'^[A-Za-z0-9_\-]+$', key):
        return False, "Key contains invalid characters"
    
    # Check for obvious patterns that indicate a placeholder
    placeholders = [
        "your_", "xxx", "placeholder", "replace", "insert", 
        "key_here"
    ]
    key_lower = key.lower()
    for ph in placeholders:
        if ph in key_lower:
            return False, f"Key appears to be a placeholder ({ph})"
    
    # nvapi- prefix is actually valid for NVIDIA keys
    if key.startswith("nvapi-") and len(key) > 20:
        return True, ""
    
    return True, ""


def validate_keys_batch(keys: list[str]) -> dict:
    """Validate multiple keys and return detailed report.
    
    Returns:
        {
            "valid": [...],      # List of valid keys
            "invalid": [...],    # List of (key, reason) tuples
            "summary": str       # Human-readable summary
        }
    """
    valid = []
    invalid = []
    
    for i, key in enumerate(keys):
        is_valid, reason = validate_key_syntax(key)
        if is_valid:
            valid.append(key)
        else:
            # Redact key for error message
            redacted = key[:5] + "..." if len(key) > 8 else "***"
            invalid.append((redacted, reason))
    
    summary = f"Validated {len(keys)} keys: {len(valid)} valid, {len(invalid)} invalid"
    
    return {
        "valid": valid,
        "invalid": invalid,
        "summary": summary,
    }


async def probe_key_liveness(
    client,
    key: str,
    upstream_base: str = "https://integrate.api.nvidia.com/v1/"
) -> tuple[bool, Optional[str]]:
    """Probe a key by making a lightweight API call.
    
    Returns (is_alive, error_reason).
    """
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "openvidia/1.0"}
    
    try:
        req = client.build_request("GET", upstream_base + "models", headers=headers)
        resp = await client.send(req)
        
        if resp.status_code == 200:
            await resp.aclose()
            return True, None
        elif resp.status_code == 401:
            await resp.aclose()
            return False, "Invalid key (401 Unauthorized)"
        elif resp.status_code == 403:
            await resp.aclose()
            return False, "Key forbidden (403 Forbidden)"
        elif resp.status_code == 429:
            # Key is valid but rate-limited
            retry_after = resp.headers.get("retry-after")
            await resp.aclose()
            return True, f"Rate limited (429), retry-after: {retry_after}"
        else:
            await resp.aread()
            await resp.aclose()
            return False, f"Unexpected status {resp.status_code}"
            
    except Exception as e:
        return False, f"Connection error: {type(e).__name__}: {e}"


def sanitize_keys_for_storage(keys: list[str]) -> list[str]:
    """Clean and normalize keys before saving.
    
    - Strips whitespace
    - Removes duplicates (preserving order)
    - Filters out obvious placeholders
    """
    seen = set()
    cleaned = []
    
    for key in keys:
        if not isinstance(key, str):
            continue
        
        key = key.strip()
        
        # Skip duplicates
        if key in seen:
            continue
        
        # Skip obvious placeholders
        is_valid, _ = validate_key_syntax(key)
        if not is_valid:
            continue
        
        seen.add(key)
        cleaned.append(key)
    
    return cleaned
