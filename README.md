# agent-gateway

English | [简体中文](README.zh.md)

[![CI](https://github.com/yuanyxh/agent-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/yuanyxh/agent-gateway/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A self-hosted AI capability layer for desktop agents. It provides the parts an agent
needs beyond the model itself — durable memory, code intelligence, web access and a
model gateway — behind MCP and plain HTTP.

The package runs as a standalone Python service or as a subprocess of a larger host.
It does not depend on a desktop framework, and hosts integrate through MCP tools,
HTTP endpoints and JSON contracts instead of importing internals.

> Status: 0.1.0. The public surface is usable, but interfaces may still change
> between minor releases.

## Features

### Durable memory

- **Two-tier storage.** The hot layer keeps raw facts in mem0 backed by an embedded
  Qdrant collection. The cold layer indexes consolidated knowledge in a separate
  collection. Both share one embedder and one local vector store.
- **Write-time hygiene.** Near-duplicate facts are deduplicated, contradictory
  memories are checked, and facts from the same episode are grouped so related
  context can be recalled together.
- **Lifecycle control.** Memories move through active, superseded, archived and
  deleted states. Important entries can be protected, and superseded versions stay
  traceable instead of being overwritten.
- **Memory graph.** A SQLite graph records version chains, episode membership,
  derivation, contradictions, related entries and mentions, and exposes them through
  `memory_graph`.
- **Bounded growth.** Scheduled jobs archive stale entries, consolidate summaries and
  fold summary budgets, so the hot layer does not grow without limit.
- **Associative network.** Episode groups become neurons joined by weighted synapses
  built by a daily vector-neighbour search plus LLM review; edges use raw cosine with a
  0.62 gate and sparse engram codes rank candidates. Recall spreads through an
  attractor activation, co-recalled memories reinforce synapses, and nightly replay
  adds low-weight inferred edges (revoked after 14 days without review). The weekly
  pass tiers neurons into active, warm, archive and trash with timed physical cleanup.

### Retrieval

- Three retrieval arms — dense hot memory, dense cold memory and lexical project
  notes — are fused and gated by a single pipeline.
- Relevance floors are calibrated per embedding model and can be re-calibrated when
  the model changes.
- Recall responses are layered: `results` holds confirmed hits only (may be empty),
  `candidates` holds unconfirmed entry points with a `reason` (`below_floor` /
  `rerank_rejected` / `filter_miss`), and `status` is `ok` / `weak_match` /
  `candidates_only` / `empty`. `weak_match` means the hits are lexical or related
  context rather than semantic matches. Short queries (≤3 terms), where the
  cross-encoder is unreliable, rescue term-matched rejects into `results` with
  `rerank_rescued`; they never outrank rerank-passed rows. Trace metadata joins a
  result back to its arms. Failed web reads also record `error_code` as trace `error`.

### Project intelligence

- A resident tree-sitter indexer builds a per-project symbol and call graph for
  Python, Swift, JavaScript/TypeScript/TSX, Java, Kotlin, Objective-C and Vue.
- `project_lookup` returns definitions with one-hop caller context.
- `project_impact` walks callers or callees to show what a change touches.
- `project_notes` searches project Markdown by meaning, using hybrid semantic,
  keyword and path ranking with de-duplication.

### Web access

- `websearch_engine` queries configurable search engines, including Bing,
  DuckDuckGo, Brave, Sogou, 360, Baidu and Marginalia. Google can be plugged in
  through an external command provider.
- `read_url` converts pages to Markdown with trafilatura, falls back to a lighter
  converter for short or empty pages, and discards boilerplate.
- Search caching, per-engine rate limits and optional top-page fetching are built in.

### Model gateway

- The token proxy accepts Anthropic- and OpenAI-shaped traffic on `/v1/*`, with
  `/open/*` and `/raw/*` aliases for compatibility and transparent forwarding.
- Requests are routed by exact model name across multiple providers; the upstream
  model can be remapped.
- Streaming responses, `gzip`/`deflate`/`br`/`zstd` decoding, SSE usage extraction
  and per-request accounting are handled in one place.
- Optional request/response rewrites include tool-output compaction, repeated skill
  de-duplication and a per-request annotation of what was rewritten.
- Usage is stored in SQLite and exposed through `/usage/summary`, `/usage/tools` and
  `/usage/layers`.
- Every request gets a trace id: the response carries `X-Trace-Id`, and the same id
  lands in the usage and gateway-log tables, so a client report maps to one row.

### Local-first operation

- `agent-gateway.yaml` is the single configuration source; environment variables
  override individual values.
- Vector data, caches, logs and SQLite databases stay under `GATEWAY_HOME` and
  `GATEWAY_CACHE`.
- No hosted control plane and no telemetry. The embedding model is downloaded once
  and then served from the local cache.

## Architecture

```mermaid
flowchart TD
    Client["Desktop agent / MCP client"] -->|MCP streamable HTTP| MCP["services.mcp.server"]
    Client -->|HTTP| REST["/api/*"]
    Client -->|Anthropic / OpenAI traffic| Proxy["services.proxy.token_proxy"]

    MCP --> Memory["Hot + cold memory"]
    MCP --> Graph["Code graph / notes"]
    MCP --> Web["Search / read"]
    REST --> LLM["llm.client"]
    REST --> Summ["Summarize"]
    Proxy --> Upstream["LLM providers"]

    Memory --> Qdrant[("Qdrant (local)")]
    Memory --> MemGraph[("SQLite memory graph")]
    Graph --> TS["tree-sitter"]
    Web --> Fetch["trafilatura / HTTP"]
```

| Module | Path | Responsibility |
| --- | --- | --- |
| CLI | `src/agent_gateway/cli/` | `agent-gateway` entry, service supervision, contract export |
| Core | `src/agent_gateway/core/` | Config, paths, logging, ports, prompts, l10n |
| LLM | `src/agent_gateway/llm/` | One exit point for model calls |
| MCP | `src/agent_gateway/services/mcp/` | MCP server, REST routes, memory tools, project tools |
| Memory | `src/agent_gateway/services/memory/` | Memory graph, links, retention, consolidation |
| Retrieval | `src/agent_gateway/services/retrieval/` | Pipeline, fusion, gating, calibration, tracing |
| Graph | `src/agent_gateway/services/graph/` | tree-sitter indexer, resolvers, language parsers |
| Search | `src/agent_gateway/services/search/` | Search engine registry and execution |
| Read / fetch | `src/agent_gateway/services/read/`, `fetch/`, `markdown/` | Page retrieval and Markdown conversion |
| Proxy | `src/agent_gateway/services/proxy/` | Token proxy, provider routing, usage accounting |
| Summarize | `src/agent_gateway/services/summarize/` | Memory summarization pipeline |
| Knowledge | `src/agent_gateway/services/knowledge/` | Knowledge card refill job |
| Scanner | `src/agent_gateway/scanner/` | AST symbol extractor and JS helper |

## Requirements

- Python 3.12 or newer.
- A local directory for Qdrant data and the embedding model cache.
- Optional: Node.js 18+ only if you use the JavaScript/Vue branch of the legacy AST
  scanner. The graph indexer is pure Python.

## Install

From source:

```bash
git clone https://github.com/yuanyxh/agent-gateway.git
cd agent-gateway
python -m venv .venv
. .venv/bin/activate
pip install -e ".[proxy,memory,server]"
```

Extras are additive; install the ones for the features you use:

| Extra | Installs |
| --- | --- |
| `server` | MCP server, HTTP layer, page extraction |
| `memory` | mem0, fastembed, numpy, Qdrant client |
| `proxy` | aiohttp and Brotli/zstd decoders |

Build a wheel for distribution:

```bash
python -m build
```

The wheel includes prompts, locale files and the `ast-js` helper, so a host can
install it into an isolated environment without copying source files.

## Quick start

Create a configuration root:

```bash
mkdir -p ~/.config/agent-gateway
export GATEWAY_HOME="$HOME/.config/agent-gateway"
```

Write `~/.config/agent-gateway/agent-gateway.yaml`:

```yaml
server:
  mcp:
    enabled: true
  proxy:
    enabled: false

memory:
  qdrantPath: ~/.config/agent-gateway/data/qdrant
  embedder:
    model: sentence-transformers/all-MiniLM-L6-v2

notes:
  dir: ~/notes
```

Check the environment and start the MCP service:

```bash
agent-gateway --check
agent-gateway --serve --service mcp
```

The first start downloads the embedding model into the cache. The service listens on
`http://127.0.0.1:9001`; point an MCP client at the streamable HTTP endpoint:

```json
{
  "mcpServers": {
    "agent-gateway": {
      "url": "http://127.0.0.1:9001/mcp"
    }
  }
}
```

`/health` reports liveness. If `mcp.port` is `0`, the CLI probes for a free port and
records it under `GATEWAY_HOME/runtime/`.

### Enable the model gateway

Add one or more providers and start the proxy:

```yaml
modelProxy:
  enabled: true
  providers:
    - name: anthropic
      url: https://api.anthropic.com
      apiFormat: anthropic
      apiKey: "your-key"
      models: [claude-sonnet-5-5]
```

```bash
agent-gateway --serve --service proxy
```

Point the client's base URL at `http://127.0.0.1:8613`. Provider URLs are base URLs
without the protocol path; the proxy appends `v1/messages` or
`v1/chat/completions` according to the client and provider formats.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `remember` | Write one fact with context, type, sources, importance and lifecycle links |
| `remember_batch` | Write several facts in one call |
| `recall` | Search hot memory, cold knowledge and project notes |
| `catch_up` | Read recent memories since a number of days, with pagination |
| `forget` | Delete a memory or knowledge point |
| `set_memory_processed` | Set or clear processing bits on memories |
| `memory_get` | Read one memory by id, including archived entries |
| `memory_graph` | Follow version, episode, derivation, dispute or mention edges |
| `project_lookup` | Find a symbol or file definition with one-hop callers |
| `project_impact` | Walk callers or callees to estimate change impact |
| `project_notes` | Search project Markdown by meaning and keywords |
| `websearch_engine` | Search the web and optionally fetch top pages |
| `read_url` | Convert a page to Markdown |
| `tool_resource` | Load a packaged reference or script when instructed |
| `service_register` | Register or update a service card in the mesh registry |
| `service_deregister` | Move a service card to the mesh trash |
| `service_list` | List service cards, merging configured peers |
| `service_probe` | Check one card's liveness and status |
| `service_send` | Deliver a message to a service, forwarding to its peer gateway |
| `service_inbox` | Read messages delivered to a service |
| `service_heartbeat` | Refresh liveness and optionally report a status |
| `service_start` | Ask a service to start its command |

Skills found under `<notes>/MCP/Skills` are also exposed as zero-argument tools.

## HTTP API

The MCP service exposes generic REST capabilities on the same port:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness |
| `POST` | `/api/summarize` | Summarize text with an instruction |
| `POST` | `/api/read` | Fetch a URL as Markdown |
| `POST` | `/api/llm/chat` | Run one LLM turn |
| `POST` | `/api/memory/add_knowledge` | Add a cold-layer knowledge entry |
| `GET` | `/api/memories/recent` | List recent memories |
| `POST` | `/api/memories/processed` | Update memory processing state |
| `POST` | `/api/memories/status` | Set lifecycle status |
| `POST` | `/api/project/notes/prewarm` | Pre-index a project's notes |
| `POST` | `/api/consolidation/run` | Run consolidation, optionally dry-run |
| `GET` | `/api/retrieval/stats` | Retrieval counters |
| `POST` | `/api/retrieval/rebuild` | Rebuild a vector index |
| `GET` | `/api/retrieval/rebuild/status` | Rebuild progress |
| `POST` | `/api/retrieval/diagnose` | Diagnose retrieval for a query |
| `POST` | `/api/retrieval/calibrate` | Calibrate relevance floors |

The token proxy adds:

| Method | Path | Purpose |
| --- | --- | --- |
| Any | `/v1/{path}` | Anthropic/OpenAI-compatible forwarding |
| Any | `/open/{path}` | Legacy alias |
| Any | `/raw/{path}` | Transparent forwarding without rewrites |
| `GET` | `/health` | Liveness |
| `GET` | `/usage/summary` | Token usage summary |
| `GET` | `/usage/tools` | Per-tool call counts |
| `GET` | `/usage/layers` | Request-level usage records |

### Service mesh

The same port serves a small service registry under `/mesh/*`, so agents can find and message each other across processes and machines. Cards are JSON files under `server.mesh.dataDir`; a card without a heartbeat for `heartbeatTtlSeconds` expires but stays readable until deregistered.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/mesh/register` | Register or update a card (loopback only) |
| `POST` | `/mesh/deregister` | Move a card to `.trash` (loopback only) |
| `POST` | `/mesh/send` | Deliver a message, forwarding to a peer when owned elsewhere |
| `POST` | `/mesh/heartbeat` | Refresh liveness and optional status |
| `GET` | `/mesh/services` | List cards, merging reachable peers |
| `GET` | `/mesh/services/{id}` | Read one card |
| `GET` | `/mesh/health` | Mesh liveness |

The eight `service_*` tools wrap this API. Delivery is best-effort: failures return `target_offline` (with `startable`) or `peer_unreachable`, and the caller decides whether to retry. `server.mesh.peers[].name` must equal the peer's own `server.mesh.gatewayId`. When `server.mcp.bind` is not loopback, `server.mesh.token` is required: REST, MCP and `/mesh/*` all demand `Authorization: Bearer <token>`, and the server refuses to start without one.

```yaml
server:
  mcp:
    bind: 127.0.0.1
  mesh:
    enabled: true
    gatewayId: gw-local
    dataDir: ~/.config/utility/mesh
    heartbeatTtlSeconds: 120
    peers:
      - name: gw-b
        baseUrl: http://10.0.0.2:9001
```

## Configuration

`agent-gateway` reads `$GATEWAY_HOME/agent-gateway.yaml` unless `GATEWAY_CONFIG`
points to another file. An environment variable always wins over the YAML value;
the YAML value wins over the built-in default. A legacy host `config.yaml` shape
is translated for one release (`GATEWAY_CONFIG_FORMAT=legacy` forces it).

| Variable | Purpose |
| --- | --- |
| `GATEWAY_HOME` | State root; defaults to `~/.config/agent-gateway` |
| `GATEWAY_CONFIG` | Explicit config file |
| `GATEWAY_CACHE` | Cache root; defaults to `~/.cache/agent-gateway` |
| `GATEWAY_PROJECT_DIR` | Project state dir name; defaults to `.agent-gateway` |
| `GATEWAY_LANG`, `GATEWAY_LOG_MAX_SIZE`, `GATEWAY_LOG_BACKUP_COUNT` | Language and log rotation |
| `MCP_PORT`, `MODEL_PROXY_PORT` | Service ports |
| `QDRANT_PATH`, `FASTEMBED_CACHE_PATH`, `EMBEDDER_MODEL` | Vector storage and embedder |
| `AI_API_KEY`, `AI_LLM_TIMEOUT`, `SUMMARY_MODEL`, `SUMMARY_API_URL` | Model access |
| `RETRIEVAL_TRACE`, `RETRIEVAL_TRACE_DAYS` | Retrieval tracing |
| `GATEWAY_SEARCH_*` | Engine list, order, providers and runtime limits |

`UTILITY_DATA_DIR`, `UTILITY_LANG`, `UTILITY_LOG_*` and `UTILITY_USER_ID` are
accepted as deprecated aliases for one release.

Main `agent-gateway.yaml` sections:

| Section | Contents |
| --- | --- |
| `llm` | API key, base URL, model, timeout |
| `language`, `logging` | UI/prompt language and log rotation |
| `server.mcp`, `server.proxy` | Enable switches, ports, providers, output filter, tool-output dedup |
| `memory`, `notes` | Qdrant, embedder, memory limits, note paths/rules |
| `search` | Engine list, order, per-engine endpoints and rate limits |
| `knowledge`, `summary`, `graph` | Product modules and project graph settings |

`memory.neuron` configures the associative network: `enabled`, `build` and
`maintenance` gates, spread `maxDepth` / `beam` / `activationFloor`, tier retention
`archiveDays` / `trashDays`, `llmPairs` per daily build, and `reviewConcurrency` for
parallel LLM review batches.

`memory.brain` holds gateway-native switches (`pairMode` defaults to v3, `sparseFloor`,
`attractor`, `forgetting`, `buffer`, `replay`, `semantic`); the host persists `mcp.brain`
into `agent-gateway.yaml`, so edit the user `config.yaml` and restart. `neurons.centroid` must
share the active `EMBEDDER_MODEL` space: after switching models, run
`scripts/memory_centroids.py` and then `memory_brain_backfill.py --sparse --force`;
cosine across different models is meaningless.

`contracts/env.json` is generated from the implementation and lists every
environment variable the package reads. `contracts/entries.json` lists the module
entry points a host can start. Consumers can validate against these files instead of
scanning source across repositories.

## Development

```bash
pip install -e ".[proxy,memory,server]"
pip install ruff build

ruff format --check src tests scripts
ruff check src tests scripts
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .
python -m build
agent-gateway --check
```

The same commands run in GitHub Actions. Enable the local hooks once:

```bash
git config core.hooksPath .githooks
```

`pre-commit` runs formatting and lint checks; `commit-msg` enforces
`type(scope): summary`.

## Project layout

```text
src/agent_gateway/     package source
  cli/                 command line and contract export
  core/                config, paths, logging, lifecycle constants
  llm/                 single model-call exit point
  services/            MCP, memory, retrieval, graph, search, read, proxy, summarize
  scanner/             AST symbol extractor and JS helper
  prompts/             prompt templates per language
  locales/             UI strings per language
contracts/             generated JSON contracts for host integration
scripts/               maintenance and evaluation scripts
tests/                 unittest suite
```

## Contributing

Issues and pull requests are welcome. Keep changes focused, add tests for behavior
changes, and run the development commands above before opening a pull request.

## License

MIT. See [LICENSE](LICENSE).
