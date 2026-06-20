# Phase 3 — Architecture Evolution Design

**Date**: 2026-06-20
**Status**: Approved
**Scope**: Plugin Interface Definition (ARCH-001), PII Redaction Processor (ARCH-002), Dockerfile & Docker Compose (ARCH-003)

---

## ARCH-001: Plugin Interface Definition

### Motivation

LogPilot currently has no formal plugin system. The four analyzers in `analyzers/` function as ad-hoc "plugins" — each accepts `(log_text, error_lines)` and returns a `dict`, scheduled via `ThreadPoolExecutor` in `analyzer.py:_run_parallel_analyzers()`. Input and output paths are hardwired: input comes from Streamlit/file upload/API POST, output goes to the API response or UI render.

A formal plugin interface enables:
- Third-party extensions (new log sources, custom processors, different output sinks)
- Loose coupling between pipeline stages
- Independent testability of each component

### Architecture

```
┌──────────────┐     ┌──────────────────────┐     ┌──────────────┐
│ InputPlugin  │ ──> │  ProcessorPlugin[]   │ ──> │ OutputPlugin │
│ (1 required) │     │  (0..N, ordered)     │     │ (1 required) │
└──────────────┘     └──────────────────────┘     └──────────────┘
                           │
                     PipelineExecutor
                     (orchestrates chain)
```

### Interfaces

#### PluginMetadata (dataclass)

```python
@dataclass(frozen=True)
class PluginMetadata:
    name: str           # unique identifier, e.g. "pii_redact"
    version: str        # semver, e.g. "1.0.0"
    description: str    # one-line summary
    author: str         # "name <email>"
    plugin_type: Literal["input", "processor", "output"]
```

#### InputPlugin (Protocol + ABC)

- `async fetch() -> AsyncIterator[LogRecord]` — yield parsed log records one at a time
- `async close() -> None` — cleanup resources
- Provided sync adapter: `fetch_sync() -> Iterator[LogRecord]` via `asyncio.run()`

#### ProcessorPlugin (Protocol + ABC)

- `async process(record: LogRecord) -> LogRecord` — transform a single record; must return a valid LogRecord
- `async process_batch(records: list[LogRecord]) -> list[LogRecord]` — optional batch optimization
- Default batch impl calls `process()` in a `asyncio.gather()` fan-out

#### OutputPlugin (Protocol + ABC)

- `async write(result: AnalysisResult) -> None` — emit one analysis result
- `async write_batch(results: list[AnalysisResult]) -> None` — optional batch optimization
- `async close() -> None` — flush + cleanup

### PluginRegistry

- `register(plugin, metadata: PluginMetadata) -> None` — validate interface compliance, store
- `unregister(name: str) -> None`
- `list_by_type(plugin_type: str) -> list[PluginMetadata]`
- `get(name: str) -> Any` — retrieve by name
- `discover(entry_point_group: str = "logpilot.plugins") -> int` — setuptools entry point auto-discovery
- Thread-safe (RLock)

### PipelineExecutor

```python
class PipelineExecutor:
    def __init__(
        self,
        input_plugin: InputPlugin,
        processors: list[ProcessorPlugin],
        output_plugin: OutputPlugin,
        error_policy: Literal["skip", "retry", "abort"] = "skip",
        max_retries: int = 3,
    ): ...

    async def run() -> PipelineResult: ...
```

- Orchestrates: fetch → [processors] → output
- Per-record error handling: skip (log + continue), retry (N times), abort (stop pipeline)
- Emits `PipelineResult` with counts: `records_total`, `records_succeeded`, `records_failed`, `errors: list[PipelineError]`
- Timeout per stage configurable

### Compatibility Design

- **ABC + Protocol dual definition**: `@runtime_checkable` Protocol for structural subtyping (no inheritance required); ABC base classes for `isinstance()` checks and shared utility methods
- **Sync fallback**: Each async method has a `_sync` sibling that wraps via `asyncio.to_thread()`; `AsyncAdapter` mixin auto-generates sync wrappers
- **Existing code path**: No existing code is broken. The hardwired path in `analyzer.py` continues to work; the plugin pipeline is an alternative entry point invoked via `PipelineExecutor`

### File Layout

```
plugins/
├── __init__.py          # re-exports: PluginMetadata, all interfaces, PluginRegistry, PipelineExecutor
├── interfaces.py        # ABC + Protocol definitions, PluginMetadata, PluginRegistry, PipelineExecutor
├── processors/
│   ├── __init__.py
│   └── pii_redact.py    # ARCH-002
├── inputs/
│   └── __init__.py      # placeholder for future input plugins
└── outputs/
    └── __init__.py      # placeholder for future output plugins
```

