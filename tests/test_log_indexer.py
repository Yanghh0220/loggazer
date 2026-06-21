# tests/test_log_indexer.py — 日志索引机制测试
#
# 测试覆盖:
#   1. 索引构建 (build_index)
#   2. 索引加载 (load_index)
#   3. 索引验证 (validate_index)
#   4. 索引失效检测 (mtime/size/fingerprint change)
#   5. 原子写入 (temp file cleanup)
#   6. 时间戳提取 (extract_timestamp_us)
#   7. 日志级别检测 (detect_level)
#   8. 文件指纹 (compute_fingerprint)
#   9. 按偏移读取原始行 (read_lines_by_offset)
#   10. 主入口 open_log 的各种路径
#   11. 损坏索引恢复
#   12. 空文件和超大文件边界条件
#   13. 与 log_parser 集成 (parse_log_from_file, get_index_info)

import hashlib
import os
import tempfile
import time
from pathlib import Path

import pytest

# Skip entire module if pyarrow is not installed
pyarrow = pytest.importorskip("pyarrow", reason="pyarrow not installed")
import pyarrow.parquet as pq  # noqa: E402 — explicit submodule import required
pa = pyarrow
pd = pytest.importorskip("pandas", reason="pandas not installed")


# ============================================================
# 测试辅助函数
# ============================================================


def _make_sample_log(num_lines: int = 100, with_timestamps: bool = True) -> str:
    """生成样本日志内容。"""
    lines = []
    base_ts = 1705300000_000_000  # 2024-01-15 10:00:00 UTC (microseconds)

    levels = ["DEBUG", "INFO", "INFO", "INFO", "WARN", "WARN", "ERROR", "FATAL"]
    messages = [
        "Starting application v{}.{}.{}",
        "Connecting to database at {}:{}",
        "Request processed in {}ms",
        "Cache miss for key {}",
        "Connection pool size: {}",
        "Retry attempt {}/{} for endpoint {}",
        "Failed to parse config file {}",
        "Unexpected error: {}",
    ]

    for i in range(num_lines):
        level = levels[i % len(levels)]
        msg_template = messages[i % len(messages)]
        msg = msg_template.format(i, i % 10, i % 100)

        if with_timestamps:
            ts_us = base_ts + i * 1_000_000  # 1 second apart
            # ISO 8601 format
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc)
            ts_str = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ts_us % 1_000_000:06d}Z"
            lines.append(f"{ts_str} [{level}] {msg}")
        else:
            lines.append(f"[{level}] {msg}")

    return "\n".join(lines) + "\n"


def _write_temp_log(content: str, suffix: str = ".log") -> str:
    """将日志内容写入临时文件，返回文件路径。"""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# ============================================================
# 测试: 时间戳提取
# ============================================================


class TestTimestampExtraction:
    def test_iso8601_with_z(self):
        from datetime import datetime, timezone
        from log_indexer import extract_timestamp_us
        line = "2024-01-15T10:30:45.123456Z [INFO] Server started"
        ts = extract_timestamp_us(line)
        assert ts is not None
        # 动态计算期望值 (避免时区/平台差异)
        expected_dt = datetime(2024, 1, 15, 10, 30, 45, 123456, tzinfo=timezone.utc)
        expected = int(expected_dt.timestamp() * 1_000_000)
        assert abs(ts - expected) < 2000  # 2ms tolerance for float precision

    def test_iso8601_with_timezone(self):
        from log_indexer import extract_timestamp_us
        line = "2024-01-15T10:30:45+08:00 [INFO] Server started"
        ts = extract_timestamp_us(line)
        assert ts is not None

    def test_iso_space_separator(self):
        from log_indexer import extract_timestamp_us
        line = "2024-01-15 10:30:45,123 [ERROR] Something failed"
        ts = extract_timestamp_us(line)
        assert ts is not None

    def test_syslog_format(self):
        from log_indexer import extract_timestamp_us
        line = "Jan 15 10:30:45 hostname app[123]: Started"
        ts = extract_timestamp_us(line)
        assert ts is not None

    def test_unix_seconds(self):
        from log_indexer import extract_timestamp_us
        line = "1705312245 [INFO] Message"
        ts = extract_timestamp_us(line)
        assert ts is not None
        assert ts == 1705312245_000000

    def test_unix_milliseconds(self):
        from log_indexer import extract_timestamp_us
        line = "1705312245123 [INFO] Message"
        ts = extract_timestamp_us(line)
        assert ts is not None
        assert ts == 1705312245123_000

    def test_no_timestamp(self):
        from log_indexer import extract_timestamp_us
        line = "Just a plain log message without timestamp"
        ts = extract_timestamp_us(line)
        assert ts is None

    def test_out_of_range_unix_seconds(self):
        from log_indexer import extract_timestamp_us
        # 9999999999 is year ~2286 — within range
        line = "4102444800 [INFO] Edge case"  # year ~2100 — still valid
        ts = extract_timestamp_us(line)
        # 4102444800 is 2100-01-01, should be accepted
        assert ts is not None


