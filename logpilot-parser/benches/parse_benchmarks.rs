//! Criterion benchmarks for the LogPilot Rust parser.
//!
//! Run with: `cargo bench --bench parse_benchmarks`
//!
//! These benchmarks measure:
//! - Stage 1 scan throughput (lines/sec, MB/sec)
//! - Timestamp extraction performance
//! - Level detection performance
//! - Full single-pass parse performance
//! - Comparison against typical log file sizes

use criterion::{black_box, criterion_group, criterion_main, Criterion, Throughput};
use std::io::Write;
use tempfile::NamedTempFile;

use logpilot_parser::{
    level::detect_level,
    parser::{categorize_error, full_single_pass},
    scanner::scan_log_stage1,
    timestamp::extract_timestamp_us,
};

// ============================================================
// Benchmark fixture: generate a realistic log file
// ============================================================

fn generate_log_file(num_lines: usize) -> NamedTempFile {
    let mut file = NamedTempFile::new().unwrap();

    let templates = [
        "2024-01-15T10:30:45.123Z INFO Server started on port 8080",
        "2024-01-15T10:30:46.000Z DEBUG Initializing connection pool with max_connections=100",
        "2024-01-15T10:30:47.500Z WARN Disk usage at 85%, consider cleanup of /var/log",
        "2024-01-15T10:30:48.000Z ERROR Failed to connect to upstream service: connection refused at 10.0.1.5:9090",
        "2024-01-15T10:30:49.100Z FATAL Critical system failure, shutting down all services",
        "2024-01-15T10:30:50.000Z INFO Shutdown complete, 42 connections closed",
        "Just a plain message without any level or timestamp",
        "2024-01-15T10:30:51.000Z TRACE Entering function process_request with id=12345",
        "2024-01-15T10:30:52.000Z ERROR ImportError: No module named 'requests' in /app/main.py:42",
        "2024-01-15T10:30:53.000Z INFO Build completed successfully in 5.3 seconds",
    ];

    for i in 0..num_lines {
        let template = templates[i % templates.len()];
        writeln!(file, "{} [line={}]", template, i).unwrap();
    }

    file
}

// ============================================================
// Benchmarks
// ============================================================

fn bench_stage1_scan(c: &mut Criterion) {
    let file = generate_log_file(100_000);
    let path = file.path();

    let file_size = std::fs::metadata(path).unwrap().len();

    let mut group = c.benchmark_group("stage1_scan");
    group.throughput(Throughput::Bytes(file_size));
    group.sample_size(20);

    group.bench_function("scan_100k_lines", |b| {
        b.iter(|| {
            scan_log_stage1(black_box(path), None, None).unwrap()
        })
    });

    group.finish();
}

fn bench_timestamp_extraction(c: &mut Criterion) {
    let lines: Vec<&[u8]> = vec![
        b"2024-01-15T10:30:45.123Z INFO Server started",
        b"2024-01-15 10:30:45 ERROR Something failed",
        b"Jan 15 10:30:45 ERROR Disk full",
        b"1705312245 INFO Unix seconds",
        b"1705312245123 INFO Unix milliseconds",
        b"2024/01/15 10:30:45 INFO Slash format",
        b"01-15-2024 10:30:45 INFO US format",
        b"No timestamp here at all",
    ];

    let mut group = c.benchmark_group("timestamp_extraction");
    group.sample_size(200);

    group.bench_function("extract_8_formats", |b| {
        b.iter(|| {
            for line in &lines {
                black_box(extract_timestamp_us(black_box(line)));
            }
        })
    });

    group.finish();
}

fn bench_level_detection(c: &mut Criterion) {
    let lines: Vec<&str> = vec![
        "2024-01-15 FATAL: Out of memory",
        "CRITICAL: Database connection lost",
        "ERROR: File not found",
        "WARN: Disk usage 90%",
        "WARNING: Low memory",
        "INFO: Server started",
        "DEBUG: Variable x = 42",
        "TRACE: Entering function foo",
        "Just a plain message",
        "",
    ];

    let mut group = c.benchmark_group("level_detection");
    group.sample_size(500);

    group.bench_function("detect_10_lines", |b| {
        b.iter(|| {
            for line in &lines {
                black_box(detect_level(black_box(line)));
            }
        })
    });

    group.finish();
}

fn bench_error_categorization(c: &mut Criterion) {
    let lines: Vec<&str> = vec![
        "ImportError: No module named 'foo'",
        "TypeError: expected str, got int",
        "SyntaxError: invalid syntax at line 42",
        "Connection refused: connect to 127.0.0.1:8080",
        "Build failed: error[E0425]: cannot find value",
        "FAILED: test_login",
        "Permission denied: /etc/shadow",
        "npm ERR! ERESOLVE could not resolve dependency",
        "Server started successfully on port 8080",
        "WARN: deprecated API will be removed",
    ];

    let mut group = c.benchmark_group("error_categorization");
    group.sample_size(500);

    group.bench_function("categorize_10_errors", |b| {
        b.iter(|| {
            for line in &lines {
                black_box(categorize_error(black_box(line)));
            }
        })
    });

    group.finish();
}

fn bench_full_single_pass(c: &mut Criterion) {
    // Generate ~1MB of log text
    let file = generate_log_file(10_000);
    let path = file.path();
    let log_text = std::fs::read_to_string(path).unwrap();

    let mut group = c.benchmark_group("full_single_pass");
    group.throughput(Throughput::Bytes(log_text.len() as u64));
    group.sample_size(50);

    group.bench_function("parse_10k_lines", |b| {
        b.iter(|| {
            full_single_pass(black_box(&log_text), 30)
        })
    });

    group.finish();
}

fn bench_large_file_scan(c: &mut Criterion) {
    // Generate a 50MB log file for realistic large-file benchmarks
    let file = generate_log_file(500_000);
    let path = file.path();
    let path_str = path.to_string_lossy().to_string();

    let mut group = c.benchmark_group("large_file");
    group.sample_size(10);

    group.bench_function("scan_500k_lines", |b| {
        b.iter(|| {
            scan_log_stage1(black_box(path.as_ref()), None, None).unwrap()
        })
    });

    group.finish();
}

criterion_group!(
    benches,
    bench_timestamp_extraction,
    bench_level_detection,
    bench_error_categorization,
    bench_full_single_pass,
    bench_stage1_scan,
    bench_large_file_scan,
);
criterion_main!(benches);
