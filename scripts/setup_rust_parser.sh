#!/usr/bin/env bash
# setup_rust_parser.sh — Build and install the LogPilot Rust parser
#
# Usage:
#   bash scripts/setup_rust_parser.sh           # Build Python bindings (default)
#   bash scripts/setup_rust_parser.sh --dev     # Development install (fast compile)
#   bash scripts/setup_rust_parser.sh --tauri   # Build Tauri app
#   bash scripts/setup_rust_parser.sh --test    # Run all tests
#   bash scripts/setup_rust_parser.sh --bench   # Run benchmarks
#
# Requirements:
#   - Rust 1.85+ (https://rustup.rs)
#   - Python 3.10+ with pip
#   - (optional) Node.js for Tauri frontend
#   - (optional) VS Build Tools for MSVC on Windows

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PARSER_DIR="$PROJECT_DIR/logpilot-parser"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Check prerequisites
check_rust() {
    if ! command -v rustc &>/dev/null; then
        err "Rust not found. Install from https://rustup.rs"
    fi
    local version
    version=$(rustc --version | grep -oP '\d+\.\d+' | head -1)
    log "Rust version: $version"
}

check_python() {
    if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
        err "Python not found"
    fi
    log "Python: $(python3 --version 2>/dev/null || python --version)"
}

# Build Python bindings
build_python() {
    local mode="$1"  # "release" or "dev"
    log "Building Python bindings ($mode mode)..."

    cd "$PARSER_DIR"

    if ! pip show maturin &>/dev/null; then
        log "Installing maturin..."
        pip install maturin
    fi

    if [ "$mode" = "release" ]; then
        maturin develop --release --features python-bindings
    else
        maturin develop --features python-bindings
    fi

    log "Python bindings installed. Test with:"
    log "  python -c 'import logpilot_parser; print(\"OK\")'"
}

# Run all tests
run_tests() {
    log "Running Rust unit tests..."
    cd "$PARSER_DIR"
    cargo test --no-default-features

    log "Running Python correctness tests..."
    cd "$PROJECT_DIR"
    python -m pytest tests/test_rust_parser_correctness.py -v --tb=short

    log "All tests passed!"
}

# Run benchmarks
run_benchmarks() {
    log "Running Criterion benchmarks..."
    cd "$PARSER_DIR"
    cargo bench --bench parse_benchmarks --no-default-features
}

# Build Tauri desktop app
build_tauri() {
    log "Building Tauri desktop app..."

    if ! command -v node &>/dev/null; then
        warn "Node.js not found. Tauri requires a frontend build."
        warn "Skipping Tauri build. Install Node.js and retry."
        return
    fi

    cd "$PROJECT_DIR"

    # Install Tauri CLI if needed
    if ! cargo install --list | grep -q "tauri-cli"; then
        log "Installing Tauri CLI..."
        cargo install tauri-cli --version "^2"
    fi

    cd src-tauri
    cargo build --release
    log "Tauri app built: target/release/logpilot-desktop"
}

# ============================================================
# Main
# ============================================================

MODE="${1:---dev}"

check_rust

case "$MODE" in
    --dev)
        check_python
        build_python "dev"
        ;;
    --release)
        check_python
        build_python "release"
        ;;
    --test)
        check_python
        build_python "dev"
        run_tests
        ;;
    --bench)
        run_benchmarks
        ;;
    --tauri)
        build_tauri
        ;;
    --all)
        check_python
        build_python "release"
        run_tests
        run_benchmarks
        ;;
    *)
        echo "Usage: $0 [--dev|--release|--test|--bench|--tauri|--all]"
        exit 1
        ;;
esac

log "Done!"