# ============================================================
# 测试: 日志级别检测
# ============================================================


class TestLevelDetection:
    def test_error_level(self):
        from log_indexer import detect_level
        assert detect_level("[ERROR] Something failed") == "ERROR"
        assert detect_level("ERROR: database connection lost") == "ERROR"

    def test_warn_level(self):
        from log_indexer import detect_level
        assert detect_level("[WARN] Disk usage high") == "WARN"
        assert detect_level("WARNING: deprecated API used") == "WARN"

    def test_info_level(self):
        from log_indexer import detect_level
        assert detect_level("[INFO] Server started on port 8080") == "INFO"

    def test_debug_level(self):
        from log_indexer import detect_level
        assert detect_level("[DEBUG] Request payload: { }") == "DEBUG"

    def test_fatal_level(self):
        from log_indexer import detect_level
        assert detect_level("[FATAL] Out of memory") == "FATAL"

    def test_multiple_levels_first_match(self):
        from log_indexer import detect_level
        # FATAL is checked before ERROR
        assert detect_level("FATAL ERROR: system crash") == "FATAL"

    def test_unknown_level(self):
        from log_indexer import detect_level
        assert detect_level("Just a plain message") == "UNKNOWN"


# ============================================================
# 测试: 文件指纹
# ============================================================


class TestFingerprint:
    def test_fingerprint_small_file(self):
        from log_indexer import compute_fingerprint
        content = "Hello World\n"
        path = _write_temp_log(content)
        try:
            fp = compute_fingerprint(path)
            assert len(fp) == 64  # SHA256 hex digest
            # Read back the actual bytes the file contains (handles platform line endings)
            with open(path, "rb") as f:
                actual_bytes = f.read()
            expected_fp = hashlib.sha256(actual_bytes).hexdigest()
            assert fp == expected_fp
        finally:
            os.unlink(path)

    def test_fingerprint_large_file(self):
        from log_indexer import compute_fingerprint
        content = "x" * 20000
        path = _write_temp_log(content)
        try:
            fp = compute_fingerprint(path)
            assert len(fp) == 64
            # Should use head+tail sampling
        finally:
            os.unlink(path)

    def test_fingerprint_changes_with_content(self):
        from log_indexer import compute_fingerprint
        path1 = _write_temp_log("Content A\n" * 1000)
        path2 = _write_temp_log("Content B\n" * 1000)
        try:
            fp1 = compute_fingerprint(path1)
            fp2 = compute_fingerprint(path2)
            assert fp1 != fp2
        finally:
            os.unlink(path1)
            os.unlink(path2)


# ============================================================
# 测试: 索引构建
# ============================================================


