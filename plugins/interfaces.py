"""
Plugin Interfaces for LogPilot

ABC + Protocol dual definition:
  - `@runtime_checkable` Protocol for structural subtyping (no inheritance required)
  - ABC base classes for isinstance() checks and shared utility methods
  - Async-first: all core methods are async def; sync wrappers provided as fallback

Python 3.10+ style type annotations throughout.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import (
    Any,
    AsyncIterator,
    ClassVar,
    Literal,
    Protocol,
    runtime_checkable,
)

logger = logging.getLogger(__name__)


# ── Plugin Type Enum ──────────────────────────────────────────────────────────

PluginType = Literal["input", "processor", "output"]


# ── Error Policy Enum ─────────────────────────────────────────────────────────

class ErrorPolicy(str, Enum):
    """Per-stage error handling strategy."""
    SKIP = "skip"      # log + continue with next record
    RETRY = "retry"    # retry up to max_retries, then skip
    ABORT = "abort"    # stop entire pipeline immediately


# ── Plugin Metadata ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PluginMetadata:
    """Immutable metadata for a registered plugin.

    Attributes:
        name: Unique identifier (e.g. "pii_redact", "stdin_reader").
        version: Semantic version string (e.g. "1.0.0").
        description: One-line human-readable summary.
        author: Contact string (e.g. "name <email>").
        plugin_type: Which interface this plugin implements.
    """
    name: str
    version: str
    description: str
    author: str
    plugin_type: PluginType

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("PluginMetadata.name must not be empty")
        if self.plugin_type not in ("input", "processor", "output"):
            raise ValueError(f"Invalid plugin_type: {self.plugin_type}")


# ── Pipeline Record ───────────────────────────────────────────────────────────

@dataclass
class LogRecord:
    """A single log entry flowing through the pipeline.

    Attributes:
        id: Unique record identifier (UUID4).
        content: The raw or partially-processed log text.
        platform: Detected CI/CD platform (e.g. "GitHub Actions", "Jenkins").
        metadata: Arbitrary key-value annotations attached by processors.
        error_lines: Extracted error lines, if any.
        truncated: Whether the content was truncated from a larger source.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    content: str = ""
    platform: str = "Unknown"
    metadata: dict[str, Any] = field(default_factory=dict)
    error_lines: list[str] = field(default_factory=list)
    truncated: bool = False


# ── Pipeline Result Types ─────────────────────────────────────────────────────

@dataclass
class PipelineError:
    """Record of a single error encountered during pipeline execution."""
    record_id: str
    stage: str          # plugin name where error occurred
    error_type: str     # exception class name
    message: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class PipelineResult:
    """Summary returned by PipelineExecutor.run()."""
    records_total: int = 0
    records_succeeded: int = 0
    records_failed: int = 0
    errors: list[PipelineError] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    @property
    def elapsed_seconds(self) -> float:
        return self.finished_at - self.started_at

    @property
    def success_rate(self) -> float:
        if self.records_total == 0:
            return 1.0
        return self.records_succeeded / self.records_total


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT PLUGIN
# ═══════════════════════════════════════════════════════════════════════════════

@runtime_checkable
class InputPlugin(Protocol):
    """Protocol for log input sources.

    An InputPlugin produces LogRecord instances from an external source
    (stdin, file, HTTP endpoint, S3 bucket, Kafka topic, etc.).

    Implementations must provide `fetch()` and `close()`.
    Structural subtyping: any object with these two async methods
    satisfies the protocol — no inheritance required.
    """

    metadata: ClassVar[PluginMetadata]

    async def fetch(self) -> AsyncIterator[LogRecord]:
        """Yield LogRecord instances one at a time from the source.

        Yields:
            LogRecord: Parsed log record ready for processing.

        Raises:
            PluginError: On unrecoverable source failure.
        """
        ...  # pragma: no cover

    async def close(self) -> None:
        """Release any resources held by the input source.

        Called once when the pipeline finishes or is cancelled.
        Must be idempotent (safe to call multiple times).
        """
        ...  # pragma: no cover


