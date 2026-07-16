"""
Auto-compaction: history never makes a request fail from context overflow.

The conversation history is summarized in-place when it exceeds the model's
budget. We keep a small rolling cache so each turn only summarizes newly
added messages, not the whole history. If the summarization call fails for
any reason (keys down, timeout, upstream 400), we fall back to deterministic
trimming so the request still goes through.
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable, Optional

from . import config

UPSTREAM = "https://integrate.api.nvidia.com/v1/chat/completions"

_DEFAULTS = {
    "enabled": True,
    "budget_tokens": 100_000,  # activation threshold (~4 chars/token)
    "keep_recent": 8,  # final messages kept verbatim
    "summary_max_tokens": 1024,  # cap on summarized output
}


def _settings() -> dict:
    try:
        p = config.config_dir() / "compaction.json"
        if p.exists():
            return {**_DEFAULTS, **json.loads(p.read_text())}
    except (json.JSONDecodeError, OSError):
        pass
    return dict(_DEFAULTS)


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: ~4 chars/token on the serialized JSON."""
    try:
        return len(json.dumps(messages, ensure_ascii=False)) // 4
    except (TypeError, ValueError):
        return sum(len(str(m)) for m in messages) // 4


def _split(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split into (leading system messages, the rest of the conversation)."""
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        i += 1
    return messages[:i], messages[i:]


def _render(messages: list[dict]) -> str:
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        c = m.get("content")
        if not isinstance(c, str):
            c = json.dumps(c, ensure_ascii=False)
        tcs = m.get("tool_calls")
        if tcs:
            names = ", ".join(t.get("function", {}).get("name", "") for t in tcs)
            c = (c or "") + f" [tool_calls: {names}]"
        lines.append(f"{role}: {c}")
    return "\n".join(lines)


_CONV_KEY_PREFIX_MSGS = (
    4  # initial messages of `rest` used for the conversation identity
)


def _conv_key(system_block: list[dict], rest: list[dict]) -> str:
    """
    Stable identity for a conversation: system prompt + first N messages.

    Using only rest[:1] (one message) collides across conversations sharing
    the same system prompt and first message — typical for automated tools.
    Using the first 4 instead keeps collisions vanishingly rare without
    changing the roll-forward logic (which keys on `covered_count`, not here).
    """
    h = hashlib.sha256()
    for m in system_block + rest[:_CONV_KEY_PREFIX_MSGS]:
        h.update(_render([m]).encode("utf-8", "replace"))
    h.update(str(min(len(rest), _CONV_KEY_PREFIX_MSGS)).encode())
    return h.hexdigest()


def _fingerprint(msgs: list[dict]) -> str:
    """Short fingerprint of a message sequence to guard against cache collisions."""
    h = hashlib.sha256()
    for m in msgs:
        h.update(_render([m]).encode("utf-8", "replace"))
    return h.hexdigest()[:16]


_rolling: dict[str, tuple[int, str, str]] = {}
_ROLLING_CAP = 256


def _pick_key(state) -> Optional[str]:
    for k in state.keys:
        if state.is_key_healthy(k) and state.key_can_send_rpm(k):
            return k
    return None


async def _summarize(
    client,
    key: str,
    model: str,
    prev_summary: Optional[str],
    new_msgs: list[dict],
    max_tokens: int,
) -> str:
    sys = (
        "You are a conversation history compressor. Produce a concise but dense "
        "summary that preserves decisions, facts, file modifications and their "
        "paths, the current state of the task, and important values/parameters. "
        "No preambles — return only the summary."
    )
    user = ""
    if prev_summary:
        user += "Previous summary:\n" + prev_summary + "\n\n"
    user += "New messages to integrate into the summary:\n" + _render(new_msgs)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
        "max_completion_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
    }
    hdrs = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "openvidia/2.0",
    }
    req = client.build_request("POST", UPSTREAM, json=payload, headers=hdrs)
    resp = await client.send(req)
    try:
        if resp.status_code != 200:
            raise RuntimeError(f"summarize HTTP {resp.status_code}")
        data = resp.json()
    finally:
        await resp.aclose()

    txt = ((data.get("choices") or [{}])[0].get("message", {}) or {}).get(
        "content"
    ) or ""
    if not txt.strip():
        raise RuntimeError("summarize empty")
    return txt.strip()


def _trim(
    system_block: list[dict], rest: list[dict], budget: int, keep_recent: int
) -> list[dict]:
    """Deterministic fallback: keep system + first msg + as many recent as fit."""
    head = rest[:1]
    total = estimate_tokens(system_block + head)
    tail: list[dict] = []
    for m in reversed(rest[1:]):
        t = estimate_tokens([m])
        if total + t > budget and len(tail) >= keep_recent:
            break
        tail.insert(0, m)
        total += t
    notice = {"role": "system", "content": "[previous messages omitted to fit context]"}
    return system_block + head + [notice] + tail


async def maybe_compact(
    messages: list[dict], *, state, client, log: Callable[[str], None]
) -> list[dict]:
    """
    Return compacted messages if needed, otherwise the original list
    (same reference — caller can skip re-serializing).
    """
    cfg = _settings()
    if not cfg["enabled"] or not isinstance(messages, list) or len(messages) < 4:
        return messages
    budget = cfg["budget_tokens"]
    if estimate_tokens(messages) <= budget:
        return messages

    keep_recent = cfg["keep_recent"]
    system_block, rest = _split(messages)
    if len(rest) <= keep_recent + 1:
        return messages

    old = rest[:-keep_recent]
    tail = rest[-keep_recent:]

    ck = _conv_key(system_block, rest)
    prev = _rolling.get(ck)
    fp_check_len = min(len(old), _CONV_KEY_PREFIX_MSGS)
    current_fp = _fingerprint(old[:fp_check_len])
    if prev and prev[0] <= len(old) and prev[2] == current_fp:
        covered, prev_summary, _fp = prev
        new_msgs = old[covered:]
        base = prev_summary
        if not new_msgs:
            block = {
                "role": "system",
                "content": "Previous conversation summary:\n" + prev_summary,
            }
            return system_block + [block] + tail
    else:
        if prev and prev[2] != current_fp:
            log(
                "⧉ compaction: cache collision detected (fingerprint mismatch) → summarize from scratch"
            )
        new_msgs = old
        base = None

    model = state.active_model or "deepseek-ai/deepseek-v4-pro"
    key = _pick_key(state)
    if key is not None:
        try:
            summary = await _summarize(
                client, key, model, base, new_msgs, cfg["summary_max_tokens"]
            )
            if len(_rolling) >= _ROLLING_CAP:
                _rolling.clear()
            _rolling[ck] = (len(old), summary, current_fp)
            log(
                f"⧉ compaction: summarized {len(old)} msg → {len(summary)} char (kept {len(tail)} recent)"
            )
            block = {
                "role": "system",
                "content": "Previous conversation summary:\n" + summary,
            }
            return system_block + [block] + tail
        except Exception as e:  # noqa: BLE001 — any failure → trim, never block
            log(f"⧉ compaction: summarize failed ({e}) → trim fallback")
    else:
        log("⧉ compaction: no healthy key → trim fallback")

    trimmed = _trim(system_block, rest, budget, keep_recent)
    log(f"⧉ compaction: trim → {len(trimmed)} msg (~{estimate_tokens(trimmed)} tok)")
    return trimmed
