"""
Auto-compaction: la history non fa MAI fallire una richiesta per context overflow.

Strategia SUMMARIZE + cache roll-forward + fallback TRIM:
- se la history stimata supera il budget del modello, i messaggi vecchi
  (tutto tranne i system iniziali + gli ultimi KEEP_RECENT) vengono riassunti
  con UNA call upstream e sostituiti da un blocco system "riassunto".
- il riassunto e' cache-ato per conversazione (roll-forward): a ogni turno si
  riassumono solo i messaggi NUOVI oltre a quelli gia' coperti dal riassunto
  precedente, non da capo → una sola call e piccola, non 2x a ogni messaggio.
- se il riassunto fallisce (chiavi giu' / timeout / 400 troppo lungo) → TRIM
  deterministico: la richiesta passa comunque e NON si blocca mai.

Zero astrazioni: funzioni pure + un dict di cache in-process.
Config opzionale (no-env): config_dir()/compaction.json.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable, Optional

from . import config

UPSTREAM = "https://integrate.api.nvidia.com/v1/chat/completions"

_DEFAULTS = {
    "enabled": True,
    "budget_tokens": 100_000,    # soglia di attivazione (stima ~4 char/token)
    "keep_recent": 8,            # messaggi finali tenuti verbatim
    "summary_max_tokens": 1024,  # cap output del riassunto
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
    """Stima grezza: ~4 char/token sul JSON serializzato."""
    try:
        return len(json.dumps(messages, ensure_ascii=False)) // 4
    except (TypeError, ValueError):
        return sum(len(str(m)) for m in messages) // 4


def _split(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Divide in (system iniziali, resto della conversazione)."""
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


def _conv_key(system_block: list[dict], rest: list[dict]) -> str:
    """Identita' stabile della conversazione: system + primo messaggio utente."""
    h = hashlib.sha256()
    for m in system_block + rest[:1]:
        h.update(_render([m]).encode("utf-8", "replace"))
    return h.hexdigest()


# conv_key -> (covered_count, summary_text)
#   covered_count = quanti messaggi di `rest` sono gia' inclusi nel riassunto
_rolling: dict[str, tuple[int, str]] = {}
_ROLLING_CAP = 256


def _pick_key(state) -> Optional[str]:
    for k in state.keys:
        if state.is_key_healthy(k) and state.key_can_send_rpm(k):
            return k
    return None


async def _summarize(client, key: str, model: str, prev_summary: Optional[str],
                     new_msgs: list[dict], max_tokens: int) -> str:
    sys = (
        "Sei un compressore di cronologia conversazioni. Produci un riassunto "
        "conciso ma denso che preservi: decisioni prese, fatti, modifiche a file "
        "e relativi percorsi, stato del task corrente, valori/parametri importanti. "
        "Niente preamboli: restituisci solo il riassunto."
    )
    user = ""
    if prev_summary:
        user += "Riassunto finora:\n" + prev_summary + "\n\n"
    user += "Nuovi messaggi da integrare nel riassunto:\n" + _render(new_msgs)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
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

    txt = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content") or ""
    if not txt.strip():
        raise RuntimeError("summarize empty")
    return txt.strip()


def _trim(system_block: list[dict], rest: list[dict], budget: int, keep_recent: int) -> list[dict]:
    """Fallback deterministico: system + primo msg + ultimi finche' stanno nel budget."""
    head = rest[:1]
    total = estimate_tokens(system_block + head)
    tail: list[dict] = []
    for m in reversed(rest[1:]):
        t = estimate_tokens([m])
        if total + t > budget and len(tail) >= keep_recent:
            break
        tail.insert(0, m)
        total += t
    notice = {"role": "system", "content": "[messaggi precedenti omessi per rientrare nel contesto]"}
    return system_block + head + [notice] + tail


async def maybe_compact(messages: list[dict], *, state, client,
                        log: Callable[[str], None]) -> list[dict]:
    """
    Ritorna la lista di messaggi compattata se serve, altrimenti quella originale
    (stessa reference → il chiamante puo' evitare di ri-serializzare).
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
        return messages  # niente di comprimibile

    old = rest[:-keep_recent]
    tail = rest[-keep_recent:]

    # roll-forward: riassumi solo i messaggi nuovi oltre a quelli gia' coperti
    ck = _conv_key(system_block, rest)
    prev = _rolling.get(ck)
    if prev and prev[0] <= len(old):
        covered, prev_summary = prev
        new_msgs = old[covered:]
        base = prev_summary
        if not new_msgs:
            block = {"role": "system", "content": "Riassunto conversazione precedente:\n" + prev_summary}
            return system_block + [block] + tail
    else:
        new_msgs = old
        base = None

    model = state.active_model or "deepseek-ai/deepseek-v4-pro"
    key = _pick_key(state)
    if key is not None:
        try:
            summary = await _summarize(client, key, model, base, new_msgs, cfg["summary_max_tokens"])
            if len(_rolling) >= _ROLLING_CAP:
                _rolling.clear()
            _rolling[ck] = (len(old), summary)
            log(f"⧉ compaction: riassunti {len(old)} msg → {len(summary)} char (tenuti {len(tail)} recenti)")
            block = {"role": "system", "content": "Riassunto conversazione precedente:\n" + summary}
            return system_block + [block] + tail
        except Exception as e:  # noqa: BLE001 — qualsiasi errore → trim, non bloccare
            # NON marchiamo la chiave failed: un 400 "troppo lungo" non e' colpa della chiave.
            log(f"⧉ compaction: summarize fallito ({e}) → trim fallback")
    else:
        log("⧉ compaction: nessuna chiave sana → trim fallback")

    trimmed = _trim(system_block, rest, budget, keep_recent)
    log(f"⧉ compaction: trim → {len(trimmed)} msg (~{estimate_tokens(trimmed)} tok)")
    return trimmed
