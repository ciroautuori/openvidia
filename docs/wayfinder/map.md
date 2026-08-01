# Graph Engine: mappa wayfinder

Label: `wayfinder:map` — tracker local-markdown (GitHub issues disabilitati sul repo).

## Destination

Modulo `openvidia/graph_engine.py` (~400 righe, async) che implementa il Graph Loop su DeepSeek locale (localhost:1919): Hub + run_agent + tool spawn/status/collect/kill + generate_and_verify, con test pytest verdi e ruff pulito.

## Notes

- domain: python async, httpx, orchestrazione agenti (pattern Anthropic: orchestrator-subagent, generator-verifier, contesto fresco, depth bound)
- skill: /grilling, /domain-modeling, /tdd
- OVERRIDE: effort in **full-delivery** — i ticket si risolvono nella sessione stessa, non solo decisioni. Chart minimale, poi implementazione completa.

## Decisions so far

- [wf-001 full-delivery approved](tickets/wf-001-full-delivery.md) — l'utente ha richiesto mappa + implementazione completa nella stessa sessione ("E POI PARTI FULL DELIVERY").
- [wf-002 API pubblica graph_engine](tickets/wf-002-API-pubblica-graph-engine.md) — `openvidia/graph_engine.py` implementato e verificato: Hub + run_agent + graph_tools + generate_and_verify, 10/10 test, ruff pulito, export da `__init__.py`.

## Not yet specified

- Provider alternativi (non-DeepSeek) via env/CLI
- Persistenza sessioni graph su disco

## Out of scope

- Multi-nodo Redis / HTTP2 pool upstream (roadmap separata del proxy)
- Frontend dashboard del graph engine
