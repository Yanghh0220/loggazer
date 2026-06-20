"""
LogPilot Plugin System

Three-layer plugin pipeline:
    InputPlugin → [ProcessorPlugin, ...] → OutputPlugin

Usage:
    from plugins import PluginRegistry, PipelineExecutor, InputPlugin, ProcessorPlugin, OutputPlugin
"""

from plugins.interfaces import (
    # Metadata
    PluginMetadata,
    PluginType,
    # Pipeline record
    LogRecord,
    # Interfaces (ABC + Protocol dual definition)
    InputPlugin,
    ProcessorPlugin,
    OutputPlugin,
    # Registry
    PluginRegistry,
    # Executor
    PipelineExecutor,
    PipelineResult,
    PipelineError,
    ErrorPolicy,
)

__all__ = [
    "PluginMetadata",
    "PluginType",
    "LogRecord",
    "InputPlugin",
    "ProcessorPlugin",
    "OutputPlugin",
    "PluginRegistry",
    "PipelineExecutor",
    "PipelineResult",
    "PipelineError",
    "ErrorPolicy",
]
