// analysis-panel.js - LogGazer Analysis Panel Webview Script
//
// Handles messages from the extension host and renders the analysis result.
// Vanilla JS, no frameworks. Communicates via VS Code Webview API.
//
// v3.0: Fix suggestions use structured rendering with riskLevel/riskLabel/recommended.
// No dangerouslySetInnerHTML pattern — all user content is escaped. CSS-driven styles.

(function () {
    const vscode = acquireVsCodeApi();

    // DOM elements
    const resultContainer = document.getElementById('result-container');
    const loadingContainer = document.getElementById('loading-container');
    const loadingText = document.getElementById('loading-text');

    // ---- Risk level visual config (CSS classes handle styling, inline only for dynamic color) ----
    var RISK_CONFIG = {
        safe:    { color: '#10b981', icon: '🟢', bg: '#ecfdf5' },
        warning: { color: '#f59e0b', icon: '🟡', bg: '#fffbeb' },
        danger:  { color: '#ef4444', icon: '🔴', bg: '#fef2f2' },
    };

    // Listen for messages from the extension host
    window.addEventListener('message', function (event) {
        var message = event.data;

        switch (message.type) {
            case 'loading':
                showLoading(message.message || 'Analyzing...');
                break;

            case 'result':
                hideLoading();
                renderResult(message.data);
                break;

            case 'error':
                hideLoading();
                renderError(message.message || 'Unknown error');
                break;
        }
    });

    function showLoading(text) {
        if (loadingContainer) {
            loadingContainer.style.display = 'block';
            if (loadingText) loadingText.textContent = text;
        }
        if (resultContainer) {
            resultContainer.innerHTML = '';
        }
    }

    function hideLoading() {
        if (loadingContainer) {
            loadingContainer.style.display = 'none';
        }
    }

    function renderResult(response) {
        if (!resultContainer) return;
        resultContainer.innerHTML = formatAnalyzeResponse(response);
    }

    function renderError(message) {
        if (!resultContainer) return;
        resultContainer.innerHTML =
            '<div class="error-display">' +
            '<strong>Analysis Failed</strong><br/>' +
            escapeHtml(message) +
            '</div>';
    }

    /**
     * Copy a command to clipboard with visual feedback.
     */
    window.copyCommand = function (command, btnEl) {
        navigator.clipboard.writeText(command).then(
            function () {
                vscode.postMessage({ command: 'copyCommand', text: command });
                if (btnEl) {
                    var orig = btnEl.textContent;
                    btnEl.textContent = '✓ 已复制';
                    setTimeout(function () { btnEl.textContent = orig; }, 2000);
                }
            },
            function (err) {
                vscode.postMessage({ command: 'alert', text: 'Failed to copy: ' + err });
            }
        );
    };

    // ---- Data compatibility layer ----
    /**
     * Strip HTML tags and decode common entities from text.
     * Defense in depth: even if AI returns HTML in text fields, this cleans it.
     */
    function stripHtml(text) {
        if (!text) return '';
        var cleaned = String(text).replace(/<[^>]*>/g, '');
        cleaned = cleaned.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#039;/g, "'");
        cleaned = cleaned.replace(/<[^>]*>/g, ''); // strip again after entity decode
        cleaned = cleaned.replace(/\s+/g, ' ').trim();
        return cleaned;
    }

    function normalizeFixSuggestions(suggestions) {
        if (!suggestions || !Array.isArray(suggestions)) return [];
        return suggestions.map(function (fix, idx) {
            // Already has new fields
            if (fix.riskLevel && (fix.riskLabel || fix.riskLevel)) {
                // Defense: strip HTML from text fields even in new format
                fix.title = stripHtml(fix.title || '');
                fix.description = stripHtml(fix.description || '');
                fix.command = stripHtml(fix.command || '');
                return fix;
            }
            // Migrate from old safety_level
            var map = {
                safe:      { riskLevel: 'safe',    riskLabel: '安全' },
                review:    { riskLevel: 'warning', riskLabel: '谨慎' },
                dangerous: { riskLevel: 'danger',  riskLabel: '高风险' },
            };
            var mapped = map[fix.safety_level || 'safe'] || { riskLevel: 'safe', riskLabel: '安全' };
            fix.riskLevel = mapped.riskLevel;
            fix.riskLabel = mapped.riskLabel;
            fix.recommended = fix.recommended || false;
            // Defense: strip HTML from all text fields
            fix.title = stripHtml(fix.title || '');
            fix.description = stripHtml(fix.description || '');
            fix.command = stripHtml(fix.command || '');
            return fix;
        });
    }

    /**
     * Format the full AnalyzeResponse into HTML.
     */
    function formatAnalyzeResponse(response) {
        var result = response.result;
        var meta = response.meta;
        var html = '';

        // Severity badge
        html += buildSeverityBadge(result.severity);

        // Metadata
        html += '<details class="meta-details"><summary>📊 Analysis Metadata</summary><div class="meta-grid">';
        html += metaItem('Duration', meta.duration_ms.toFixed(0) + 'ms');
        html += metaItem('Cache', meta.cache_status);
        html += metaItem('Model', escapeHtml(meta.model_used));
        html += metaItem('Platform', escapeHtml(meta.platform_detected));
        html += metaItem('Cost', '$' + meta.cost_usd.toFixed(6));
        html += '</div></details>';

        // Error Summary
        html += '<section class="result-section error-summary"><h2>🔴 Error Summary</h2>';
        html += '<p class="summary-text">' + escapeHtml(result.error_summary) + '</p></section>';

        // Error Detail
        html += '<section class="result-section error-detail"><h2>📝 Key Error</h2>';
        html += '<pre class="error-code"><code>' + escapeHtml(result.error_detail) + '</code></pre></section>';

        // Root Causes
        if (result.root_causes && result.root_causes.length > 0) {
            html += '<section class="result-section root-causes"><h2>🔍 Root Causes</h2>';
            result.root_causes.forEach(function (cause) {
                var pct = Math.max(cause.probability, 2);
                html += '<div class="root-cause">';
                html += '<div class="rc-header">';
                html += '<span class="rc-probability">' + cause.probability + '%</span>';
                html += '<span class="rc-description">' + escapeHtml(cause.description) + '</span>';
                html += '</div>';
                html += '<div class="rc-bar-track"><div class="rc-bar-fill" style="width:' + pct + '%;"></div></div>';
                html += '</div>';
            });
            html += '</section>';
        }

        // ---- Fix Suggestions (v3.0: structured component, no raw HTML) ----
        if (result.fix_suggestions && result.fix_suggestions.length > 0) {
            var normalized = normalizeFixSuggestions(result.fix_suggestions);
            html += '<section class="result-section fix-suggestions"><h2>🛠️ Fix Suggestions</h2>';
            html += buildFixSuggestionList(normalized);
            html += '</section>';
        }

        // Debug Commands
        if (result.debug_commands && result.debug_commands.length > 0) {
            html += '<section class="result-section debug-commands"><h2>🔧 Debug Commands</h2>';
            result.debug_commands.forEach(function (cmd) {
                html += buildCommandBlock(cmd);
            });
            html += '</section>';
        }

        // Prevention
        if (result.prevention && result.prevention.length > 0) {
            html += '<section class="result-section prevention"><h2>🛡️ Prevention Tips</h2><ul>';
            result.prevention.forEach(function (tip) {
                html += '<li>' + escapeHtml(tip) + '</li>';
            });
            html += '</ul></section>';
        }

        // Security Warning
        if (result.security_warning) {
            html += '<section class="result-section security-warning"><h2>⚠️ Security Warning</h2>';
            html += '<p>' + escapeHtml(result.security_warning) + '</p></section>';
        }

        return html;
    }

    function buildSeverityBadge(severity) {
        var config = {
            critical: { icon: '🔴', color: '#dc2626', bg: '#fef2f2', label: 'CRITICAL' },
            high: { icon: '🟠', color: '#ea580c', bg: '#fff7ed', label: 'HIGH' },
            medium: { icon: '🟡', color: '#ca8a04', bg: '#fefce8', label: 'MEDIUM' },
            low: { icon: '🟢', color: '#16a34a', bg: '#f0fdf4', label: 'LOW' },
        };
        var cfg = config[severity] || config.medium;
        return '<div class="severity-badge" style="background:' + cfg.bg + '; border-left:4px solid ' + cfg.color + ';">' +
            '<span class="severity-icon">' + cfg.icon + '</span>' +
            '<span class="severity-label" style="color:' + cfg.color + ';">Severity: ' + cfg.label + '</span></div>';
    }

    // ---- v3.0: Structured Fix Suggestion List (CSS classes, no inline style strings) ----
    function buildFixSuggestionList(suggestions) {
        var html = '<div class="fix-list">';
        for (var i = 0; i < suggestions.length; i++) {
            var item = suggestions[i];
            var riskLevel = item.riskLevel || 'safe';
            var riskCfg = RISK_CONFIG[riskLevel] || RISK_CONFIG.safe;
            var riskLabel = escapeHtml(item.riskLabel || '安全');
            var title = escapeHtml(item.title || '方案');
            var desc = escapeHtml(item.description || '');
            var isRec = item.recommended || false;

            html += '<div class="fix-item">' +
                '<div class="fix-header">' +
                '<span class="fix-num">' + (i + 1) + '</span>' +
                '<span class="fix-title" style="min-width:4em;word-break:break-word;overflow-wrap:break-word;">' + title + '</span>' +
                '<span class="fix-risk" style="color:' + riskCfg.color + ';background:' + riskCfg.bg + ';">' +
                riskCfg.icon + ' ' + riskLabel +
                '</span>' +
                (isRec ? '<span class="fix-recommend-badge">⭐ 推荐</span>' : '') +
                '</div>' +
                '<div class="fix-desc">' + desc + '</div>';

            if (item.command) {
                var escapedCmd = escapeHtml(item.command);
                html += '<div class="fix-command">' +
                    '<code>' + escapedCmd + '</code>' +
                    '<button class="copy-btn fix-copy-btn" onclick="var b=this;copyCommand(' +
                    JSON.stringify(item.command) + ',b);">📋 复制</button>' +
                    '</div>';
            }

            html += '</div>';
        }
        html += '</div>';
        return html;
    }

    function buildCommandBlock(command) {
        var escapedCmd = escapeHtml(command);
        return '<div class="command-block">' +
            '<pre class="command-code"><code>' + escapedCmd + '</code></pre>' +
            '<button class="copy-btn" onclick="var b=this;copyCommand(' +
            JSON.stringify(command) + ',b);">📋 复制</button></div>';
    }

    function metaItem(label, value) {
        return '<div class="meta-item"><span class="meta-label">' + label +
            '</span><span class="meta-value">' + value + '</span></div>';
    }

    function escapeHtml(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }
})();
