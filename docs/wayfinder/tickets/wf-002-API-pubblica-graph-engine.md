# wf-002 — API pubblica graph_engine

Label: `wayfinder:task` · AFK · **CLOSED**

blocked-by: wf-001

## Question

Quale public API espone `openvidia/graph_engine.py`? Design già deciso in sessione: `Hub` (register/new_name/close/snapshot), `run_agent`, `generate_and_verify`, `ToolSpec`/`WorkerSpec`/`Provider` dataclass, `graph_tools` (get_status, send_message, wait_for_message, create_subagents, kill_subagents), export da `__init__.py`.

## Resolution

Implementata e verificata (10/10 test, ruff pulito): `openvidia/graph_engine.py` con `Provider` (chat async OpenAI-style), `ToolSpec`, `WorkerSpec`, `Hub` (status/mailbox/task/events), `run_agent` (loop con max_iterations, budget_tokens, timeout, crash su risposta vuota, tool error recovery), `graph_tools` (closure con factory per spawn/kill, depth bound: i worker non spawnano), `generate_and_verify` (rubric esplicita, APPROVED/REJECTED, feedback nel round successivo). Export public da `openvidia/__init__.py`. Test: `tests/test_graph_engine.py` (10 test, ScriptedProvider).
