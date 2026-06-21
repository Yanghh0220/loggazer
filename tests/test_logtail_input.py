"""
Tests for LogtailInputPlugin — integration with the Go logtail daemon.

These tests verify:
- Line parsing (tab-separated format)
- Subprocess lifecycle (spawn, graceful shutdown, forced kill)
- Error handling (binary not found)
"""

import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the plugins package is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

from plugins.inputs.logtail_input import (
    LogtailInputPlugin,
    LogtailInputConfig,
    _find_logtail_binary,
)
from plugins.interfaces import LogRecord


class TestLineParsing:
    """Unit tests for logtail output line parsing."""

    def test_parse_valid_line(self):
        line = "/var/log/app.log\t42\terror: something failed"
        record = LogtailInputPlugin._parse_line(line)
        assert record is not None
        assert record.content == "error: something failed"
        assert record.metadata["file_path"] == "/var/log/app.log"
        assert record.metadata["byte_offset"] == 42
        assert record.metadata["source"] == "logtail"

    def test_parse_tab_in_content(self):
        """Content itself may contain tabs."""
        line = "/tmp/log\t0\tcol1\tcol2\tcol3"
        record = LogtailInputPlugin._parse_line(line)
        assert record is not None
        assert record.content == "col1\tcol2\tcol3"
        assert record.metadata["file_path"] == "/tmp/log"

    def test_parse_missing_fields(self):
        assert LogtailInputPlugin._parse_line("only_one_field") is None
        assert LogtailInputPlugin._parse_line("") is None

    def test_parse_invalid_offset(self):
        line = "/tmp/log\tnot_a_number\tcontent"
        record = LogtailInputPlugin._parse_line(line)
        assert record is not None
        assert record.metadata["byte_offset"] == -1

    def test_parse_multiple_path_separators(self):
        """Windows paths with drive letters."""
        line = "C:\\logs\\app.log\t100\tsome content"
        record = LogtailInputPlugin._parse_line(line)
        assert record is not None
        assert record.metadata["file_path"] == "C:\\logs\\app.log"
        assert record.metadata["byte_offset"] == 100


class TestBinaryDetection:
    """Tests for logtail binary auto-detection."""

    def test_env_var_priority(self, monkeypatch, tmp_path):
        fake_binary = tmp_path / "fake-logtail"
        fake_binary.write_text("#!/bin/sh\necho fake")
        fake_binary.chmod(0o755)

        monkeypatch.setenv("LOGTAIL_BINARY", str(fake_binary))
        result = _find_logtail_binary()
        assert result == str(fake_binary)

    def test_env_var_nonexistent(self, monkeypatch):
        monkeypatch.setenv("LOGTAIL_BINARY", "/nonexistent/logtail")
        # Should fall through to PATH / project lookup.
        result = _find_logtail_binary()
        # May be None if nothing on PATH.
        assert result is None or result != "/nonexistent/logtail"

    def test_path_fallback(self, monkeypatch):
        monkeypatch.delenv("LOGTAIL_BINARY", raising=False)
        result = _find_logtail_binary()
        # Should not raise; result depends on environment.
        assert result is None or isinstance(result, str)


class TestConfig:
    """Tests for LogtailInputConfig."""

    def test_defaults(self):
        cfg = LogtailInputConfig(files=["/var/log/test.log"])
        assert cfg.checkpoint_dir == ".logtail-checkpoints"
        assert cfg.poll_interval_sec == 5.0
        assert cfg.flush_interval_sec == 5.0
        assert cfg.start_at_end is False
        assert cfg.max_line_size == 64 * 1024

    def test_custom_values(self):
        cfg = LogtailInputConfig(
            files=["/a.log", "/b.log"],
            checkpoint_dir="/tmp/ckpts",
            poll_interval_sec=2.0,
            start_at_end=True,
        )
        assert len(cfg.files) == 2
        assert cfg.checkpoint_dir == "/tmp/ckpts"
        assert cfg.poll_interval_sec == 2.0
        assert cfg.start_at_end is True


class TestPluginLifecycle:
    """Integration-style tests for plugin lifecycle (mocking subprocess)."""

    @pytest.mark.asyncio
    async def test_close_before_fetch(self):
        """close() should be safe before fetch() is called."""
        plugin = LogtailInputPlugin(files=["/tmp/test.log"])
        await plugin.close()  # should not raise

    @pytest.mark.asyncio
    async def test_fetch_binary_not_found(self):
        """fetch() should raise PluginError when binary is missing."""
        plugin = LogtailInputPlugin(
            config=LogtailInputConfig(
                files=["/tmp/test.log"],
                binary_path="/nonexistent/logtail-binary",
            )
        )
        with pytest.raises(Exception):  # PluginError or FileNotFoundError
            async for _ in plugin.fetch():
                pass

    @pytest.mark.asyncio
    async def test_fetch_with_mock_subprocess(self):
        """Test fetch() with a mock subprocess that emits valid lines."""
        plugin = LogtailInputPlugin(files=["/tmp/test.log"])

        # Create a mock process.
        mock_proc = MagicMock()
        mock_proc.returncode = None

        # Simulate stdout with a few valid lines then EOF.
        async def mock_readline():
            lines = [
                b"/tmp/test.log\t0\tline one\n",
                b"/tmp/test.log\t9\tline two\n",
                b"/tmp/test.log\t18\tline three\n",
                b"",  # EOF
            ]
            for line in lines:
                yield line

        line_iter = mock_readline()

        async def readline():
            try:
                return await anext(line_iter)
            except StopAsyncIteration:
                return b""

        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = readline
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")

        with patch(
            "plugins.inputs.logtail_input._find_logtail_binary",
            return_value="/usr/local/bin/logtail",
        ):
            with patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ):
                records = []
                async for record in plugin.fetch():
                    records.append(record)

                assert len(records) == 3
                assert records[0].content == "line one"
                assert records[0].metadata["byte_offset"] == 0
                assert records[1].content == "line two"
                assert records[1].metadata["byte_offset"] == 9
                assert records[2].content == "line three"

    @pytest.mark.asyncio
    async def test_graceful_shutdown_sigterm(self):
        """Test that close() sends SIGTERM and waits."""
        plugin = LogtailInputPlugin(files=["/tmp/test.log"])

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.send_signal = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")

        plugin._process = mock_proc
        plugin._running = True

        await plugin.close()

        mock_proc.send_signal.assert_called_with(signal.SIGTERM)
        mock_proc.wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_forced_kill_after_timeout(self):
        """If SIGTERM doesn't work, send SIGKILL."""
        plugin = LogtailInputPlugin(files=["/tmp/test.log"])

        async def slow_wait():
            await asyncio.sleep(999)  # never finishes within timeout
            return 0

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.send_signal = MagicMock()
        mock_proc.wait = slow_wait
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")

        plugin._process = mock_proc
        plugin._running = True

        await plugin.close()

        # Should have sent both signals.
        assert mock_proc.send_signal.call_count >= 2
        signals_sent = [call.args[0] for call in mock_proc.send_signal.call_args_list]
        assert signal.SIGTERM in signals_sent
        assert signal.SIGKILL in signals_sent
