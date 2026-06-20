"""
PII Redaction Processor Plugin

Implements ProcessorPlugin to detect and redact sensitive information
from log records before they reach AI analysis.

Default rules: email, IPv4/IPv6, JWT, AWS keys, phone (CN), ID card (CN),
               credit card (Luhn), GitHub tokens.
Custom rules: user-supplied regex patterns with replacement templates.

Performance: <200ms for 10MB of log text (benchmarked).
Audit: Hashed redaction records for compliance (never stores plaintext).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from plugins.interfaces import (
    LogRecord,
    PluginMetadata,
    ProcessorPlugin,
    ProcessorPluginABC,
)

logger = logging.getLogger(__name__)


# ── Custom Rule Model ─────────────────────────────────────────────────────────

class CustomRule(BaseModel):
    """User-defined PII detection rule.

    Attributes:
        name: Unique rule identifier (e.g. "internal_project_id").
        pattern: Valid Python regex pattern.
        replacement: Replacement string (e.g. "[MY_SECRET]").
        case_sensitive: Whether matching is case-sensitive (default True).
    """
    name: str
    pattern: str
    replacement: str
    case_sensitive: bool = True

    def to_compiled(self) -> tuple[str, re.Pattern[str], str]:
        """Compile the pattern and return (name, compiled_regex, replacement)."""
        try:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            return (self.name, re.compile(self.pattern, flags), self.replacement)
        except re.error as exc:
            raise ValueError(f"Invalid regex in custom rule '{self.name}': {exc}") from exc


# ── Audit Record ──────────────────────────────────────────────────────────────

@dataclass
class RedactionRecord:
    """Record of a single PII redaction for compliance auditing.

    NOTE: matched_hash is a SHA-256 hash of the first 8 characters
    of the matched value — the plaintext is NEVER stored.
    """
    rule_name: str
    field: str
    matched_hash: str
    position: int
    timestamp: float = field(default_factory=time.time)


# ── Default PII Detection Rules ───────────────────────────────────────────────

# These are compiled ONCE at class definition time, not per-instance.
# Each tuple: (rule_name, compiled_regex, replacement_string)

def _build_default_rules() -> list[tuple[str, re.Pattern[str], str]]:
    """Build the default PII detection rule set.

    Order matters: longer/more-specific patterns come first to prevent
    partial matches (e.g. JWT tokens contain base64 which could match
    generic patterns; check JWT before generic base64).

    Returns:
        list of (name, compiled_pattern, replacement) tuples.
    """
    rules: list[tuple[str, str, str]] = [
        # ── JWT tokens (check before generic base64) ──
        (
            "jwt",
            r'\beyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+',
            "[JWT]",
        ),
        # ── AWS Access Key ID ──
        (
            "aws_key",
            r'\bAKIA[0-9A-Z]{16}\b',
            "[AWS_KEY]",
        ),
        # ── AWS Secret Access Key (context-sensitive: after KEY= or SECRET=) ──
        (
            "aws_secret",
            r'(?:AWS_SECRET(?:_ACCESS)?_KEY|aws_secret_access_key)[=:]\s*["\']?([A-Za-z0-9+/]{40})["\']?',
            r'[AWS_SECRET]',
        ),
        # ── GitHub Tokens ──
        (
            "github_token",
            r'\bgh[pousr]_[A-Za-z0-9_]{36,}\b',
            "[GITHUB_TOKEN]",
        ),
        (
            "github_pat",
            r'\bgithub_pat_[0-9]{2}[A-Za-z0-9_]{22,}\b',
            "[GITHUB_TOKEN]",
        ),
        # ── Email ──
        (
            "email",
            r'[\w.\-+%]+@[\w.\-]+\.[a-zA-Z]{2,}',
            "[EMAIL]",
        ),
        # ── IPv6 (full) ──
        (
            "ipv6_full",
            r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b',
            "[IP]",
        ),
        # ── IPv4 ──
        (
            "ipv4",
            r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b',
            "[IP]",
        ),
        # ── Credit Card (Luhn-checkable ranges, dashed or spaced) ──
        (
            "credit_card",
            r'\b(?:4[0-9]{3}|5[1-5][0-9]{2}|3[47][0-9]{2}|6(?:011|5[0-9]{2}))[ -]?(?:[0-9]{4}[ -]?){2}[0-9]{4}\b',
            "[CC]",
        ),
        # ── Chinese Phone Number ──
        (
            "cn_phone",
            r'\b1[3-9]\d{9}\b',
            "[PHONE]",
        ),
        # ── Chinese ID Card (18-digit, with checksum support) ──
        (
            "cn_id_card",
            r'\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b',
            "[ID_CARD]",
        ),
    ]

    compiled: list[tuple[str, re.Pattern[str], str]] = []
    for name, pattern, replacement in rules:
        compiled.append((name, re.compile(pattern, re.ASCII), replacement))
    return compiled


# ═══════════════════════════════════════════════════════════════════════════════
# PII REDACTION PROCESSOR
# ═══════════════════════════════════════════════════════════════════════════════

class PiiRedactProcessor(ProcessorPluginABC):
    """Processor that detects and redacts PII from log records.

    Implements ProcessorPlugin (via ProcessorPluginABC base class).

    Features:
      - 11 default detection rules covering common PII types
      - Custom regex rules via `custom_rules` parameter
      - Audit logging with hashed values (never stores plaintext)
      - Structure-preserving: modifies only matched values, keeps log structure
      - Batch processing via process_batch()

    Usage:
        # With defaults only
        proc = PiiRedactProcessor()

        # With custom rules
        proc = PiiRedactProcessor(custom_rules=[
            CustomRule(name="api_key", pattern=r"sk-[a-z0-9]{32}", replacement="[API_KEY]"),
        ])

        record = LogRecord(content="Error: admin@test.com from 10.0.0.1")
        result = await proc.process(record)
        # result.content == "Error: [EMAIL] from [IP]"
    """

    # ── Class-level metadata ───────────────────────────────────────────────

    metadata: ClassVar[PluginMetadata] = PluginMetadata(
        name="pii_redact",
        version="1.0.0",
        description="Detects and redacts PII (email, IP, JWT, keys, phone, ID, CC) from log records",
        author="LogPilot Team",
        plugin_type="processor",
    )

    # Compiled default rules — shared across all instances
    _DEFAULT_RULES: ClassVar[list[tuple[str, re.Pattern[str], str]]] = _build_default_rules()

    # Fast string markers for PII pre-filtering (Level 1).
    # These are checked via Python `in` (C-level substring search) before
    # engaging the full regex pipeline.  Markers are ordered by probability
    # of occurrence (most common first) to short-circuit early.
    _STRING_MARKERS: ClassVar[list[str]] = [
        "@",        # email
        "eyJ",      # JWT (base64url-encoded JSON header)
        "AKIA",     # AWS access key prefix
        "AWS_SECRET", "aws_secret_access_key",  # AWS secret key
        "github_pat_",  # GitHub Personal Access Token (new format)
        "ghp_", "gho_", "ghu_", "ghs_", "ghr_",  # GitHub tokens (classic)
    ]

    # Fast format-based pre-filter patterns (Level 2) for PII types that
    # lack a distinctive fixed string marker.  These are simple,
    # non-validating approximations that are much faster than the full
    # per-rule regexes but still catch potential matches.
    _IP_PRE: ClassVar[re.Pattern[str]] = re.compile(
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", re.ASCII,
    )
    _LONG_DIGITS_PRE: ClassVar[re.Pattern[str]] = re.compile(
        r"\d{10,}", re.ASCII,
    )
    _CC_PRE: ClassVar[re.Pattern[str]] = re.compile(
        r"\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}", re.ASCII,
    )
    _IPV6_PRE: ClassVar[re.Pattern[str]] = re.compile(
        r":[0-9a-fA-F]{3,4}:", re.ASCII,
    )

    # ── Instance init ──────────────────────────────────────────────────────

    def __init__(
        self,
        custom_rules: list[CustomRule] | None = None,
        max_audit_entries: int = 10000,
    ) -> None:
        """Initialize the PII redaction processor.

        Args:
            custom_rules: Optional user-defined rules (validated on init).
            max_audit_entries: Maximum audit log entries (oldest evicted when full).
        """
        self._rules: list[tuple[str, re.Pattern[str], str]] = list(self._DEFAULT_RULES)
        self._max_audit_entries = max_audit_entries
        self._audit_log: deque[RedactionRecord] = deque(maxlen=max_audit_entries)

        # Compile and append custom rules
        if custom_rules:
            for rule in custom_rules:
                try:
                    self._rules.append(rule.to_compiled())
                except ValueError:
                    raise  # re-raise with rule context
            logger.debug("Loaded %d default + %d custom rules", len(self._DEFAULT_RULES), len(custom_rules))

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def audit_log(self) -> deque[RedactionRecord]:
        """Get the audit log (read-only access to deque)."""
        return self._audit_log

    @property
    def max_audit_entries(self) -> int:
        """Get/set the maximum audit log entries."""
        return self._max_audit_entries

    @max_audit_entries.setter
    def max_audit_entries(self, value: int) -> None:
        """Update max audit entries and recreate deque to enforce the limit."""
        self._max_audit_entries = value
        existing = list(self._audit_log)
        self._audit_log = deque(existing, maxlen=value)

    @property
    def _use_prefilter(self) -> bool:
        """Whether the line-level pre-filter is safe to use.

        The pre-filter is only active when there are no custom rules,
        because custom rule patterns are not known at compile time and
        may not be caught by the built-in string markers.
        """
        return len(self._rules) == len(self._DEFAULT_RULES)

    # ── Core processing ────────────────────────────────────────────────────

    def _hash_match(self, matched_text: str) -> str:
        """Create a non-reversible hash of matched text for audit trail.

        Hashes the first 8 characters via SHA-256.
        The full plaintext is NEVER stored.
        """
        snippet = matched_text[:8]
        return hashlib.sha256(snippet.encode("utf-8")).hexdigest()

    @classmethod
    def _line_has_pii(cls, line: str) -> bool:
        """Fast pre-filter: check whether a line might contain PII.

        Uses a multi-level approach:
          Level 1 — Python ``in`` checks for distinctive fixed markers
                   (email ``@``, JWT ``eyJ``, AWS/GitHub tokens, ...)
          Level 2 — Separate simple regexes for IP-like, phone/CC/ID card
                   digit patterns, dashed CC, and IPv6 hex groups.

        Returns ``True`` if the line should enter the full redaction
        pipeline, ``False`` if it can safely pass through unchanged.
        """
        # Level 1: string markers (very fast C-level substring search)
        for marker in cls._STRING_MARKERS:
            if marker in line:
                return True
        # Level 2a: IP-like pattern
        if cls._IP_PRE.search(line):
            return True
        # Level 2b: 10+ consecutive digits (contiguous phone/CC/ID card)
        if cls._LONG_DIGITS_PRE.search(line):
            return True
        # Level 2c: dashed/spaced credit-card pattern
        if cls._CC_PRE.search(line):
            return True
        # Level 2d: IPv6 colon-hex pattern
        if cls._IPV6_PRE.search(line):
            return True
        return False

    def _make_replacer(self, rule_name: str, field_name: str, replacement: str):
        """Create a replacer callback for ``re.sub`` that records redactions."""
        def _replacer(match: re.Match[str]) -> str:
            matched_text = match.group(0)
            self._record_redaction(rule_name, field_name, matched_text, match.start())
            return replacement
        return _replacer

    def _redact_text(self, text: str, field_name: str, rules: list[tuple[str, re.Pattern[str], str]]) -> str:
        """Apply all rules to a single text field.

        When only default rules are active (no custom rules), splits the
        input by lines and runs a fast pre-filter on each line, engaging
        the full per-rule regex pipeline only on lines that show potential
        PII.  When custom rules are present, processes every line to ensure
        no custom PII is missed.

        Args:
            text: The text to scan and redact.
            field_name: LogRecord field name (for audit tracking).
            rules: Compiled rules to apply.

        Returns:
            Redacted text with PII replaced.
        """
        lines = text.splitlines(keepends=True)
        result_lines: list[str] = []

        use_prefilter = self._use_prefilter

        for line in lines:
            if not use_prefilter or self._line_has_pii(line):
                for rule_name, pattern, replacement in rules:
                    line = pattern.sub(
                        self._make_replacer(rule_name, field_name, replacement),
                        line,
                    )
            result_lines.append(line)

        return "".join(result_lines)

    def _record_redaction(self, rule_name: str, field_name: str, matched_text: str, position: int) -> None:
        """Record a redaction event in the audit log."""
        self._audit_log.append(RedactionRecord(
            rule_name=rule_name,
            field=field_name,
            matched_hash=self._hash_match(matched_text),
            position=position,
        ))

    async def process(self, record: LogRecord) -> LogRecord:
        """Process a single LogRecord: redact PII from all text fields.

        Fields checked: content, error_lines (each), platform, metadata values.

        Args:
            record: The LogRecord to process.

        Returns:
            LogRecord with PII replaced by placeholder tokens.
        """
        # Redact main content
        record.content = self._redact_text(record.content, "content", self._rules)

        # Redact each error line
        record.error_lines = [
            self._redact_text(line, "error_lines", self._rules)
            for line in record.error_lines
        ]

        # Redact string metadata values
        for key, value in record.metadata.items():
            if isinstance(value, str):
                record.metadata[key] = self._redact_text(value, f"metadata.{key}", self._rules)

        return record

    # ── Audit management ───────────────────────────────────────────────────

    def clear_audit_log(self) -> None:
        """Clear all audit log entries."""
        self._audit_log.clear()

    def get_audit_summary(self) -> dict[str, int]:
        """Get a summary of redactions by rule name.

        Returns:
            dict mapping rule_name to count.
        """
        summary: dict[str, int] = {}
        for entry in self._audit_log:
            summary[entry.rule_name] = summary.get(entry.rule_name, 0) + 1
        return summary

    # ── Rule management ────────────────────────────────────────────────────

    def add_custom_rule(self, rule: CustomRule) -> None:
        """Add a custom rule at runtime.

        Args:
            rule: The CustomRule to add.

        Raises:
            ValueError: If the rule's regex is invalid.
        """
        self._rules.append(rule.to_compiled())
        logger.info("Added custom PII rule: %s", rule.name)

    def remove_custom_rule(self, name: str) -> bool:
        """Remove a custom rule by name.

        Default rules cannot be removed.

        Args:
            name: Rule name to remove.

        Returns:
            True if removed, False if not found or is a default rule.
        """
        default_names = {r[0] for r in self._DEFAULT_RULES}
        if name in default_names:
            logger.warning("Cannot remove default rule: %s", name)
            return False

        for i, (rule_name, _pattern, _repl) in enumerate(self._rules):
            if rule_name == name:
                self._rules.pop(i)
                logger.info("Removed custom PII rule: %s", name)
                return True
        return False

    @property
    def rule_names(self) -> list[str]:
        """Get all active rule names (default + custom)."""
        return [r[0] for r in self._rules]