class TestBuildIndex:
    def test_build_basic(self):
        from log_indexer import build_index
        log_content = _make_sample_log(500)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, stats = build_index(source_path)

            assert os.path.isfile(idx_path)
            assert stats["total_lines"] == 500
            assert stats["lines_with_timestamp"] == 500
            assert stats["timestamp_coverage"] == 1.0
            assert stats["file_size_mb"] > 0
            assert stats["index_size_mb"] > 0
            assert stats["build_duration_ms"] > 0
            assert "ERROR" in stats["level_distribution"]
            assert stats["time_range_min_us"] is not None
            assert stats["time_range_max_us"] is not None

            # 验证 Parquet 文件可读
            table = pq.read_table(idx_path)
            assert table.num_rows == 500
            assert set(table.column_names) == {
                "timestamp_us", "level", "byte_offset",
                "line_number", "line_length", "message_preview",
            }

            # 验证元数据
            raw_meta = table.schema.metadata
            assert b"schema_version" in raw_meta
            assert raw_meta[b"schema_version"] == b"1"
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_build_without_timestamps(self):
        from log_indexer import build_index
        log_content = _make_sample_log(100, with_timestamps=False)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, stats = build_index(source_path)
            assert stats["total_lines"] == 100
            assert stats["lines_with_timestamp"] == 0
            assert stats["timestamp_coverage"] == 0.0
            assert stats["time_range_min_us"] is None
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_build_empty_file(self):
        from log_indexer import build_index
        source_path = _write_temp_log("")
        try:
            idx_path, stats = build_index(source_path)
            assert stats["total_lines"] == 0
            # Empty files should still produce a valid index
            assert os.path.isfile(idx_path)
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_build_single_line(self):
        from log_indexer import build_index
        source_path = _write_temp_log("2024-01-15T10:00:00Z [INFO] Single line\n")
        try:
            idx_path, stats = build_index(source_path)
            assert stats["total_lines"] == 1
            # Load and check
            from log_indexer import load_index
            df = load_index(idx_path)
            assert len(df) == 1
            assert df.iloc[0]["level"] == "INFO"
            assert df.iloc[0]["line_number"] == 1
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_atomic_write_no_temp_leftover(self):
        from log_indexer import build_index
        log_content = _make_sample_log(50)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, _ = build_index(source_path)
            # 确保没有 .tmp 文件残留
            tmp_files = list(Path(os.path.dirname(idx_path)).glob("*.tmp.*"))
            for tf in tmp_files:
                if os.path.basename(source_path) in os.path.basename(str(tf)):
                    pytest.fail(f"Temp file left behind: {tf}")
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)


# ============================================================
# 测试: 索引加载
# ============================================================


class TestLoadIndex:
    def test_load_valid_index(self):
        from log_indexer import build_index, load_index
        log_content = _make_sample_log(200)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, _ = build_index(source_path)
            df = load_index(idx_path)

            assert len(df) == 200
            assert "timestamp_us" in df.columns
            assert "level" in df.columns
            assert "byte_offset" in df.columns
            assert "line_number" in df.columns
            assert "line_length" in df.columns
            assert "message_preview" in df.columns

            # 检查数据类型
            assert df["timestamp_us"].dtype == "int64"
            assert df["byte_offset"].dtype == "int64"
            assert df["line_number"].dtype == "int32"
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_load_missing_index(self):
        from log_indexer import load_index
        with pytest.raises(FileNotFoundError):
            load_index("/nonexistent/path.loggazer")

    def test_load_corrupt_index(self):
        from log_indexer import load_index
        # Create a file that is not valid parquet
        path = _write_temp_log("not a parquet file")
        try:
            with pytest.raises(ValueError, match="Failed to load index"):
                load_index(path)
        finally:
            os.unlink(path)


# ============================================================
# 测试: 索引验证
# ============================================================