---

## ARCH-002: PII Redaction Processor

### Motivation

CI/CD logs frequently contain secrets: leaked API keys, JWT tokens in debug output, email addresses, phone numbers, IP addresses. Before sending logs to an external AI API (DeepSeek/Claude), sensitive values must be redacted. The existing `prompt_sanitizer.py` only handles prompt injection — it does not detect PII.

### Design

`PiiRedactProcessor` implements `ProcessorPlugin`. It is a mid-pipeline processor that runs after log parsing but before AI analysis.

#### Default Detection Rules (compiled once at class level)

| Rule | Pattern | Replacement |
|---|---|---|
| Email | `[\w.\-+%]+@[\w.\-]+\.[a-z]{2,}` | `[EMAIL]` |
| IPv4 | `\b(?:\d{1,3}\.){3}\d{1,3}\b` | `[IP]` |
| IPv6 | `\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b` | `[IP]` |
| JWT | `eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+` | `[JWT]` |
| AWS Key | `\bAKIA[0-9A-Z]{16}\b` | `[AWS_KEY]` |
| AWS Secret | `\b(?=.*[A-Z])(?=.*[a-z])(?=.*\d)[A-Za-z0-9+/]{40}\b` (context: after "AWS_SECRET") | `[AWS_SECRET]` |
| CN Phone | `\b1[3-9]\d{9}\b` | `[PHONE]` |
| CN ID Card | `\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]\|1[0-2])(?:0[1-9]\|[12]\d\|3[01])\d{3}[\dXx]\b` | `[ID_CARD]` |
| Credit Card | `\b(?:\d[ -]*?){13,19}\b` + Luhn check | `[CC]` |
| GitHub Token | `\bgh[pousr]_[A-Za-z0-9_]{36,}\b` | `[GITHUB_TOKEN]` |

#### Custom Rules

```python
class CustomRule(BaseModel):
    name: str
    pattern: str          # valid regex
    replacement: str      # replacement template, e.g. "[MY_SECRET]"
    case_sensitive: bool = True

processor = PiiRedactProcessor(custom_rules=[CustomRule(...)])
```

Custom rules are validated (regex compiles) on registration; `re.error` raises `PluginConfigError`.

#### Audit Log

```python
@dataclass
class RedactionRecord:
    rule_name: str        # which rule matched
    field: str            # which LogRecord field was redacted
    matched_hash: str     # SHA-256(first 8 chars of match) — not plaintext
    position: int         # character offset in field
    timestamp: float      # time.time()

processor.audit_log: deque[RedactionRecord]  # max 10,000 entries
```

Audit log is accessible for compliance reporting. It never stores plaintext matched values — only a salted hash of the first 8 characters.

#### Performance Strategy

- All default patterns pre-compiled into a single `re.Pattern` list at class definition time (not per-instance)
- Single-pass scanning: iterate `(field_name, field_value)` pairs in `LogRecord`, apply all patterns in order, collect replacements
- For 10MB of log text (~100,000 lines), each line is ~100 bytes → ~100K regex applications
- Each regex application on a 100-byte string takes ~1-2µs → total ~100-200ms target achievable
- `process_batch()` uses `asyncio.gather()` for concurrent record processing

#### Integration Point

In the pipeline chain, PiiRedactProcessor is placed as the **first processor**, before any other processor:

```
InputPlugin → [PiiRedactProcessor, ...other processors] → OutputPlugin
```

In the existing hardwired path, it is called after `log_parser.parse_log()` but before `analyzer.analyze_log()`.

---

## ARCH-003: Dockerfile & Docker Compose

### Motivation

LogPilot has no containerization. Docker enables:
- Consistent runtime across dev/staging/production
- Isolation of AI model dependencies
- Easy deployment alongside Qdrant and Redis

### Dockerfile Design

#### Multi-stage Build

```
Stage 1 — builder
├── Base: python:3.11-slim
├── Install: gcc, g++, curl (build deps only)
├── COPY requirements.txt
├── RUN pip install --user --no-cache-dir -r requirements.txt
├── RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
│     → model cached to /root/.cache/torch/sentence_transformers/
└── Size: ~2.5GB (all deps + build tools + model)

Stage 2 — runtime
├── Base: python:3.11-slim (~150MB)
├── COPY --from=builder /root/.local /home/logpilot/.local
├── COPY --from=builder /root/.cache/torch /home/logpilot/.cache/torch
├── COPY . /app
├── RUN useradd -u 1000 logpilot && chown -R logpilot /app /home/logpilot
├── USER logpilot
├── ENV PATH=/home/logpilot/.local/bin:$PATH
├── HEALTHCHECK --interval=30s CMD /app/docker/healthcheck.sh
├── ARG INCLUDE_QDRANT=true
│     → if true: Qdrant runs in-process (default)
│     → if false: Qdrant client only, connects to external
├── EXPOSE 8501 (Streamlit) 8000 (FastAPI)
└── Target size: <3GB
```

