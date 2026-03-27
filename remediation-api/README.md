# Remediation API

FastAPI service that runs the **CrashLoop remediation workflow** via shared `remediation_workflow.py`. **No `uv run` at request time** unless you opt into the legacy subprocess mode.

## Architecture

- **Default**: in-process tool dispatch (`InProcessOpenShiftToolCaller` → `openshift_tool_handlers.MCP_TOOL_DISPATCH`) — same behaviour as `server-gpt.py` tools without MCP wire protocol.
- **Remote MCP (OpenShift)**: set **`REMEDIATION_MCP_URL`** to the streamable HTTP endpoint of `server-gpt.py`, e.g.  
  `http://mcp-server.<namespace>.svc.cluster.local:9000/mcp`  
  The API then uses **`FastMcpToolCaller`** + **`fastmcp.Client(StreamableHttpTransport(...))`** so all tool calls go through real MCP (see `app/services/remediation_runner.py`).
- **Legacy subprocess**: `REMEDIATION_USE_SUBPROCESS=1` on **`/start`** runs  
  `uv run python client-gpt.py server-gpt.py --workflow remediate --approve` (stdio MCP).

## Why SSE?

- One-way server → browser log streaming.
- Browsers use `EventSource`; server returns `text/event-stream`.

## Run locally

From repo root:

```bash
cd remediation-api
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Ensure `REMEDIATION_PROJECT_ROOT` points at the **basic-mcp** repo root if imports fail (defaults are resolved from `app/services/repo_path.py`).

## Environment

| Variable                                           | Description                                                                                          |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `REMEDIATION_PROJECT_ROOT`                         | Absolute path to **basic-mcp** (contains `openshift_tool_handlers.py`).                              |
| `REMEDIATION_MCP_URL`                              | If set, use FastMCP HTTP client to this URL (streamable HTTP `/mcp`); otherwise in-process dispatch. |
| `REMEDIATION_MCP_BEARER_TOKEN`                     | Optional `Authorization: Bearer …` for the MCP HTTP endpoint.                                        |
| `REMEDIATION_MCP_HTTP_HEADERS_JSON`                | Optional extra headers JSON object (overrides bearer if both set for Authorization).                 |
| `REMEDIATION_USE_SUBPROCESS`                       | `1` / `true` → legacy CLI subprocess instead of in-process.                                          |
| `REMEDIATION_API_PORT`                             | Used by `python -m app.main` (default `8787`).                                                       |
| `KUBECONFIG`, `GRANITE_*`, `OPENAI_*`, `LLM_MODEL` | Same as CLI / cluster access.                                                                        |

## API

| Method | Path                                  | Description                                                                                                                                            |
| ------ | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `GET`  | `/api/remediation/health`             | Liveness                                                                                                                                               |
| `GET`  | `/api/remediation/status`             | `{ idle, activeSessionId }`                                                                                                                            |
| `POST` | `/api/remediation/start`              | Start run (in-process by default); returns `{ sessionId }` or **409**                                                                                  |
| `POST` | `/api/remediation/execute`            | JSON body: **`approved`** / **`dry_run`**, optional **`allow_system_namespaces`**, **`include_openshift_namespaces`**, `namespace`, `pod`; then stream |
| `GET`  | `/api/remediation/stream/{sessionId}` | SSE: JSON events                                                                                                                                       |

### `POST /api/remediation/execute` body

Requires `approved: true` **or** `dry_run: true`.

```json
{
  "approved": true,
  "dry_run": false,
  "include_openshift_namespaces": false,
  "namespace": null,
  "pod": null,
  "use_llm": false,
  "model": null
}
```

## Event protocol (SSE `data:` JSON)

```json
{ "type": "status", "state": "running", "timestamp": "..." }
{ "type": "log", "level": "info", "message": "...", "timestamp": "..." }
{ "type": "step", "step": "listing_pods", "status": "in_progress", "timestamp": "..." }
{ "type": "result", "success": true, "exitCode": 0, "summary": "...", "timestamp": "..." }
```

## Concurrency

Only **one** remediation run at a time (global lock). A second start returns **409**.