class TestValidateIndex:
    def test_valid_index(self):
        from log_indexer import build_index, validate_index
        log_content = _make_sample_log(100)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, _ = build_index(source_path)
            result = validate_index(source_path, idx_path)
            assert result.is_valid
            assert "valid" in result.reason.lower()
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_missing_source_file(self):
        from log_indexer import validate_index
        result = validate_index("/nonexistent/source.log", "/nonexistent/index.loggazer")
        assert not result.is_valid
        assert "does not exist" in result.reason.lower()

    def test_size_changed(self):
        from log_indexer import build_index, validate_index
        log_content = _make_sample_log(50)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, _ = build_index(source_path)

            # 修改文件大小
            with open(source_path, "a") as f:
                f.write("New line appended\n")

            result = validate_index(source_path, idx_path)
            assert not result.is_valid
            assert "size" in result.reason.lower()
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_mtime_changed(self):
        from log_indexer import build_index, validate_index
        log_content = _make_sample_log(50)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, _ = build_index(source_path)

            # 修改 mtime (touch)
            time.sleep(0.1)  # 确保 mtime 有变化
            os.utime(source_path, (time.time(), time.time() + 10))

            result = validate_index(source_path, idx_path)
            assert not result.is_valid
            assert "mtime" in result.reason.lower()
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_content_changed_same_size(self):
        from log_indexer import build_index, validate_index
        log_content = "A" * 100 + "\n" + "B" * 100 + "\n"
        source_path = _write_temp_log(log_content)
        try:
            idx_path, _ = build_index(source_path)

            # 在中间修改内容，保持大小不变
            with open(source_path, "r+") as f:
                f.seek(50)
                f.write("X")  # 替换一个字符

            # 恢复 mtime (因为内容变了但我们需要测试仅指纹变化)
            # 实际上 mtime 已经变了，所以这个测试验证的是 mtime + fingerprint 的双重检测
            result = validate_index(source_path, idx_path)
            assert not result.is_valid  # mtime changed will catch it
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)


# ============================================================
# 测试: 按偏移读取
# ============================================================


class TestReadLinesByOffset:
    def test_read_lines(self):
        from log_indexer import build_index, load_index, read_lines_by_offset
        log_content = _make_sample_log(50)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, _ = build_index(source_path)
            df = load_index(idx_path)

            # 读取前 3 行
            offsets = [(int(df.iloc[i]["byte_offset"]), int(df.iloc[i]["line_length"]))
                       for i in range(3)]
            lines = read_lines_by_offset(source_path, offsets)
            assert len(lines) == 3
            for line in lines:
                assert line.endswith("\n") or len(line) > 0
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_read_lines_empty_input(self):
        from log_indexer import read_lines_by_offset
        lines = read_lines_by_offset("/nonexistent", [])
        assert lines == []


# ============================================================
# 测试: open_log 主入口
# ============================================================


class TestOpenLog:
    def test_first_open_builds_index(self):
        from log_indexer import open_log, index_path_for
        log_content = _make_sample_log(100)
        source_path = _write_temp_log(log_content)
        try:
            result = open_log(source_path)
            assert result["status"] == "index_built"
            assert os.path.isfile(result["index_path"])
            assert result["stats"]["total_lines"] == 100
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_second_open_hits_index(self):
        from log_indexer import open_log, index_path_for
        log_content = _make_sample_log(100)
        source_path = _write_temp_log(log_content)
        try:
            # First open — build
            result1 = open_log(source_path)
            assert result1["status"] == "index_built"

            # Second open — hit
            result2 = open_log(source_path)
            assert result2["status"] == "index_hit"
            assert result2["stats"]["total_lines"] == 100
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_force_rebuild(self):
        from log_indexer import open_log
        log_content = _make_sample_log(100)
        source_path = _write_temp_log(log_content)
        try:
            # Build index
            open_log(source_path)

            # Force rebuild
            result = open_log(source_path, force_rebuild=True)
            assert result["status"] == "index_rebuilt"
            assert result["stats"]["total_lines"] == 100
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_auto_rebuild_when_invalid(self):
        from log_indexer import open_log
        log_content = _make_sample_log(50)
        source_path = _write_temp_log(log_content)
        try:
            # Build index
            open_log(source_path)

            # Invalidate by appending
            with open(source_path, "a") as f:
                f.write("New line\n")

            # Open — should auto-rebuild
            result = open_log(source_path)
            assert result["status"] == "index_rebuilt"
            assert result["stats"]["total_lines"] == 51
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_missing_source_file(self):
        from log_indexer import open_log
        result = open_log("/nonexistent/path/to/log.log")
        assert result["status"] == "error"
        assert result["index_path"] is None


