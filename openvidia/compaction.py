"""
Auto-compaction: history never makes a request fail from context overflow.

The conversation history is summarized in-place when it exceeds the model's
budget. A rolling cache makes every turn after the first *incremental*: only
the messages added since the last summary are sent upstream, so the steady
state costs a ~2s call instead of re-compressing the whole history.

Latency is bounded by an inline deadline, not by the upstream. If the
summarize call is still running when the deadline expires the request is
served immediately (cached summary + verbatim remainder, or deterministic
trim) while the summarize keeps running in the background and lands in the
cache for the next turn. A client (Codex/opencode) therefore never waits more
than ``inline_deadline`` seconds, and never loses early context for more than
one turn.

Fallback ladder, best first:
  1. fresh summary (just computed, or cache hit with nothing new)
  2. cached summary + as many verbatim newer messages as fit
  3. deterministic trim
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable

import httpx

from . import config

UPSTREAM = "https://integrate.api.nvidia.com/v1/chat/completions"

_DEFAULTS = {
    "enabled": True,
    # Activation threshold (~4 chars/token). Below this token count we
    # forward untouched. Keep a comfortable margin BELOW the model context
    # window. 80k is conservative for 128k-window NIM models.
    "budget_tokens": 80_000,
    # Floor for the deterministic trim fallback. On the summary path the
    # verbatim tail is sized to FILL the target instead (see _split_point),
    # so a large budget is not collapsed into "summary + 8 messages".
    "keep_recent": 8,
    "summary_max_tokens": 1024,  # cap on summarized output
    # Generation headroom reserved from budget. Combined with budget_tokens
    # this yields the effective activation point: budget - reserved.
    "reserved_tokens": 8000,
    # Compaction target as a fraction of the budget. Compacting exactly TO
    # the budget means the next turn overflows again immediately — the
    # "recompact every single turn" loop. 0.6 leaves room for several turns.
    "compact_ratio": 0.6,
    # Summarization NEVER uses the hot streamed model the client is hammering
    # (Codex/whatever); it uses a server-local "quiet" model so a compaction
    # request competes for keys on a DIFFERENT traffic stream, never the same.
    # Set to "" to fall back to the user's selected model.
    # Strongly recommended: point this at a FAST model — a reasoning-heavy
    # model needs 25s+ to compress 70k tokens.
    "summary_model": "",
    # Hard cap on summarize attempts: if the pool is saturated we must NOT
    # serially try all 25 keys, so we bail to deterministic trim after this
    # many rotate-attempts.
    "max_summarize_attempts": 3,
    # Per-attempt upstream read cap. Must be sized for the REAL cost of
    # compressing a large history (tens of seconds on a big model), not for
    # the incremental case — the client is protected by inline_deadline, not
    # by this. Too low here means the summary never completes at all.
    "summarize_timeout": 45.0,
    # How long the *client request* is willing to wait for a summary. Past
    # this the request is served from the fallback ladder while the
    # summarize keeps running in the background for the next turn.
    "inline_deadline": 6.0,
    # Bounds on the text handed to the summarizer. Tool outputs dominate a
    # coding-agent history and compress poorly; truncating them keeps the
    # summarize call fast without losing decisions/paths/state.
    "summary_msg_chars": 4_000,
    "summary_input_chars": 120_000,
    # When the pool health is below this fraction of live keys, skip the
    # summarize attempt entirely and go straight to the fallback ladder.
    "min_healthy_fraction": 0.25,
}

# ── Dynamic per-model context budgets ──────────────────────────────────
# DYNAMIC MODEL BUDGETS — no hardcoded per-model context windows.
# NVIDIA NIM does NOT advertise a context_window/max_tokens field on
# /v1/models, so we cannot read it live. Instead, every model gets the
# generic ``budget_tokens`` default unless the user explicitly overrides a
# specific model in compaction.json via the ``model_budgets`` map, e.g.:
#     {"model_budgets": {"z-ai/glm-5.2": 120000, "qwen/...": 32000}}
# This is the only place where per-model knowledge lives — runtime code
# never hardcodes a provider/model name. The proxy is provider-agnostic.
_model_budget_cache: dict[str, int] = {}


def _model_budgets(cfg: dict) -> dict[str, int]:
    """User-supplied per-model budget overrides from compaction.json (dynamic)."""
    overrides = cfg.get("model_budgets") or {}
    if overrides != _model_budget_cache:
        _model_budget_cache.clear()
        _model_budget_cache.update(overrides)
    return _model_budget_cache


# ── Learned context windows ────────────────────────────────────────────
# A model the proxy has never seen must work at full context with ZERO
# configuration: providers add models continuously, and hand-maintaining a
# budget table means every new model silently runs truncated until someone
# notices. NVIDIA does not advertise the window on /v1/models, but it states
# it exactly when a request exceeds it:
#
#     This model's maximum context length is 202752 tokens. However, your
#     messages resulted in 320011 tokens.
#
# So we ask once, in the background, with a payload above every window the
# provider currently serves. The upstream rejects it before doing any
# inference, which makes the probe cheap; the answer is cached on disk
# forever. Until it lands, the conservative default applies — an unknown
# model is never allowed to overflow.
_CTX_PROBE_CHARS = 1_300_000  # ≈325k tokens
_CTX_LIMIT_RE = re.compile(r"maximum context length is (\d+)")
# The ~4 chars/token estimator underestimates on code and JSON, so only spend
# this fraction of a learned window.
_LEARNED_SAFETY = 0.8

_learned_limits: dict[str, int] = {}
_learned_loaded = False
_probing: set[str] = set()


def _limits_path():
    return config.config_dir() / "model_limits.json"


def _load_learned() -> dict[str, int]:
    global _learned_loaded
    if not _learned_loaded:
        _learned_loaded = True
        try:
            p = _limits_path()
            if p.exists():
                data = json.loads(p.read_text())
                _learned_limits.update({str(k): int(v) for k, v in data.items() if int(v) > 0})
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    return _learned_limits


def note_context_limit(model: str, body: str) -> int | None:
    """Record the window stated in an upstream error body, if any.

    Passive counterpart to the probe: whenever a real request overflows, the
    answer is right there in the error and costs nothing to keep.
    """
    if not model or not body:
        return None
    m = _CTX_LIMIT_RE.search(body)
    if not m:
        return None
    limit = int(m.group(1))
    _load_learned()
    if _learned_limits.get(model) == limit:
        return limit
    _learned_limits[model] = limit
    try:
        config.atomic_write(_limits_path(), json.dumps(_learned_limits, indent=2))
    except OSError:
        pass
    return limit


async def _probe_context_window(client, key: str, model: str, log) -> None:
    """One oversized request; the rejection states the window exactly."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "word " * (_CTX_PROBE_CHARS // 5)}],
        "max_completion_tokens": 1,
        "stream": False,
    }
    try:
        resp = await client.post(
            UPSTREAM,
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=120.0, pool=60.0),
        )
        body = resp.text
    except httpx.HTTPError as e:
        log(f"⧉ compaction: context probe for {model} failed ({type(e).__name__})")
        return
    finally:
        _probing.discard(model)

    limit = note_context_limit(model, body)
    if limit:
        log(f"⧉ compaction: learned {model} context window = {limit} tokens")
    else:
        # It accepted a payload larger than anything the provider currently
        # serves. Record that as a lower bound rather than probing higher.
        lower = _CTX_PROBE_CHARS // 4
        _load_learned()
        _learned_limits[model] = lower
        try:
            config.atomic_write(_limits_path(), json.dumps(_learned_limits, indent=2))
        except OSError:
            pass
        log(f"⧉ compaction: {model} accepted ≥{lower} tokens — using that as the window")


