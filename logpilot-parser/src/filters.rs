//! Filtering logic for log lines.
//!
//! Supports filtering by:
//! - Log level (minimum severity threshold)
//! - Keyword (case-insensitive substring match)
//! - Time range (start/end in Unix microseconds)
//!
//! Filters can be chained. The default passes all lines.

use crate::types::{LineInfo, LogLevel, TimeRange};

// ============================================================
// Filter options
// ============================================================

/// Combined filter options for scanning/querying logs.
#[derive(Debug, Clone)]
pub struct FilterOptions {
    /// Minimum log level to include (inclusive). `None` = all levels.
    pub min_level: Option<LogLevel>,
    /// Case-insensitive keyword to search for. `None` = no keyword filter.
    pub keyword: Option<String>,
    /// Time range to restrict to. `None` = no time filter.
    pub time_range: Option<TimeRange>,
    /// Maximum number of lines to return. `None` = no limit.
    pub max_lines: Option<usize>,
}

impl Default for FilterOptions {
    fn default() -> Self {
        Self {
            min_level: None,
            keyword: None,
            time_range: None,
            max_lines: None,
        }
    }
}

impl FilterOptions {
    /// Create a new filter that passes everything.
    pub fn new() -> Self {
        Self::default()
    }

    /// Set a minimum log level.
    pub fn min_level(mut self, level: LogLevel) -> Self {
        self.min_level = Some(level);
        self
    }

    /// Set a keyword filter.
    pub fn keyword(mut self, kw: impl Into<String>) -> Self {
        self.keyword = Some(kw.into());
        self
    }

    /// Set a time range filter.
    pub fn time_range(mut self, start_us: i64, end_us: i64) -> Self {
        self.time_range = Some(TimeRange { start_us, end_us });
        self
    }

    /// Set a maximum line count.
    pub fn max_lines(mut self, n: usize) -> Self {
        self.max_lines = Some(n);
        self
    }
}

// ============================================================
// Filter application
// ============================================================

/// A single-line filter predicate. Returns `true` if the line should be included.
#[derive(Debug, Clone)]
pub struct LineFilter {
    pub min_level: Option<LogLevel>,
    pub keyword: Option<String>,
    pub time_range: Option<TimeRange>,
}

impl LineFilter {
    /// Create a `LineFilter` from `FilterOptions`.
    pub fn from_options(opts: &FilterOptions) -> Self {
        Self {
            min_level: opts.min_level,
            keyword: opts.keyword.clone(),
            time_range: opts.time_range,
        }
    }

    /// Check whether a `LineInfo` passes the filter.
    #[inline]
    pub fn matches_info(&self, info: &LineInfo) -> bool {
        // Level filter
        if let Some(ref min_level) = self.min_level {
            if info.level.severity() < min_level.severity() {
                return false;
            }
        }

        // Time range filter
        if let Some(ref range) = self.time_range {
            if info.timestamp_us != 0 && !range.contains(info.timestamp_us) {
                return false;
            }
        }

        // Keyword filter is applied against the full line text,
        // so we don't filter here — that's done at the scanner level
        true
    }

    /// Check whether a full line text passes the keyword filter.
    #[inline]
    pub fn matches_keyword(&self, line_text: &str) -> bool {
        if let Some(ref kw) = self.keyword {
            line_text.to_lowercase().contains(&kw.to_lowercase())
        } else {
            true
        }
    }

    /// Combined check for both info-level and keyword filters.
    #[inline]
    pub fn matches(&self, info: &LineInfo, line_text: &str) -> bool {
        self.matches_info(info) && self.matches_keyword(line_text)
    }
}

impl From<FilterOptions> for LineFilter {
    fn from(opts: FilterOptions) -> Self {
        Self::from_options(&opts)
    }
}

// ============================================================
// Tests
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::LineInfo;

    fn make_info(level: LogLevel, ts: i64) -> LineInfo {
        LineInfo {
            timestamp_us: ts,
            level,
            byte_offset: 0,
            line_number: 1,
            line_length: 50,
            message_preview: String::new(),
        }
    }

    #[test]
    fn test_level_filter() {
        let filter = LineFilter {
            min_level: Some(LogLevel::Error),
            keyword: None,
            time_range: None,
        };

        assert!(filter.matches_info(&make_info(LogLevel::Error, 0)));
        assert!(filter.matches_info(&make_info(LogLevel::Fatal, 0)));
        assert!(filter.matches_info(&make_info(LogLevel::Critical, 0)));
        assert!(!filter.matches_info(&make_info(LogLevel::Warn, 0)));
        assert!(!filter.matches_info(&make_info(LogLevel::Info, 0)));
        assert!(!filter.matches_info(&make_info(LogLevel::Debug, 0)));
    }

    #[test]
    fn test_time_filter() {
        let filter = LineFilter {
            min_level: None,
            keyword: None,
            time_range: Some(TimeRange {
                start_us: 1000,
                end_us: 2000,
            }),
        };

        assert!(filter.matches_info(&make_info(LogLevel::Info, 1000)));
        assert!(filter.matches_info(&make_info(LogLevel::Info, 1500)));
        assert!(filter.matches_info(&make_info(LogLevel::Info, 2000)));
        assert!(!filter.matches_info(&make_info(LogLevel::Info, 999)));
        assert!(!filter.matches_info(&make_info(LogLevel::Info, 2001)));
    }

    #[test]
    fn test_keyword_filter() {
        let filter = LineFilter {
            min_level: None,
            keyword: Some("error".into()),
            time_range: None,
        };

        assert!(filter.matches_keyword("ERROR: something failed"));
        assert!(filter.matches_keyword("an error occurred"));
        assert!(!filter.matches_keyword("all systems operational"));
    }

    #[test]
    fn test_combined_filter() {
        let filter = FilterOptions::new()
            .min_level(LogLevel::Warn)
            .keyword("disk")
            .time_range(1000, 5000);

        assert_eq!(filter.min_level, Some(LogLevel::Warn));
        assert_eq!(filter.keyword.as_deref(), Some("disk"));
        assert!(filter.time_range.is_some());

        let line_filter = LineFilter::from_options(&filter);
        let info = LineInfo {
            timestamp_us: 3000,
            level: LogLevel::Error,
            byte_offset: 0,
            line_number: 1,
            line_length: 50,
            message_preview: String::new(),
        };

        assert!(line_filter.matches_info(&info));
        assert!(line_filter.matches(&info, "ERROR: disk full"));
    }
}