# ============================================================
# 测试: 与 log_parser 集成
# ============================================================


class TestLogParserIntegration:
    def test_parse_log_from_file_first_time(self):
        from log_parser import parse_log_from_file
        from log_indexer import index_path_for

        log_content = _make_sample_log(100)
        source_path = _write_temp_log(log_content)
        try:
            parsed, index_info = parse_log_from_file(source_path)

            assert parsed.platform != ""  # Will be "Unknown" for synthetic logs
            assert len(parsed.error_lines) >= 0
            assert parsed.truncated_log != ""

            # Index should have been built
            assert index_info["index_status"] in ("index_built", "index_hit")
            assert index_info["index_path"] is not None
            assert os.path.isfile(index_info["index_path"])
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_parse_log_from_file_second_time_fast(self):
        from log_parser import parse_log_from_file

        log_content = _make_sample_log(200)
        source_path = _write_temp_log(log_content)
        try:
            # First parse
            _, info1 = parse_log_from_file(source_path)
            assert info1["index_status"] == "index_built"

            # Second parse — should hit index
            _, info2 = parse_log_from_file(source_path)
            assert info2["index_status"] == "index_hit"
            assert info2["index_stats"]["total_lines"] == 200
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_get_index_info_no_index(self):
        from log_parser import get_index_info
        info = get_index_info("/nonexistent/file.log")
        assert info["has_index"] is False
        assert info["is_valid"] is False

    def test_get_index_info_valid_index(self):
        from log_indexer import build_index
        from log_parser import get_index_info

        log_content = _make_sample_log(50)
        source_path = _write_temp_log(log_content)
        try:
            build_index(source_path)
            info = get_index_info(source_path)
            assert info["has_index"] is True
            assert info["is_valid"] is True
            assert info["stats"]["total_lines"] == 50
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_parse_log_backward_compatible(self):
        """确保原始 parse_log() 接口不受影响。"""
        from log_parser import parse_log
        log_text = "ERROR: Test error\nINFO: Test info\nWARN: Test warning\n"
        result = parse_log(log_text)
        assert result.platform != ""
        assert isinstance(result.error_lines, list)
        assert len(result.truncated_log) > 0


# ============================================================
# 测试: 管理工具
# ============================================================


class TestManagementTools:
    def test_delete_index(self):
        from log_indexer import build_index, delete_index, index_path_for
        log_content = _make_sample_log(30)
        source_path = _write_temp_log(log_content)
        try:
            build_index(source_path)
            assert os.path.isfile(index_path_for(source_path))

            result = delete_index(source_path)
            assert result is True
            assert not os.path.isfile(index_path_for(source_path))
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_delete_nonexistent_index(self):
        from log_indexer import delete_index
        result = delete_index("/nonexistent/file.log")
        assert result is False

    def test_list_indices(self):
        from log_indexer import build_index, list_indices
        log_content = _make_sample_log(20)
        source_path = _write_temp_log(log_content)
        try:
            build_index(source_path)
            dir_path = os.path.dirname(source_path)
            indices = list_indices(dir_path)
            # There should be at least our index
            our_idx = source_path + ".loggazer"
            found = any(i["index_path"] == our_idx for i in indices)
            assert found, f"Our index {our_idx} not found in {indices}"
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)


# ============================================================
# 测试: 损坏索引恢复
# ============================================================