class InputPluginABC(ABC):
    """Abstract base class for InputPlugin implementations.

    Provides a synchronous fallback wrapper and isinstance() support.
    Inherit from this when you want the registry to validate
    interface compliance at registration time.
    """

    metadata: ClassVar[PluginMetadata]

    @abstractmethod
    async def fetch(self) -> AsyncIterator[LogRecord]:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...

    def fetch_sync(self) -> list[LogRecord]:
        """Synchronous fallback: collect all records via asyncio.run()."""
        async def _collect() -> list[LogRecord]:
            records: list[LogRecord] = []
            async for record in self.fetch():
                records.append(record)
            await self.close()
            return records
        return asyncio.run(_collect())


# ═══════════════════════════════════════════════════════════════════════════════
# PROCESSOR PLUGIN
# ═══════════════════════════════════════════════════════════════════════════════

@runtime_checkable
class ProcessorPlugin(Protocol):
    """Protocol for log record processors.

    A ProcessorPlugin transforms LogRecord instances mid-pipeline.
    Processors are chained: the output of one becomes the input of the next.

    Each processor MUST return a valid LogRecord. It may modify fields
    in-place or return a new LogRecord instance.
    """

    metadata: ClassVar[PluginMetadata]

    async def process(self, record: LogRecord) -> LogRecord:
        """Transform a single LogRecord.

        Args:
            record: The LogRecord to process.

        Returns:
            LogRecord: The transformed record (may be same instance or new).
        """
        ...  # pragma: no cover

    async def process_batch(self, records: list[LogRecord]) -> list[LogRecord]:
        """Transform a batch of LogRecords (optional optimization).

        Default implementation fans out via asyncio.gather().
        Override for vectorized/batched processing.

        Args:
            records: List of LogRecords to process.

        Returns:
            list[LogRecord]: Transformed records in same order as input.
        """
        results = await asyncio.gather(
            *(self.process(r) for r in records),
            return_exceptions=True,
        )
        # On exception, return original record unchanged
        out: list[LogRecord] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.warning(
                    "Processor %s failed on record %s: %s",
                    type(self).__name__, records[i].id, result,
                )
                out.append(records[i])
            else:
                out.append(result)
        return out


class ProcessorPluginABC(ABC):
    """Abstract base class for ProcessorPlugin implementations."""

    metadata: ClassVar[PluginMetadata]

    @abstractmethod
    async def process(self, record: LogRecord) -> LogRecord:
        ...

    async def process_batch(self, records: list[LogRecord]) -> list[LogRecord]:
        results = await asyncio.gather(
            *(self.process(r) for r in records),
            return_exceptions=True,
        )
        out: list[LogRecord] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.warning(
                    "Processor %s failed on record %s: %s",
                    type(self).__name__, records[i].id, result,
                )
                out.append(records[i])
            else:
                out.append(result)
        return out


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT PLUGIN
# ═══════════════════════════════════════════════════════════════════════════════

@runtime_checkable
class OutputPlugin(Protocol):
    """Protocol for analysis result sinks.

    An OutputPlugin receives final AnalysisResult objects and writes them
    to a destination (console, Slack, PagerDuty, file, database, etc.).
    """

    metadata: ClassVar[PluginMetadata]

    async def write(self, result: Any) -> None:
        """Emit a single analysis result.

        Args:
            result: An AnalysisResult or other output object to write.
        """
        ...  # pragma: no cover

    async def write_batch(self, results: list[Any]) -> None:
        """Emit a batch of results (optional optimization).

        Default implementation calls write() sequentially.
        Override for batched writes (e.g. bulk insert).

        Args:
            results: List of results to write.
        """
        for result in results:
            await self.write(result)

    async def close(self) -> None:
        """Flush any buffered data and release resources.

        Called once when the pipeline finishes. Must be idempotent.
        """
        ...  # pragma: no cover


