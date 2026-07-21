"""
Auto-compaction: history never makes a request fail from context overflow.

The conversation history is summarized in-place when it exceeds the model's
budget. We keep a small rolling cache so each turn only summarizes newly
added messages, not the whole history. If the summarization call fails for
any reason (keys down, timeout, upstream 400), we fall back to deterministic
trimming so the request still goes through.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Callable, Optional

import httpx

from . import config

UPSTREAM = "https://integrate.api.nvidia.com/v1/chat/completions"

_DEFAULTS = {
    "enabled": True,
    "budget_tokens": 100_000,  # default activation threshold (~4 chars/token)
    "keep_recent": 8,  # final messages kept verbatim
    "summary_max_tokens": 1024,  # cap on summarized output
    "reserved_tokens": 8000,  # generation headroom reserved from budget
    # Summarization NEVER uses the hot streamed model the client is hammering
    # (Codex/whatever); it uses a server-local "quiet" model so a compaction
    # request competes for keys on a DIFFERENT traffic stream, never the same.
    # Set to "" to fall back to state.active_model (legacy, NOT recommended).
    "summary_model": "deepseek-ai/deepseek-v4-pro",
    # Hard cap on summarize attempts: if the pool is saturated we must NOT
    # serially try all 25 keys with a 30s timeout on each (= 12 min block).
    # After this many rotate-attempts we bail to deterministic trim.
    "max_summarize_attempts": 5,  # increased from 3 to improve success rate
    # Per-attempt connect/read total cap (was unbounded read=30s on a hung
    # upstream, amplifying the block). 12s allows for slower summary models.
    "summarize_timeout": 12.0,  # increased from 8.0
    # When the pool health is below this fraction of live keys, skip the
    # summarize attempt entirely and go straight to deterministic trim —
    # there is no point burning 8s×3 on a pool that is mostly rate-limited.
    "min_healthy_fraction": 0.15,  # lowered from 0.25 to allow more attempts
    # Retry configuration for summarize failures
    "summarize_retry_delay": 0.5,  # seconds between retry attempts
    "summarize_max_retries": 2,  # additional retries on transient errors
}

# Per-model context budget override (tokens). NVIDIA NIM context windows vary:
# most NIM chat models expose 128k, but some (e.g. small/legacy) are 32k.
# Override here only when you know the real limit; otherwise the
# ``budget_tokens`` default applies.
_MODEL_BUDGETS = {
    "deepseek-ai/deepseek-v4-pro": 120_000,
    "meta-llama/llama-3.3-70b-instruct": 120_000,
    "nvidia/llama-3.1-nemotron-70b-instruct": 120_000,
    "qwen/qwen2.5-7b-instruct": 30_000,
    "mistralai/mixtral-8x7b-instruct-v0.1": 30_000,
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
    Stable identity for a conversation.

    Identity = full hash of system_block + first N rest messages + total rest
    length. Two conversations that share the same system prompt but diverge
    at message 2 (common with agentic CLIs: same system, different user
    turns) MUST hash differently, otherwise the rolling cache would swap
    summaries across sessions and contaminate context. We hash the prefix
    (cheap) plus the total length (forces a brand-new cache entry when the
    conversation grows past the previous cached covered_count, instead of
    silently reusing a stale summary).
    """
    h = hashlib.sha256()
    for m in system_block + rest[:_CONV_KEY_PREFIX_MSGS]:
        h.update(_render([m]).encode("utf-8", "replace"))
    h.update(str(len(rest)).encode())
    return h.hexdigest()


def _fingerprint(msgs: list[dict]) -> str:
    """Full SHA-256 of a message sequence to guard against cache collisions.

    A 16-char truncation (~64-bit) collides too often when many concurrent
    openvidia sessions share the same system prompt: rolling cache would reuse
    the wrong summary and contaminate the context. Use the full digest.
    """
    h = hashlib.sha256()
    for m in msgs:
        h.update(_render([m]).encode("utf-8", "replace"))
    return h.hexdigest()


_rolling: dict[str, tuple[int, str, str]] = {}
_ROLLING_CAP = 256