class TestCorruptIndexRecovery:
    def test_corrupt_parquet_triggers_rebuild(self):
        from log_indexer import open_log, index_path_for
        log_content = _make_sample_log(100)
        source_path = _write_temp_log(log_content)
        try:
            # First — build valid index
            result1 = open_log(source_path)
            assert result1["status"] == "index_built"

            # Corrupt the index file
            idx_path = index_path_for(source_path)
            with open(idx_path, "wb") as f:
                f.write(b"this is garbage, not a parquet file")

            # Should rebuild successfully (not crash)
            result2 = open_log(source_path)
            assert result2["status"] in ("index_built", "index_rebuilt")
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_corrupt_index_does_not_crash(self):
        """损坏的索引文件绝不能导致应用崩溃。"""
        from log_indexer import open_log, index_path_for
        log_content = _make_sample_log(10)
        source_path = _write_temp_log(log_content)
        try:
            # Write corrupt index directly
            idx_path = index_path_for(source_path)
            with open(idx_path, "wb") as f:
                f.write(b"\x00\x01\x02\x03corrupt")

            # Should handle gracefully
            result = open_log(source_path)
            assert result["status"] != "error"  # Should rebuild
            assert os.path.isfile(idx_path)  # Replaced with valid index
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_truncated_parquet_file(self):
        """截断的 Parquet 文件应触发重建。"""
        from log_indexer import open_log, index_path_for, build_index
        log_content = _make_sample_log(100)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, _ = build_index(source_path)
            # Truncate the parquet file
            with open(idx_path, "r+b") as f:
                f.truncate(100)  # Keep only first 100 bytes

            result = open_log(source_path)
            assert result["status"] in ("index_built", "index_rebuilt")
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)


# ============================================================
# 测试: 性能特征
# ============================================================


class TestPerformance:
    def test_second_open_is_faster(self):
        """验证重复打开明显快于首次打开。"""
        from log_indexer import open_log
        log_content = _make_sample_log(5000)
        source_path = _write_temp_log(log_content)
        try:
            import time

            # First open
            t0 = time.time()
            result1 = open_log(source_path)
            first_duration = result1["stats"]["build_duration_ms"]

            # Second open
            t1 = time.time()
            result2 = open_log(source_path)
            second_duration = (time.time() - t1) * 1000

            assert result2["status"] == "index_hit"
            # Second open should be substantially faster (at least 5x)
            if first_duration > 50:  # Only if first took enough time to measure
                assert second_duration < first_duration / 5, (
                    f"First: {first_duration:.0f}ms, Second: {second_duration:.0f}ms — "
                    f"expected second to be much faster"
                )
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_index_file_is_smaller_than_source(self):
        """索引文件应比源文件小得多（压缩后）。"""
        from log_indexer import build_index
        log_content = _make_sample_log(2000)
        source_path = _write_temp_log(log_content)
        try:
            _, stats = build_index(source_path)
            source_mb = stats["file_size_mb"]
            index_mb = stats["index_size_mb"]
            # Parquet with zstd should compress well
            # Text log with timestamps + levels compresses nicely
            assert index_mb < source_mb * 0.8, (
                f"Index {index_mb:.2f}MB should be smaller than source {source_mb:.2f}MB"
            )
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)


# ============================================================
# 测试: 元数据读写
# ============================================================


class TestMetadata:
    def test_read_index_metadata(self):
        from log_indexer import build_index, read_index_metadata
        log_content = _make_sample_log(50)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, _ = build_index(source_path)
            meta = read_index_metadata(idx_path)

            assert meta is not None
            assert meta["schema_version"] == "1"
            assert "source_file" in meta
            assert "source_file_size" in meta
            assert "source_file_mtime" in meta
            assert "source_fingerprint" in meta
            assert len(meta["source_fingerprint"]) == 64
            assert meta["total_lines"] == "50"
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)

    def test_load_index_stats(self):
        from log_indexer import build_index, load_index_stats
        log_content = _make_sample_log(50)
        source_path = _write_temp_log(log_content)
        try:
            idx_path, _ = build_index(source_path)
            stats = load_index_stats(idx_path)

            assert stats["total_lines"] == 50
            assert stats["schema_version"] == 1
            assert stats["source_file_size"] > 0
        finally:
            os.unlink(source_path)
            idx = source_path + ".loggazer"
            if os.path.isfile(idx):
                os.unlink(idx)
