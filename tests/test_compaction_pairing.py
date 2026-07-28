"""Compaction must never hand NIM a tool result with no tool call.

Every cut this module makes is at a numeric index, and the OpenAI schema NIM
enforces requires each role:"tool" message to follow the assistant message
whose tool_calls it answers. Breaking that pairing produced a 400, which the
rotation loop then charged to the keys.
"""

from __future__ import annotations

import pytest

from openvidia import compaction


def assistant_with_tools(*call_ids, text=""):
    msg = {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": "Bash", "arguments": '{"cmd":"ls"}'},
            }
            for cid in call_ids
        ],
    }
    return msg


def tool_result(call_id, content="output"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def user(text):
    return {"role": "user", "content": text}


def orphaned_tool_ids(messages: list[dict]) -> list[str]:
    """tool_call_ids answered by a tool message with no preceding tool_calls."""
    offered: set[str] = set()
    orphans: list[str] = []
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                offered.add(tc.get("id"))
        elif m.get("role") == "tool":
            cid = m.get("tool_call_id")
            if cid not in offered:
                orphans.append(cid)
    return orphans


AGENT_HISTORY = [
    user("start"),
    assistant_with_tools("c1"),
    tool_result("c1"),
    user("next"),
    assistant_with_tools("c2", "c3"),
    tool_result("c2"),
    tool_result("c3"),
    user("more"),
    assistant_with_tools("c4"),
    tool_result("c4"),
    user("finally"),
]

SYSTEM = [{"role": "system", "content": "you are a helpful agent"}]


# --------------------------------------------------------------------------- #
# The helpers
# --------------------------------------------------------------------------- #


def test_orphan_free_drops_leading_tool_results():
    msgs = [tool_result("c2"), tool_result("c3"), user("more")]
    assert compaction._orphan_free(msgs) == [user("more")]


def test_orphan_free_keeps_a_well_formed_suffix():
    msgs = [assistant_with_tools("c1"), tool_result("c1")]
    assert compaction._orphan_free(msgs) == msgs


def test_orphan_free_on_empty_list():
    assert compaction._orphan_free([]) == []


def test_safe_cut_moves_forward_past_orphans():
    # index 5 and 6 are the tool results for the assistant at index 4
    assert AGENT_HISTORY[5]["role"] == "tool"
    assert compaction._safe_cut(AGENT_HISTORY, 5) == 7


def test_safe_cut_leaves_a_valid_boundary_alone():
    assert compaction._safe_cut(AGENT_HISTORY, 3) == 3


def test_safe_cut_clamps_out_of_range():
    assert compaction._safe_cut(AGENT_HISTORY, 999) == len(AGENT_HISTORY)
    assert compaction._safe_cut(AGENT_HISTORY, -5) == 0


# --------------------------------------------------------------------------- #
# _trim
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("budget", [200, 400, 800, 1600, 3200])
def test_trim_never_orphans_a_tool_result(budget):
    out = compaction._trim(SYSTEM, AGENT_HISTORY, budget, keep_recent=3)
    assert orphaned_tool_ids(out) == []


@pytest.mark.parametrize("budget", [200, 400, 800, 1600, 3200])
def test_trim_stays_within_budget(budget):
    out = compaction._trim(SYSTEM, AGENT_HISTORY, budget, keep_recent=3)
    assert compaction.estimate_tokens(out) <= budget


def tail_after_notice(out: list[dict], history: list[dict]) -> list[dict]:
    """_trim returns system + first message + an omission notice + a tail.

    The gap before the notice is deliberate and labelled. Everything after it
    is supposed to be a real suffix of the history.
    """
    for i, m in enumerate(out):
        if m.get("content", "").startswith("[previous messages omitted"):
            return out[i + 1 :]
    return [m for m in out if m in history]


def test_trim_keeps_a_contiguous_suffix():
    """The old loop skipped an over-budget message and kept older ones."""
    history = [
        user("a"),
        user("b"),
        {"role": "assistant", "content": "X" * 4000},  # the big one
        user("d"),
        user("e"),
    ]
    out = compaction._trim(SYSTEM, history, budget=400, keep_recent=2)

    tail = tail_after_notice(out, history)
    assert tail, "nothing was kept"
    first = history.index(tail[0])
    assert history[first : first + len(tail)] == tail, "kept messages are not contiguous"
    assert history[-1] == tail[-1], "the tail must reach the end of the conversation"


def test_trim_does_not_reach_past_a_big_message_for_older_ones():
    big = {"role": "assistant", "content": "X" * 6000}
    history = [user("oldest"), user("old"), big, user("recent")]

    out = compaction._trim(SYSTEM, history, budget=300, keep_recent=1)

    assert user("old") not in out, "skipped the big message and kept an older one"


# --------------------------------------------------------------------------- #
# _assemble
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("budget", [200, 400, 900, 2000])
def test_assemble_never_orphans_a_tool_result(budget):
    out = compaction._assemble(SYSTEM, "a summary", AGENT_HISTORY, [], budget)
    assert orphaned_tool_ids(out) == []


def test_assemble_drops_tool_results_together_with_their_assistant():
    remainder = [
        assistant_with_tools("c1"),
        tool_result("c1"),
        assistant_with_tools("c2"),
        tool_result("c2"),
    ]
    out = compaction._assemble(SYSTEM, "s", remainder, [], budget=120)
    assert orphaned_tool_ids(out) == []


def test_assemble_tail_is_cleaned_too():
    tail = [tool_result("c9"), user("last")]
    out = compaction._assemble(SYSTEM, "s", [], tail, budget=5000)
    assert orphaned_tool_ids(out) == []


# --------------------------------------------------------------------------- #
# Learned windows
# --------------------------------------------------------------------------- #


def test_probe_artifact_window_is_not_trusted(tmp_path, monkeypatch):
    """325000 is what the removed probe wrote when it could not parse a reply."""
    import json

    monkeypatch.setattr(compaction.config, "config_dir", lambda: tmp_path)
    (tmp_path / "model_limits.json").write_text(
        json.dumps(
            {
                "vendor/real": 202752,
                "vendor/poisoned": compaction._PROBE_ARTIFACT_WINDOW,
            }
        )
    )
    compaction._learned_limits.clear()
    compaction._learned_loaded = False

    learned = compaction._load_learned()

    assert learned == {"vendor/real": 202752}
    cfg = dict(compaction._DEFAULTS)
    # The poisoned model falls back to the conservative default, so compaction
    # runs again instead of being permanently disabled for it.
    assert compaction._resolve_budget(cfg, "vendor/poisoned") == cfg["budget_tokens"]


def test_the_active_probe_is_gone():
    assert not hasattr(compaction, "_probe_context_window")
    assert not hasattr(compaction, "_CTX_PROBE_CHARS")
