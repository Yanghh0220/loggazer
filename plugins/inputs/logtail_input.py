"""
LogtailInputPlugin — Pipe logs from the Go `logtail` daemon into the LogPilot pipeline.

This plugin spawns the Go `logtail` binary as a subprocess and yields LogRecord
instances as they arrive. It handles subprocess lifecycle, graceful shutdown,
and health monitoring.

Usage:
    plugin = LogtailInputPlugin(
        files=["/var/log/app.log"],
        checkpoint_dir=".logtail-checkpoints",
    )
    async for record in plugin.fetch():
        pipeline.process(record)

The Go binary must be built first:
    cd logtail && go build -o logtail ./cmd/logtail/
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, ClassVar

from plugins.interfaces import (
    InputPluginABC,
    LogRecord,
    PluginMetadata,
    PluginError,
)

logger = logging.getLogger(__name__)


def _find_logtail_binary() -> str | None:
    """Locate the logtail Go binary.

    Search order:
        1. LOGTAIL_BINARY environment variable
        2. ./logtail/logtail (relative to project root)
        3. logtail on PATH
    """
    explicit = os.environ.get("LOGTAIL_BINARY")
    if explicit and Path(explicit).is_file():
        return explicit

    # Look in project root's logtail directory.
    project_logtail = Path(__file__).parent.parent.parent / "logtail" / "logtail"
    if project_logtail.is_file():
        return str(project_logtail)

    # Fall back to PATH.
    return shutil.which("logtail")


@dataclass
class LogtailInputConfig:
    """Configuration for the LogtailInputPlugin."""

    files: list[str] = field(default_factory=list)
    checkpoint_dir: str = ".logtail-checkpoints"
    poll_interval_sec: float = 5.0
    flush_interval_sec: float = 5.0
    start_at_end: bool = False
    max_line_size: int = 64 * 1024
    binary_path: str | None = None  # auto-detect if None


class LogtailInputPlugin(InputPluginABC):
    """Input plugin that reads from the Go logtail daemon via a subprocess pipe.

    The Go logtail handles rotation detection, checkpoint management,
    and multi-file support. This plugin wraps the subprocess lifecycle
    and converts logtail's tab-separated output into LogRecord instances.
    """

    metadata: ClassVar[PluginMetadata] = PluginMetadata(
        name="logtail_input",
        version="1.0.0",
        description="Stream logs from the Go logtail daemon with rotation handling",
        author="LogPilot Team",
        plugin_type="input",
    )

    def __init__(self, config: LogtailInputConfig | None = None, **kwargs) -> None:
        """Initialize the plugin.

        Args:
            config: Full configuration object.
            **kwargs: Shorthand for LogtailInputConfig fields
                      (e.g., files=["/var/log/a.log"], checkpoint_dir=".ckpt").
        """
        if config is None:
            config = LogtailInputConfig(**kwargs)
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._running = False

    async def fetch(self) -> AsyncIterator[LogRecord]:
        """Yield LogRecord instances as the logtail daemon produces lines.

        Spawns the logtail subprocess on first iteration and keeps it running
        until the pipeline is done or close() is called.
        """
        if self._running:
            raise PluginError("LogtailInputPlugin.fetch() is not reentrant")

        self._running = True
        try:
            binary = self._config.binary_path or _find_logtail_binary()
            if not binary:
                raise PluginError(
                    "logtail binary not found. Build it with: "
                    "cd logtail && go build -o logtail ./cmd/logtail/"
                )

            args = [binary]
            if self._config.checkpoint_dir:
                args.extend(["--checkpoint-dir", self._config.checkpoint_dir])
            if self._config.poll_interval_sec:
                args.extend(["--poll-interval", f"{self._config.poll_interval_sec}s"])
            if self._config.flush_interval_sec:
                args.extend(["--flush-interval", f"{self._config.flush_interval_sec}s"])
            if self._config.start_at_end:
                args.append("--start-at-end")
            if self._config.max_line_size:
                args.extend(["--max-line-size", str(self._config.max_line_size)])
            args.extend(self._config.files)

            logger.info("Starting logtail: %s", shlex.join(args))

            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Read stderr in background for stats/logging.
            asyncio.create_task(self._read_stderr())

            # Read stdout line by line.
            assert self._process.stdout is not None
            while self._running:
                line = await self._process.stdout.readline()
                if not line:
                    break  # EOF — subprocess exited

                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                record = self._parse_line(decoded)
                if record:
                    yield record

        except asyncio.CancelledError:
            logger.info("LogtailInputPlugin.fetch() cancelled")
        except Exception as exc:
            raise PluginError(f"LogtailInputPlugin error: {exc}") from exc
        finally:
            await self._cleanup()

    async def close(self) -> None:
        """Gracefully shut down the logtail subprocess.

        Sends SIGTERM first, waits for clean exit, then SIGKILL if needed.
        """
        self._running = False
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Stop the subprocess gracefully."""
        if self._process is None:
            return

        proc = self._process
        self._process = None

        if proc.returncode is not None:
            return  # already exited

        try:
            # SIGTERM for graceful shutdown (triggers final checkpoint flush).
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("logtail did not exit after SIGTERM, sending SIGKILL")
                proc.send_signal(signal.SIGKILL)
                await proc.wait()
        except ProcessLookupError:
            pass  # already gone
        except Exception as exc:
            logger.warning("Error during logtail cleanup: %s", exc)

    async def _read_stderr(self) -> None:
        """Read and log stderr from the logtail subprocess."""
        if self._process is None or self._process.stderr is None:
            return
        try:
            while self._running:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text.startswith("# stats:"):
                    logger.debug("logtail %s", text)
                else:
                    logger.warning("logtail stderr: %s", text)
        except Exception:
            pass

    @staticmethod
    def _parse_line(line: str) -> LogRecord | None:
        """Parse a logtail output line into a LogRecord.

        Format: <file_path>\\t<byte_offset>\\t<line_text>
        """
        parts = line.split("\t", 2)
        if len(parts) < 3:
            return None

        file_path, offset_str, text = parts
        try:
            offset = int(offset_str)
        except ValueError:
            offset = -1

        return LogRecord(
            content=text,
            metadata={
                "source": "logtail",
                "file_path": file_path,
                "byte_offset": offset,
            },
        )
