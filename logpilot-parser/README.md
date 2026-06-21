# LogPilot Parser — High-Performance Rust Log Parsing Engine

A native Rust crate that replaces the Python hot path in LogPilot's log
parsing pipeline, delivering 8–15x faster parsing, significantly lower
memory usage, and a clean staged-loading API.

## Architecture

```
┌─────────────────────────────────────────────────┐
│ Stage 1: scan_log_stage1()                      │
│ mmap → line split → timestamp + level + offset  │
│ Returns: Vec<LineInfo> + ScanStats              │
│ Replaces: log_indexer.py::build_index()          │
├─────────────────────────────────────────────────┤
│ Stage 2: parse_log_range()                      │
│ Read byte range → full field extraction          │
│ Returns: Vec<ParseResult>                       │
│ Replaces: log_parser.py::_single_pass_scan()     │
├─────────────────────────────────────────────────┤
│ Stage 3: hydrate_log_detail()                    │
│ Single line → deep parse + categorize           │
│ Returns: ParseResult                            │
│ Replaces: analyzers/pattern_analyzer.py          │
└─────────────────────────────────────────────────┘
```

## Quick Start

### 1. Prerequisites

```bash
# Rust toolchain (1.85+)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Python 3.10+ with maturin
pip install maturin
```

### 2. Build Python bindings

```bash
cd logpilot-parser
maturin develop --release
```

### 3. Use from Python

```python
import logpilot_parser

# Stage 1: Fast scan
result = logpilot_parser.scan_log_stage1_py("path/to/file.log")
print(f"Scanned {result['stats']['total_lines']} lines")
print(f"Level distribution: {result['stats']['level_distribution']}")

# Stage 2: Parse a range
parsed = logpilot_parser.parse_log_range_py("path/to/file.log", 1, 100)

# Stage 3: Deep parse one line
detail = logpilot_parser.hydrate_log_detail_py("ERROR: disk full at /dev/sda1")

# Full single-pass (drop-in replacement for _single_pass_scan)
result = logpilot_parser.full_single_pass_py(log_text, max_error_lines=30)
```

## Module Structure

| Module | Purpose |
|--------|---------|
| `types.rs` | Core types: `LineInfo`, `LogLevel`, `ScanResult`, `ParseResult`, `TimeRange`, `ErrorCategory` |
| `scanner.rs` | Stage 1: mmap-based file scan with byte-offset tracking |
| `timestamp.rs` | Timestamp detection: ISO 8601, syslog, Unix epoch (7 formats) |
| `level.rs` | Aho-Corasick log level detection (FATAL→TRACE) |
| `filters.rs` | Filtering by level, keyword, time range |
| `parser.rs` | Stage 2/3: full field extraction, error categorization (22 categories) |
| `python_bindings.rs` | PyO3 bindings for Python interoperability |
| `tauri_commands.rs` | Tauri IPC command handlers for desktop integration |

## Performance

| Benchmark | Python | Rust | Speedup |
|-----------|--------|------|---------|
| Stage 1 scan (100k lines) | ~1200ms | ~80ms | ~15x |
| Timestamp extraction (1M ops) | ~450ms | ~35ms | ~13x |
| Level detection (1M ops) | ~280ms | ~22ms | ~12x |
| Full single pass (10k lines) | ~180ms | ~18ms | ~10x |

## Running Tests

```bash
# Rust unit tests
cargo test --no-default-features

# With Python bindings
cargo test --features python-bindings

# Benchmarks
cargo bench --bench parse_benchmarks

# Python correctness tests
pytest tests/test_rust_parser_correctness.py -v
```

## Feature Flags

| Feature | Description |
|---------|-------------|
| `python-bindings` | Build PyO3 extension module (default) |
| `tauri-commands` | Build Tauri IPC command handlers |
| `full` | Enable all features |

## Compatibility

The Rust parser maintains behavioral compatibility with the Python
reference implementation:

- Timestamp parsing: 7 formats, identical logic to `log_indexer.py`
- Level detection: 7 levels + UNKNOWN, case-insensitive
- Error categorization: 22 categories matching `pattern_analyzer.py`
- Platform detection: 10 CI/CD platforms
- Byte offset tracking: identical sequential offset semantics

If a format cannot be parsed by Rust, the Python integration layer
(`logpilot_rust/__init__.py`) provides a seamless fallback.

## License

MIT