#### Key Design Decisions

- **Non-root**: UID 1000, no `sudo`, no package manager access in runtime
- **Model pre-download**: `all-MiniLM-L6-v2` (~90MB) baked into image — zero network at startup
- **Layer caching**: `requirements.txt` COPY before source code — dependency install only re-runs when requirements change
- **INCLUDE_QDRANT ARG**: Controls whether `qdrant-client` runs embedded mode or client-only mode. Default `true` for self-contained deployment
- **HEALTHCHECK**: Shell script checks `curl -f http://localhost:8000/healthz` — FastAPI health endpoint

### docker/healthcheck.sh

```bash
#!/bin/bash
# Check FastAPI health endpoint; exit 0 if healthy, 1 if not
curl -sf http://localhost:8000/healthz || exit 1
```

### docker-compose.yml (Full Stack)

```yaml
services:
  logpilot:
    build: .
    ports: ["8501:8501", "8000:8000"]
    environment:
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
      - QDRANT_URL=http://qdrant:6333
      - REDIS_URL=redis://redis:6379
      - LOGPILOT_CACHE_DIR=/app/.cache
    depends_on: [qdrant, redis]
    volumes:
      - logpilot_cache:/app/.cache
      - ./config.toml:/app/config.toml:ro
    healthcheck:
      test: ["CMD", "/app/docker/healthcheck.sh"]
      interval: 30s

  qdrant:
    image: qdrant/qdrant:latest
    ports: ["6333:6333"]
    volumes: [qdrant_data:/qdrant/storage]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    volumes: [redis_data:/data]

volumes:
  logpilot_cache:
  qdrant_data:
  redis_data:
```

### docker-compose.minimal.yml (No Qdrant/Redis)

```yaml
services:
  logpilot:
    build:
      context: .
      args:
        INCLUDE_QDRANT: "true"   # embedded mode, no external Qdrant needed
    ports: ["8501:8501", "8000:8000"]
    environment:
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
      - QDRANT_MODE=memory        # in-process, no persistence
      - RATE_LIMITER_BACKEND=memory  # no Redis needed
    volumes:
      - logpilot_cache:/app/.cache

volumes:
  logpilot_cache:
```

### .dockerignore

```
.git/
.github/
__pycache__/
*.pyc
.venv/
venv/
env/
.env
.env.local
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
dist/
build/
*.log
.vscode/
.streamlit/secrets.toml
tests/
docs/
scripts/
node_modules/
```

---

## File Manifest

| File | ARCH | Purpose |
|---|---|---|
| `plugins/__init__.py` | 001 | Public API re-exports |
| `plugins/interfaces.py` | 001 | ABC + Protocol definitions, PluginMetadata, PluginRegistry, PipelineExecutor |
| `plugins/processors/__init__.py` | 001 | Processor plugin package |
| `plugins/processors/pii_redact.py` | 002 | PII redaction ProcessorPlugin |
| `plugins/inputs/__init__.py` | 001 | Placeholder for future input plugins |
| `plugins/outputs/__init__.py` | 001 | Placeholder for future output plugins |
| `Dockerfile` | 003 | Multi-stage build |
| `docker-compose.yml` | 003 | Full stack (LogPilot + Qdrant + Redis) |
| `docker-compose.minimal.yml` | 003 | LogPilot only |
| `.dockerignore` | 003 | Build context exclusions |
| `docker/healthcheck.sh` | 003 | Container health probe |

## Testing Strategy

- **ARCH-001**: Unit tests for PluginRegistry (register/unregister/discover), Protocol compliance via `isinstance()` checks, PipelineExecutor with mock plugins
- **ARCH-002**: Parametrized tests for each default rule (known PII → redacted, non-PII → unchanged), performance benchmark (10MB in <200ms), custom rule validation, audit log verification (hash, not plaintext)
- **ARCH-003**: `docker build` dry-run, HEALTHCHECK script returns 0, non-root user confirmed via `docker run --id`, image size check

## Migration & Compatibility

- **No breaking changes**: The existing hardwired analysis path is untouched
- **Plugin pipeline** is an alternative entry point; both paths can coexist
- **PiiRedactProcessor** can be used standalone (without full pipeline) by importing and calling `process()` directly
- **Docker** adds deployment option; existing bare-metal/venv deployment continues to work