async def _summarize(
    client,
    keys: list[str],
    model: str,
    prev_summary: Optional[str],
    new_msgs: list[dict],
    max_tokens: int,
    log: Optional[Callable[[str], None]] = None,
    *,
    max_attempts: int = 5,
    attempt_timeout: float = 12.0,
    retry_delay: float = 0.5,
    max_retries: int = 2,
) -> str:
    """Summarize with rotation across the given keys + bounded attempts.

    A missing/empty summary must NEVER block the request. We rotate across
    at most ``max_attempts`` keys (NOT the whole pool — with 25 keys that
    previously meant 25×30s = 12 min of hard block while Codex waited). Each
    attempt is capped at ``attempt_timeout`` seconds total connect+read so a
    hung or rate-limited upstream falls back to deterministic trim quickly.
    
    Added detailed error logging for 400/404/500 errors and retry logic for
    transient failures.
    """
    
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
    last_err = ""
    # Pre-import the key-failure marker so we can record summarize-induced
    # 429s back into ProxyState (live state, not just local string) — this
    # stops the main proxy loop from re-selecting a key we just learned is
    # saturated via the compaction path.
    attempts = 0
    seen_429 = False
    error_details: list[str] = []  # Collect detailed error info for logging
    
    for key in keys:
        if attempts >= max_attempts:
            break
        attempts += 1
        
        # Retry loop for transient errors on same key
        retries_left = max_retries
        while retries_left >= 0:
            hdrs = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "openvidia/2.0",
            }
            try:
                req = client.build_request(
                    "POST",
                    UPSTREAM,
                    json=payload,
                    headers=hdrs,
                    extensions={
                        "timeout": {
                            "connect": 3.0,
                            "read": attempt_timeout,
                            "write": 3.0,
                            "pool": attempt_timeout,
                        }
                    },
                )
                resp = await client.send(req)
            except httpx.ConnectTimeout as e:
                err_msg = f"ConnectTimeout after 3s"
                error_details.append(f"key[{keys.index(key)}] {err_msg}")
                if log:
                    log(f"⧉ compaction: key[{keys.index(key)}] {err_msg} (attempt {attempts}/{max_attempts})")
                last_err = err_msg
                break  # Don't retry connection timeouts
            except httpx.ReadTimeout as e:
                err_msg = f"ReadTimeout after {attempt_timeout}s"
                error_details.append(f"key[{keys.index(key)}] {err_msg}")
                if log:
                    log(f"⧉ compaction: key[{keys.index(key)}] {err_msg} (attempt {attempts}/{max_attempts})")
                last_err = err_msg
                retries_left -= 1
                if retries_left >= 0 and log:
                    log(f"⧉ compaction: retrying key[{keys.index(key)}] ({retries_left} retries left)")
                    await asyncio.sleep(retry_delay)
                continue
            except httpx.HTTPError as e:
                err_msg = str(e) or type(e).__name__
                error_details.append(f"key[{keys.index(key)}] {err_msg}")
                if log:
                    log(f"⧉ compaction: key[{keys.index(key)}] {err_msg} (attempt {attempts}/{max_attempts})")
                last_err = err_msg
                retries_left -= 1
                if retries_left >= 0 and log:
                    log(f"⧉ compaction: retrying key[{keys.index(key)}] ({retries_left} retries left)")
                    await asyncio.sleep(retry_delay)
                continue
            
            try:
                if resp.status_code != 200:
                    body_text = ""
                    try:
                        body_text = resp.text[:200]  # First 200 chars of error body
                    except Exception:
                        pass
                    
                    err_msg = f"summarize HTTP {resp.status_code}"
                    if body_text:
                        err_msg += f" (body: {body_text})"
                    
                    error_details.append(f"key[{keys.index(key)}] {err_msg}")
                    
                    if log:
                        log(f"⧉ compaction: key[{keys.index(key)}] {err_msg} (attempt {attempts}/{max_attempts})")
                    
                    if resp.status_code == 429:
                        seen_429 = True
                        last_err = "summarize HTTP 429 (pool saturated)"
                        # Don't retry 429 - move to next key
                        break
                    elif resp.status_code in (400, 404):
                        # Client errors - likely bad request or endpoint issue
                        # Don't retry, move to next key
                        last_err = err_msg
                        break
                    elif resp.status_code >= 500:
                        # Server errors - may be transient, retry
                        last_err = err_msg
                        retries_left -= 1
                        if retries_left >= 0 and log:
                            log(f"⧉ compaction: server error, retrying key[{keys.index(key)}] ({retries_left} retries left)")
                            await asyncio.sleep(retry_delay)
                        continue
                    else:
                        # Other errors - don't retry
                        last_err = err_msg
                        break
                    
                data = resp.json()
            finally:
                await resp.aclose()

            txt = ((data.get("choices") or [{}])[0].get("message", {}) or {}).get(
                "content"
            ) or ""
            if not txt.strip():
                last_err = "summarize empty"
                error_details.append(f"key[{keys.index(key)}] empty response")
                if log:
                    log(f"⧉ compaction: key[{keys.index(key)}] empty response (attempt {attempts}/{max_attempts})")
                retries_left -= 1
                if retries_left >= 0 and log:
                    log(f"⧉ compaction: retrying key[{keys.index(key)}] ({retries_left} retries left)")
                    await asyncio.sleep(retry_delay)
                continue
            
            # Success! Log any accumulated errors for debugging
            if error_details and log:
                log(f"⧉ compaction: summarize succeeded after {len(error_details)} failed attempts")
                for err in error_details[-3:]:  # Log last 3 errors
                    log(f"  └─ {err}")
            return txt.strip()

    suffix = " (pool rate-limited)" if seen_429 else ""
    
    # Log all collected error details before raising
    if error_details and log:
        log(f"⧉ compaction: all {attempts} summarize attempts failed:")
        for err in error_details:
            log(f"  └─ {err}")
    
    raise RuntimeError(last_err or "summarize: all keys failed" + suffix)


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
    # Per-model context budget − reserved generation headroom.
    reserved = cfg.get("reserved_tokens", _DEFAULTS["reserved_tokens"])
    model_budget = _MODEL_BUDGETS.get(
        state.active_model or "", cfg["budget_tokens"]
    )
    budget = max(model_budget - reserved, reserved)
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
    # Fingerprint on the whole covered prefix (everything we'd summarise),
    # not just the first _CONV_KEY_PREFIX_MSGS messages. This catches the case
    # where two conversations share the same prefix but diverge later —
    # important when many concurrent sessions reuse the same system prompt.
    current_fp = _fingerprint(old)
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

    # Summarize ALWAYS runs on the server-local "quiet" model, NOT the hot
    # streamed model the client is saturating (e.g. z-ai/glm-5.2 being
    # hammered by Codex). Using the same model would compete with the very
    # requests that triggered compaction and burn the only available RPM.
    summary_model = cfg.get("summary_model") or "deepseek-ai/deepseek-v4-pro"
    # Gather all healthy+rate-feasible keys so _summarize can rotate on 429.
    # Least-RPM-first lets summarization land on the least busy key, not key[0].
    candidate_keys = [
        k
        for k in state.keys
        if state.is_key_healthy(k) and state.key_can_send_rpm(k)
    ]
    n_keys = len(state.keys) or 1
    min_healthy = max(1, int(n_keys * cfg.get("min_healthy_fraction", 0.25)))
    if len(candidate_keys) < min_healthy:
        # Pool is mostly saturated: a summarize attempt would serially 429
        # (the very bug that blocked Codex) — go directly to deterministic
        # trim so the real request goes through immediately.
        log(
            f"⧉ compaction: pool saturated ({len(candidate_keys)}/{n_keys} healthy) → trim fallback (no summarize)"
        )
    elif candidate_keys:
        max_attempts = cfg.get("max_summarize_attempts", _DEFAULTS["max_summarize_attempts"])
        attempt_timeout = cfg.get("summarize_timeout", _DEFAULTS["summarize_timeout"])
        retry_delay = cfg.get("summarize_retry_delay", _DEFAULTS["summarize_retry_delay"])
        max_retries = cfg.get("summarize_max_retries", _DEFAULTS["summarize_max_retries"])
        try:
            summary = await _summarize(
                client,
                candidate_keys,
                summary_model,
                base,
                new_msgs,
                cfg["summary_max_tokens"],
                log=log,
                max_attempts=max_attempts,
                attempt_timeout=attempt_timeout,
                retry_delay=retry_delay,
                max_retries=max_retries,
            )
            if len(_rolling) >= _ROLLING_CAP:
                _rolling.clear()
            _rolling[ck] = (len(old), summary, current_fp)
            log(
                f"⧉ compaction: summarized {len(old)} msg → {len(summary)} char (kept {len(tail)} recent, model={summary_model})"
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