class OutputPluginABC(ABC):
    """Abstract base class for OutputPlugin implementations."""

    metadata: ClassVar[PluginMetadata]

    @abstractmethod
    async def write(self, result: Any) -> None:
        ...

    async def write_batch(self, results: list[Any]) -> None:
        for result in results:
            await self.write(result)

    async def close(self) -> None:
        pass  # default: no resources to release


# ═══════════════════════════════════════════════════════════════════════════════
# PLUGIN ERROR
# ═══════════════════════════════════════════════════════════════════════════════

class PluginError(Exception):
    """Base exception for all plugin-related errors."""
    pass


class PluginRegistrationError(PluginError):
    """Raised when a plugin fails validation during registration."""
    pass


class PluginNotFoundError(PluginError):
    """Raised when a requested plugin is not in the registry."""
    pass


class PipelineExecutionError(PluginError):
    """Raised when the pipeline encounters an unrecoverable error."""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# PLUGIN REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

class PluginRegistry:
    """Thread-safe registry for plugin discovery and management.

    Supports:
      - Explicit registration via register()
      - Auto-discovery via setuptools entry points (discover())
      - Type-based lookup via list_by_type()
      - Name-based retrieval via get()

    Usage:
        registry = PluginRegistry()
        registry.register(my_plugin, PluginMetadata(...))
        registry.discover("logpilot.plugins")  # optional auto-discovery
        plugin = registry.get("pii_redact")
    """

    def __init__(self) -> None:
        self._plugins: dict[str, tuple[Any, PluginMetadata]] = {}
        self._lock = RLock()

    # ── Registration ──────────────────────────────────────────────────────

    def register(self, plugin: Any, metadata: PluginMetadata) -> None:
        """Register a plugin instance with its metadata.

        Validates that:
          1. name is unique
          2. plugin satisfies the interface for its declared plugin_type

        Args:
            plugin: Plugin instance (must satisfy Input/Processor/Output protocol).
            metadata: PluginMetadata describing this plugin.

        Raises:
            PluginRegistrationError: If validation fails.
        """
        with self._lock:
            if metadata.name in self._plugins:
                raise PluginRegistrationError(
                    f"Plugin '{metadata.name}' is already registered"
                )

            # Structural type check via Protocol
            if metadata.plugin_type == "input":
                if not isinstance(plugin, InputPlugin):
                    raise PluginRegistrationError(
                        f"Plugin '{metadata.name}' does not satisfy InputPlugin protocol. "
                        f"Missing: async fetch() -> AsyncIterator[LogRecord] or async close() -> None"
                    )
            elif metadata.plugin_type == "processor":
                if not isinstance(plugin, ProcessorPlugin):
                    raise PluginRegistrationError(
                        f"Plugin '{metadata.name}' does not satisfy ProcessorPlugin protocol. "
                        f"Missing: async process(LogRecord) -> LogRecord"
                    )
            elif metadata.plugin_type == "output":
                if not isinstance(plugin, OutputPlugin):
                    raise PluginRegistrationError(
                        f"Plugin '{metadata.name}' does not satisfy OutputPlugin protocol. "
                        f"Missing: async write(result) -> None"
                    )

            self._plugins[metadata.name] = (plugin, metadata)
            logger.info("Registered %s plugin: %s v%s", metadata.plugin_type, metadata.name, metadata.version)

    def unregister(self, name: str) -> None:
        """Remove a plugin from the registry by name.

        Args:
            name: Plugin name to remove.

        Raises:
            PluginNotFoundError: If no plugin with this name is registered.
        """
        with self._lock:
            if name not in self._plugins:
                raise PluginNotFoundError(f"Plugin '{name}' is not registered")
            del self._plugins[name]
            logger.info("Unregistered plugin: %s", name)

    # ── Lookup ────────────────────────────────────────────────────────────

    def get(self, name: str) -> Any:
        """Retrieve a plugin instance by name.

        Args:
            name: Plugin name.

        Returns:
            The registered plugin instance.

        Raises:
            PluginNotFoundError: If not found.
        """
        with self._lock:
            if name not in self._plugins:
                raise PluginNotFoundError(f"Plugin '{name}' is not registered")
            return self._plugins[name][0]

    def get_metadata(self, name: str) -> PluginMetadata:
        """Retrieve plugin metadata by name."""
        with self._lock:
            if name not in self._plugins:
                raise PluginNotFoundError(f"Plugin '{name}' is not registered")
            return self._plugins[name][1]

    def list_by_type(self, plugin_type: PluginType) -> list[PluginMetadata]:
        """List all registered plugins of a given type.

        Args:
            plugin_type: "input", "processor", or "output".

        Returns:
            list[PluginMetadata] sorted by plugin name.
        """
        with self._lock:
            return sorted(
                [meta for _, meta in self._plugins.values() if meta.plugin_type == plugin_type],
                key=lambda m: m.name,
            )

    def list_all(self) -> list[PluginMetadata]:
        """List all registered plugins."""
        with self._lock:
            return sorted(
                [meta for _, meta in self._plugins.values()],
                key=lambda m: m.name,
            )

    def discover(self, entry_point_group: str = "logpilot.plugins") -> int:
        """Auto-discover plugins via setuptools entry points.

        Scans installed packages for entry points declared under the
        given group name in setup.py/pyproject.toml.

        Example setup.py entry:
            entry_points={
                "logpilot.plugins": [
                    "pii_redact = plugins.processors.pii_redact:PiiRedactProcessor",
                ],
            }

        Args:
            entry_point_group: Entry point group name to scan.

        Returns:
            int: Number of new plugins discovered and registered.
        """
        try:
            from importlib.metadata import entry_points
        except ImportError:
            logger.debug("importlib.metadata not available, skipping discovery")
            return 0

        count = 0
        try:
            eps = entry_points(group=entry_point_group)
        except TypeError:
            # Python < 3.12: entry_points() takes no args
            all_eps = entry_points()
            eps = [ep for ep in all_eps if ep.group == entry_point_group]

        for ep in eps:
            try:
                plugin_class = ep.load()
                plugin_instance = plugin_class()
                metadata = getattr(plugin_instance, "metadata", None)
                if metadata is None:
                    logger.warning("Entry point %s has no metadata, skipping", ep.name)
                    continue
                self.register(plugin_instance, metadata)
                count += 1
            except Exception as exc:
                logger.warning("Failed to load plugin from entry point %s: %s", ep.name, exc)

        logger.info("Discovered %d plugins from group '%s'", count, entry_point_group)
        return count

    def __len__(self) -> int:
        with self._lock:
            return len(self._plugins)

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._plugins


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE EXECUTOR
# ═══════════════════════════════════════════════════════════════════════════════