def _resolve_budget(cfg: dict, model: str) -> int:
    """User override → learned window → conservative default."""
    override = _model_budgets(cfg).get(model or "")
    if override:
        return int(override)
    learned = _load_learned().get(model or "")
    if learned:
        return int(learned * _LEARNED_SAFETY)
    return int(cfg["budget_tokens"])


_settings_cache: tuple[float, float, dict] | None = None
_SETTINGS_TTL = 5.0


def _settings() -> dict:
    """compaction.json, re-read at most every _SETTINGS_TTL seconds.

    maybe_compact runs on every request; stat+read+parse per request is pure
    syscall overhead on the hot path.
    """
    global _settings_cache
    now = time.monotonic()
    if _settings_cache is not None and now - _settings_cache[0] < _SETTINGS_TTL:
        return _settings_cache[2]
    loaded = dict(_DEFAULTS)
    mtime = 0.0
    try:
        p = config.config_dir() / "compaction.json"
        if p.exists():
            mtime = p.stat().st_mtime
            loaded = {**_DEFAULTS, **json.loads(p.read_text())}
    except (json.JSONDecodeError, OSError):
        pass
    _settings_cache = (now, mtime, loaded)
    return loaded


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: ~4 chars/token on the serialized JSON."""
    try:
        return len(json.dumps(messages, ensure_ascii=False)) // 4
    except (TypeError, ValueError):
        return sum(len(str(m)) for m in messages) // 4


def _msg_tokens(m: dict) -> int:
    """Per-message estimate. Summing these over a list is always >= the
    estimate of the serialized list, so budget arithmetic built on them is
    conservative by construction."""
    return estimate_tokens([m])


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


def _clip(text: str, cap: int) -> str:
    """Keep the head and the tail of an oversized blob, drop the middle."""
    if cap <= 0 or len(text) <= cap:
        return text
    head = (cap * 2) // 3
    tail = cap - head
    return f"{text[:head]}\n…[{len(text) - cap} chars omitted]…\n{text[-tail:]}"


def _render_for_summary(messages: list[dict], msg_cap: int, total_cap: int) -> str:
    """Bounded render used as summarizer input.

    Never send an unbounded history to the summarizer: a coding-agent
    transcript is mostly tool output, which is what makes the call slow
    enough to blow the deadline. Per-message clipping keeps decisions, paths
    and state; the total cap keeps the most recent material when even the
    clipped render is too large.
    """
    rendered = [_clip(_render([m]), msg_cap) for m in messages]
    out = "\n".join(rendered)
    if total_cap > 0 and len(out) > total_cap:
        kept: list[str] = []
        size = 0
        for line in reversed(rendered):
            if size + len(line) > total_cap:
                break
            kept.insert(0, line)
            size += len(line) + 1
        out = "[older messages elided]\n" + "\n".join(kept)
    return out


_CONV_KEY_PREFIX_MSGS = 4  # initial messages of `rest` used for the conversation identity


def _conv_key(system_block: list[dict], rest: list[dict]) -> str:
    """
    Stable identity for a conversation, INVARIANT as the conversation grows.

    Identity = hash of system_block + the first N messages. Two conversations
    that share a system prompt but diverge at message 2 (common with agentic
    CLIs) hash differently, so the rolling cache never swaps summaries across
    sessions. The length is deliberately NOT part of the key: including it
    minted a new key on every turn, which made the rolling cache unreachable
    and forced a full re-summarize of the whole history each time. Staleness
    is handled by the fingerprint check on the covered prefix, not by the key.
    """
    h = hashlib.sha256()
    for m in system_block + rest[:_CONV_KEY_PREFIX_MSGS]:
        h.update(_render([m]).encode("utf-8", "replace"))
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


# ck -> (covered_count, summary, fingerprint_of_covered_prefix)
_rolling: dict[str, tuple[int, str, str]] = {}
_ROLLING_CAP = 256
# ck -> (task, covered_count_the_task_will_cover)
_inflight: dict[str, tuple[asyncio.Task, int]] = {}


def _cache_put(ck: str, covered: int, summary: str, fp: str) -> None:
    if ck not in _rolling and len(_rolling) >= _ROLLING_CAP:
        # FIFO eviction — dropping the whole cache would re-trigger a full
        # re-summarize for every live session at once.
        _rolling.pop(next(iter(_rolling)), None)
    _rolling[ck] = (covered, summary, fp)


def _cache_get(ck: str, old: list[dict]) -> tuple[int, str] | None:
    """Return (covered, summary) if the cached summary is a valid prefix of
    the current history, else None."""
    prev = _rolling.get(ck)
    if not prev:
        return None
    covered, summary, fp = prev
    if covered > len(old):
        return None
    if _fingerprint(old[:covered]) != fp:
        return None
    return covered, summary


async def _summarize(
    client,
    keys: list[str],
    model: str,
    prev_summary: str | None,
    new_msgs: list[dict],
    max_tokens: int,
    *,
    max_attempts: int = 3,
    attempt_timeout: float = 45.0,
    msg_cap: int = 4_000,
    total_cap: int = 120_000,
) -> str:
    """Summarize with rotation across the given keys + bounded attempts.

    We rotate across at most ``max_attempts`` keys (NOT the whole pool — with
    25 keys that would mean minutes of upstream work for a single summary).
    ``attempt_timeout`` must be generous enough for the call to actually
    succeed; the caller protects the client with its own inline deadline and
    lets this finish in the background.
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
    user += "New messages to integrate into the summary:\n" + _render_for_summary(
        new_msgs, msg_cap, total_cap
    )

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
    attempts = 0
    seen_429 = False
    for key in keys:
        if attempts >= max_attempts:
            break
        attempts += 1
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
                        "connect": 5.0,
                        "read": attempt_timeout,
                        "write": 10.0,
                        "pool": attempt_timeout,
                    }
                },
            )
            resp = await client.send(req)
        except httpx.HTTPError as e:
            last_err = str(e) or type(e).__name__
            continue
        try:
            if resp.status_code != 200:
                last_err = f"summarize HTTP {resp.status_code}"
                if resp.status_code == 429:
                    seen_429 = True
                    last_err = "summarize HTTP 429 (pool saturated)"
                continue
            data = resp.json()
        finally:
            await resp.aclose()

        txt = ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
        if not txt.strip():
            last_err = "summarize empty"
            continue
        return txt.strip()

    suffix = " (pool rate-limited)" if seen_429 else ""
    raise RuntimeError((last_err or "summarize: all keys failed") + suffix)


