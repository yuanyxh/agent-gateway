# agent-gateway

AI capability layer shared by Utility clients: MCP memory, LLM gateway, token proxy,
RSS/knowledge jobs and the symbol scanner. Independent repository — nothing here
imports the macOS host, and the host does not import these internals.

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[proxy,memory,server]"   # or a subset of the extras
```

Source layout is `src/agent_gateway/`; running from the source tree requires the
editable install above (the entry point imports the installed package).

Extras: `proxy` (aiohttp + br/zstd), `memory` (mem0ai / fastembed / qdrant-client),
`server` (mcp / uvicorn / starlette).

## Run

```bash
export UTILITY_DATA_DIR=~/.config/utility     # config root; no personal default
agent-gateway --check                        # environment self-check
agent-gateway --serve --service all          # start enabled services
agent-gateway --export-contracts             # write contracts/env.json
```

- Config: `<UTILITY_DATA_DIR>/config.yaml` (the single source; env vars only override)
- Retrieval floors: `<UTILITY_DATA_DIR>/retrieval_thresholds.json` — auto-calibrated on
  first use of a model, re-read on every call, so no restart; `scripts/calibrate_thresholds.py`
  can measure a hand-picked table instead
- Runtime state: `<UTILITY_DATA_DIR>/runtime/<service>.json` (port + pid)
- Services: MCP on `:9001`, token proxy on `:8613` (both auto-probe when configured port is 0)

## Test

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
```

## Contracts

`contracts/env.json` lists every environment variable the package reads, generated from
the implementation. Consumers (the macOS host, the Node server) validate against this
file instead of scanning source, which cannot work across repositories.
