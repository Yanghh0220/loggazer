# logtail — Production-grade log file follower for LogPilot

`logtail` is a Go daemon that continuously tails log files with production-grade
rotation handling and durable checkpointing for crash-safe resume.

## Features

- **Rotation-safe**: handles rename+create, copytruncate, remove+recreate, and symlink target changes
- **Crash-safe resume**: atomic checkpoint persistence (temp file → fsync → rename) with at-least-once semantics
- **Multi-file support**: tail many files concurrently with isolated state per file
- **File identity tracking**: identifies files by (inode, device) not path, so rotation is handled correctly
- **Hybrid detection**: fsnotify on parent directory + file, plus periodic stat polling as fallback
- **Graceful shutdown**: final checkpoint flush on SIGINT/SIGTERM
- **Lightweight**: only dependency is `fsnotify`

## Installation

```bash
cd logtail
go build -o logtail ./cmd/logtail/
```

## Usage

```bash
# Tail a single file from the beginning
./logtail /var/log/app.log

# Tail multiple files from the end (tail -f behavior)
./logtail --start-at-end /var/log/app.log /var/log/nginx/access.log

# Custom checkpoint directory and poll interval
./logtail --checkpoint-dir /var/lib/logpilot/checkpoints --poll-interval 2s /var/log/*.log
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--checkpoint-dir` | `.logtail-checkpoints` | Directory for checkpoint state |
| `--poll-interval` | `5s` | Stat polling interval for fallback detection |
| `--flush-interval` | `5s` | How often to flush checkpoints to disk |
| `--start-at-end` | `false` | Start reading from end of file (like `tail -f`) |
| `--max-line-size` | `65536` | Maximum line size in bytes |

### Output Format

Each line is printed to stdout as:

```
<file_path>\t<byte_offset>\t<line_text>
```

Stats are printed to stderr every 30s:

```
# stats: files=2 rotations=3 bytes=1048576 errors=0
```

## Rotation Handling

### rename + create (logrotate default)

1. fsnotify detects the rename event on the parent directory
2. The old file descriptor is drained to EOF (no data loss)
3. A new file descriptor is opened at the original path
4. Reading continues from the new file at offset 0

### copytruncate

1. Periodic stat polling detects that file size < current offset (same inode)
2. Offset is reset to 0
3. Reading continues from the beginning of the truncated file

### symlink target change (container environments)

1. Periodic stat polling resolves the symlink
2. If the resolved inode has changed, treat as rotation
3. Drain old fd, open new target

## Checkpoint Semantics

- **At-least-once**: after a crash, the tailer resumes from the last flushed offset.
  A small window of lines may be re-processed — consumers MUST be idempotent.
- **Byte offsets, not line numbers**: precise positioning with `Seek()`.
- **File identity**: (inode, device) is stored alongside path and offset.
  On restart, if the identity doesn't match, the offset is not applied — preventing
  incorrect seeks after rotation.
- **Truncation safety**: if the offset exceeds the current file size, the offset
  is reset to 0 (copytruncate recovery).
- **Atomic writes**: checkpoint is written to a temp file, fsynced, then renamed
  over the target. This prevents corruption from partial writes.

## Integration with LogPilot Python Pipeline

The `logtail` binary outputs lines in a format compatible with the LogPilot
plugin pipeline. You can pipe it directly:

```bash
./logtail /var/log/app.log | python -m logpilot ingest --source stdin
```

Or use the included Python input plugin (`plugins/inputs/logtail_input.py`):

```python
from plugins.inputs.logtail_input import LogtailInputPlugin

plugin = LogtailInputPlugin(
    files=["/var/log/app.log", "/var/log/nginx/access.log"],
    checkpoint_dir="/var/lib/logpilot/checkpoints",
)
```

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                       Manager                             │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐                  │
│  │ Tailer  │  │ Tailer  │  │ Tailer  │  ...              │
│  │ (file1) │  │ (file2) │  │ (file3) │                  │
│  └────┬────┘  └────┬────┘  └────┬────┘                  │
│       │            │            │                         │
│       └────────────┼────────────┘                         │
│                    │ (fan-in)                             │
│               ┌────▼────┐                                 │
│               │ Lines() │  ← merged output channel        │
│               └────┬────┘                                 │
│                    │                                      │
│            ┌───────▼───────┐                              │
│            │  Checkpoint   │  ← atomic, durable           │
│            │    Store      │     temp→fsync→rename        │
│            └───────────────┘                              │
└──────────────────────────────────────────────────────────┘
```

## Testing

```bash
cd logtail
go test ./... -v -race -count=1
```

Key test scenarios:
- Basic file following (start from 0, start from end)
- rename+create rotation (no data loss)
- copytruncate detection
- Checkpoint resume across restarts
- Truncation safety (offset > file size)
- File appears after tailer starts
- Graceful shutdown final checkpoint flush
- Multi-file concurrent tailing