def _trim(system_block: list[dict], rest: list[dict], budget: int, keep_recent: int) -> list[dict]:
    """Deterministic fallback: keep system + first msg + as many recent as fit.

    GUARANTEE: the returned message list never exceeds ``budget`` tokens.
    Sizing uses per-message estimates (whose sum is always >= the estimate of
    the serialized list), so the arithmetic is conservative and the whole
    function stays O(n) — the previous version re-serialized the entire list
    inside its safety loops, which is O(n²) on a 400-message history.
    """
    notice = {"role": "system", "content": "[previous messages omitted to fit context]"}
    notice_t = _msg_tokens(notice)
    sys_t = sum(_msg_tokens(m) for m in system_block)

    head = rest[:1]
    head_t = sum(_msg_tokens(m) for m in head)

    base = sys_t + notice_t + head_t
    if base > budget:  # head does not fit: drop it
        head, head_t = [], 0
        base = sys_t + notice_t
    if base > budget:  # system block itself overflows: notice only
        return [notice]

    tail: list[dict] = []
    total = base
    for m in reversed(rest[1:]):
        t = _msg_tokens(m)
        if total + t > budget:
            if len(tail) >= keep_recent:
                break
            continue
        tail.insert(0, m)
        total += t

    result = system_block + head + [notice] + tail
    # Cheap post-condition check against the real serialization; the summed
    # estimate is an upper bound, so this practically never fires.
    while tail and estimate_tokens(result) > budget:
        tail.pop(0)
        result = system_block + head + [notice] + tail
    return result