class PipelineExecutor:
    """Orchestrates the full plugin pipeline: Input → Processors → Output.

    The executor:
      1. Fetches LogRecords from the input plugin one at a time
      2. Passes each record through all processor plugins in order
      3. Collects processor outputs and passes them to the output plugin
      4. Handles errors per the configured error_policy

    Usage:
        executor = PipelineExecutor(
            input_plugin=stdin_reader,
            processors=[pii_redact, log_enricher],
            output_plugin=slack_notifier,
            error_policy="skip",
            max_retries=3,
        )
        result = await executor.run()
        print(f"Processed {result.records_succeeded}/{result.records_total} records")
    """

    def __init__(
        self,
        input_plugin: Any,
        processors: list[Any],
        output_plugin: Any,
        error_policy: ErrorPolicy | str = ErrorPolicy.SKIP,
        max_retries: int = 3,
        stage_timeout: float = 30.0,
    ) -> None:
        """Initialize the pipeline executor.

        Args:
            input_plugin: An InputPlugin-compatible instance.
            processors: Ordered list of ProcessorPlugin-compatible instances.
            output_plugin: An OutputPlugin-compatible instance.
            error_policy: How to handle per-record errors ("skip", "retry", "abort").
            max_retries: Max retries when error_policy is "retry".
            stage_timeout: Per-stage timeout in seconds (0 = no timeout).

        Raises:
            PluginRegistrationError: If any plugin fails interface validation.
        """
        # Validate input
        if not isinstance(input_plugin, InputPlugin):
            raise PluginRegistrationError(
                f"Input plugin does not satisfy InputPlugin protocol"
            )
        # Validate processors
        for i, proc in enumerate(processors):
            if not isinstance(proc, ProcessorPlugin):
                raise PluginRegistrationError(
                    f"Processor at index {i} does not satisfy ProcessorPlugin protocol"
                )
        # Validate output
        if not isinstance(output_plugin, OutputPlugin):
            raise PluginRegistrationError(
                f"Output plugin does not satisfy OutputPlugin protocol"
            )

        self.input_plugin = input_plugin
        self.processors = processors
        self.output_plugin = output_plugin
        self.error_policy = ErrorPolicy(error_policy)
        self.max_retries = max_retries
        self.stage_timeout = stage_timeout

    async def run(self) -> PipelineResult:
        """Execute the full pipeline.

        Returns:
            PipelineResult with counts and error details.
        """
        result = PipelineResult()

        try:
            async for record in self.input_plugin.fetch():
                result.records_total += 1
                try:
                    processed = await self._process_record(record, result)
                    if processed is not None:
                        result.records_succeeded += 1
                except PipelineExecutionError:
                    raise
                except Exception as exc:
                    result.records_failed += 1
                    result.errors.append(PipelineError(
                        record_id=record.id,
                        stage="pipeline",
                        error_type=type(exc).__name__,
                        message=str(exc),
                    ))
                    if self.error_policy == ErrorPolicy.ABORT:
                        raise PipelineExecutionError(
                            f"Aborting pipeline after error on record {record.id}: {exc}"
                        ) from exc
        finally:
            await self.input_plugin.close()
            await self.output_plugin.close()

        result.finished_at = time.time()
        logger.info(
            "Pipeline complete: %d/%d succeeded, %d failed (%.1fs)",
            result.records_succeeded, result.records_total,
            result.records_failed, result.elapsed_seconds,
        )
        return result

    async def _process_record(self, record: LogRecord, result: PipelineResult) -> LogRecord | None:
        """Run one record through all processors with error handling."""
        for proc in self.processors:
            proc_name = type(proc).__name__
            for attempt in range(self.max_retries + 1):
                try:
                    if self.stage_timeout > 0:
                        record = await asyncio.wait_for(
                            proc.process(record),
                            timeout=self.stage_timeout,
                        )
                    else:
                        record = await proc.process(record)
                    break  # success, exit retry loop
                except asyncio.TimeoutError:
                    err = PipelineError(
                        record_id=record.id,
                        stage=proc_name,
                        error_type="TimeoutError",
                        message=f"Stage timed out after {self.stage_timeout}s",
                    )
                    if self.error_policy == ErrorPolicy.RETRY and attempt < self.max_retries:
                        logger.warning("Retry %d/%d for %s on %s", attempt + 1, self.max_retries, proc_name, record.id)
                        continue
                    result.errors.append(err)
                    if self.error_policy == ErrorPolicy.ABORT:
                        raise PipelineExecutionError(f"Aborting after timeout in {proc_name}") from None
                    return None
                except Exception as exc:
                    err = PipelineError(
                        record_id=record.id,
                        stage=proc_name,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                    if self.error_policy == ErrorPolicy.RETRY and attempt < self.max_retries:
                        logger.warning("Retry %d/%d for %s on %s: %s", attempt + 1, self.max_retries, proc_name, record.id, exc)
                        continue
                    result.errors.append(err)
                    if self.error_policy == ErrorPolicy.ABORT:
                        raise PipelineExecutionError(f"Aborting after error in {proc_name}: {exc}") from exc
                    return None  # skip this record
        return record


# ── Module-level singleton registry ───────────────────────────────────────────

_default_registry: PluginRegistry | None = None


def get_registry() -> PluginRegistry:
    """Get or create the module-level singleton PluginRegistry."""
    global _default_registry
    if _default_registry is None:
        _default_registry = PluginRegistry()
    return _default_registry