def _summary_block(summary: str) -> dict:
    return {"role": "system", "content": "Previous conversation summary:\n" + summary}


def _split_point(system_block: list[dict], rest: list[dict], target: int, reserve: int) -> int:
    """How many trailing messages to keep verbatim.

    Fills the target with the largest recent suffix that fits, leaving room
    for the system block and the summary. ``keep_recent`` is only a floor for
    the trim fallback: when a 150k budget is available, collapsing everything
    but the last 8 messages into one paragraph throws away context the model
    could have used.
    """
    avail = target - sum(_msg_tokens(m) for m in system_block) - reserve
    n = 0
    total = 0
    for m in reversed(rest):
        t = _msg_tokens(m)
        if n >= 1 and total + t > avail:
            break
        total += t
        n += 1
    # Always leave at least one message on the summarize side.
    return max(1, min(n, len(rest) - 1))


def _assemble(
    system_block: list[dict],
    summary: str,
    remainder: list[dict],
    tail: list[dict],
    budget: int,
) -> list[dict]:
    """system + summary block + verbatim remainder + recent tail, under budget.

    ``remainder`` are messages newer than the summary but older than the
    kept tail (they exist when we serve a summary computed one turn ago).
    They are dropped oldest-first until the result fits.
    """
    block = _summary_block(summary)
    fixed = sum(_msg_tokens(m) for m in system_block + [block] + tail)
    rem = list(remainder)
    total = fixed + sum(_msg_tokens(m) for m in rem)
    while rem and total > budget:
        total -= _msg_tokens(rem.pop(0))
    result = system_block + [block] + rem + tail
    while len(tail) > 1 and estimate_tokens(result) > budget:
        tail = tail[1:]
        result = system_block + [block] + rem + tail
    return result


def _healthy_keys(state) -> tuple[list[str], int]:
    """Healthy + RPM-eligible keys, least-busy first."""
    keys = [k for k in state.keys if state.is_key_healthy(k) and state.key_can_send_rpm(k)]
    try:  # least-RPM-first: land the summary on the quietest key, not key[0]
        keys.sort(key=state.key_rpm)
    except (AttributeError, TypeError):
        pass
    return keys, len(state.keys) or 1


def _start_summarize(
    ck: str,
    covered_target: int,
    fp: str,
    *,
    client,
    keys: list[str],
    model: str,
    base: str | None,
    new_msgs: list[dict],
    cfg: dict,
    log: Callable[[str], None],
) -> asyncio.Task:
    """Launch (or reuse) the single summarize task for this conversation."""
    existing = _inflight.get(ck)
    if existing and not existing[0].done():
        return existing[0]

    started = time.monotonic()
    n_new = len(new_msgs)

    async def _run() -> str:
        return await _summarize(
            client,
            keys,
            model,
            base,
            new_msgs,
            cfg["summary_max_tokens"],
            max_attempts=cfg.get("max_summarize_attempts", 3),
            attempt_timeout=cfg.get("summarize_timeout", 45.0),
            msg_cap=cfg.get("summary_msg_chars", 4_000),
            total_cap=cfg.get("summary_input_chars", 120_000),
        )

    task = asyncio.ensure_future(_run())

    def _done(t: asyncio.Task) -> None:
        cur = _inflight.get(ck)
        if cur and cur[0] is t:
            _inflight.pop(ck, None)
        if t.cancelled():
            return
        err = t.exception()
        elapsed = time.monotonic() - started
        if err is not None:
            log(f"⧉ compaction: summarize failed ({err}) in {elapsed:.1f}s → fallback")
            return
        summary = t.result()
        _cache_put(ck, covered_target, summary, fp)
        log(
            f"⧉ compaction: summarized {n_new} new msg (covers {covered_target}) "
            f"→ {len(summary)} char in {elapsed:.1f}s (model={model})"
        )

    task.add_done_callback(_done)
    _inflight[ck] = (task, covered_target)
    return task


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
    # Per-model context budget − reserved generation headroom. Per-model
    # override comes from the user compaction.json ``model_budgets`` map
    # (dynamic, no hardcoded provider names). Falls back to budget_tokens.
    reserved = cfg.get("reserved_tokens", _DEFAULTS["reserved_tokens"])
    active = state.active_model or ""
    # An unseen model runs on the conservative default while a one-off
    # background probe learns its real window. No per-model configuration is
    # ever required — a model added by the provider tomorrow works at full
    # context the request after we first see it.
    if (
        active
        and active not in _model_budgets(cfg)
        and active not in _load_learned()
        and active not in _probing
    ):
        keys, _ = _healthy_keys(state)
        if keys:
            _probing.add(active)
            asyncio.ensure_future(_probe_context_window(client, keys[0], active, log))
    budget = max(_resolve_budget(cfg, active) - reserved, reserved)
    if estimate_tokens(messages) <= budget:
        return messages

    # Compact BELOW the trigger, not exactly to it: landing on the threshold
    # means the very next turn overflows again (the recompact-every-turn loop).
    ratio = cfg.get("compact_ratio", _DEFAULTS["compact_ratio"])
    target = max(int(budget * ratio), reserved)

    keep_recent = cfg["keep_recent"]
    system_block, rest = _split(messages)
    if len(rest) < 2:
        return messages

    ck = _conv_key(system_block, rest)
    # Room the summary block itself will occupy once generated.
    reserve = cfg["summary_max_tokens"] + 64

    cached = _cache_get(ck, rest)
    covered, base = cached if cached else (0, None)

    if base is not None:
        # Fast path: cached summary + EVERYTHING after it, verbatim. The
        # summary boundary only advances when this no longer fits, so an
        # upstream summarize happens once every few turns instead of once
        # per turn — and the model keeps the maximum verbatim context.
        candidate = system_block + [_summary_block(base)] + rest[covered:]
        if estimate_tokens(candidate) <= budget:
            return candidate

    # Boundary must advance: keep the largest recent suffix that fits the
    # target and summarize everything before it.
    keep_n = _split_point(system_block, rest, target, reserve)
    split = len(rest) - keep_n
    new_msgs = rest[covered:split]
    if not new_msgs:
        # Already summarized up to the split yet still over budget (huge
        # recent messages): shed verbatim messages oldest-first.
        if base is not None:
            return _assemble(system_block, base, rest[covered:], [], target)
        return _trim(system_block, rest, target, keep_recent)

    # Summarize ALWAYS runs on the server-local "quiet" model, NOT the hot
    # streamed model the client is saturating. Using the same model would
    # compete with the very requests that triggered compaction and burn the
    # only available RPM. No hardcoded model here — the user's selection
    # is the source of truth when summary_model is unset.
    from .proxy_app import default_model

    summary_model = cfg.get("summary_model") or default_model(state)
    candidate_keys, n_keys = _healthy_keys(state)
    min_healthy = max(1, int(n_keys * cfg.get("min_healthy_fraction", 0.25)))

    task: asyncio.Task | None = None
    inflight = _inflight.get(ck)
    if inflight and not inflight[0].done():
        # Another request (or an earlier turn) is already compressing this
        # conversation — never duplicate the work, just wait on it.
        task = inflight[0]
    elif len(candidate_keys) < min_healthy:
        log(
            f"⧉ compaction: pool saturated ({len(candidate_keys)}/{n_keys} healthy) "
            f"→ no summarize this turn"
        )
    else:
        task = _start_summarize(
            ck,
            split,
            _fingerprint(rest[:split]),
            client=client,
            keys=candidate_keys,
            model=summary_model,
            base=base,
            new_msgs=new_msgs,
            cfg=cfg,
            log=log,
        )

    if task is not None:
        deadline = cfg.get("inline_deadline", _DEFAULTS["inline_deadline"])
        # Shield: if the client disconnects mid-wait the summarize must
        # survive to populate the cache for the next turn.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=deadline)
        except TimeoutError:
            log(
                f"⧉ compaction: summarize still running (>{deadline:.0f}s) "
                f"→ serving now, summary lands next turn"
            )
        except Exception:  # noqa: BLE001 — logged by the task callback
            pass
        fresh = _cache_get(ck, rest)
        if fresh and fresh[0] >= covered:
            covered, base = fresh

    # Stale-but-valid summary beats a raw trim: keep the compressed early
    # context and append as many verbatim newer messages as fit.
    if base is not None:
        result = _assemble(system_block, base, rest[covered:], [], target)
        log(
            f"⧉ compaction: summary covers {covered}/{len(rest)} msg "
            f"+ {len(result) - len(system_block) - 1} verbatim (~{estimate_tokens(result)} tok)"
        )
        return result

    trimmed = _trim(system_block, rest, target, keep_recent)
    log(f"⧉ compaction: trim → {len(trimmed)} msg (~{estimate_tokens(trimmed)} tok)")
    return trimmed
