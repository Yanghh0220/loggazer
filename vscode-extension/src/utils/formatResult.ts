// formatResult.ts - Convert AnalysisResult → HTML for Webview
//
// Pure function: takes the API response and returns HTML string.
// Used by the webview panel's JavaScript to render results dynamically.
//
// v3.0: Fix suggestions now use structured rendering — no raw HTML from AI is trusted.
// All user content is escaped. riskLevel/riskLabel/recommended fields drive UI.

import { AnalyzeResponse, AnalysisResult, RootCause, FixSuggestion } from '../api/loggazerClient';

// ---- Risk level visual config ----
const RISK_CONFIG: Record<string, { color: string; icon: string; bg: string }> = {
    safe:    { color: '#10b981', icon: '🟢', bg: '#ecfdf5' },
    warning: { color: '#f59e0b', icon: '🟡', bg: '#fffbeb' },
    danger:  { color: '#ef4444', icon: '🔴', bg: '#fef2f2' },
};

// ---- Old safety_level → riskLevel mapping (backward compat) ----
function mapLegacySafetyLevel(legacy?: string): { riskLevel: string; riskLabel: string } {
    const map: Record<string, { riskLevel: string; riskLabel: string }> = {
        safe:       { riskLevel: 'safe',    riskLabel: '安全' },
        review:     { riskLevel: 'warning', riskLabel: '谨慎' },
        dangerous:  { riskLevel: 'danger',  riskLabel: '高风险' },
    };
    return map[legacy || 'safe'] || { riskLevel: 'safe', riskLabel: '安全' };
}

// ---- Data compatibility layer ----
function normalizeFixSuggestions(suggestions: FixSuggestion[]): FixSuggestion[] {
    if (!suggestions || !Array.isArray(suggestions)) return [];
    return suggestions.map((fix, idx) => {
        // If new fields are present, trust them
        if (fix.riskLevel && fix.riskLabel) {
            return fix;
        }
        // Migrate from old safety_level
        const mapped = mapLegacySafetyLevel(fix.safety_level);
        return {
            ...fix,
            riskLevel: mapped.riskLevel as FixSuggestion['riskLevel'],
            riskLabel: mapped.riskLabel,
            recommended: false,
        };
    });
}

// ---- HTML escape ----
function escapeHtml(text: string): string {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

/**
 * Format an API response into HTML for the analysis panel.
 */
export function formatResultHtml(response: AnalyzeResponse): string {
    const result = response.result;
    const meta = response.meta;
    let html = '';

    // ---- Severity Badge ----
    html += buildSeverityBadge(result.severity);

    // ---- Metadata (collapsible) ----
    html += `
    <details class="meta-details">
        <summary>📊 Analysis Metadata</summary>
        <div class="meta-grid">
            <div class="meta-item">
                <span class="meta-label">Duration</span>
                <span class="meta-value">${meta.duration_ms.toFixed(0)}ms</span>
            </div>
            <div class="meta-item">
                <span class="meta-label">Cache</span>
                <span class="meta-value">${meta.cache_status}</span>
            </div>
            <div class="meta-item">
                <span class="meta-label">Model</span>
                <span class="meta-value">${escapeHtml(meta.model_used)}</span>
            </div>
            <div class="meta-item">
                <span class="meta-label">Platform</span>
                <span class="meta-value">${escapeHtml(meta.platform_detected)}</span>
            </div>
            <div class="meta-item">
                <span class="meta-label">Cost</span>
                <span class="meta-value">$${meta.cost_usd.toFixed(6)}</span>
            </div>
        </div>
    </details>`;

    // ---- Error Summary ----
    html += `
    <section class="result-section error-summary">
        <h2>🔴 Error Summary</h2>
        <p class="summary-text">${escapeHtml(result.error_summary)}</p>
    </section>`;

    // ---- Error Detail ----
    html += `
    <section class="result-section error-detail">
        <h2>📝 Key Error</h2>
        <pre class="error-code"><code>${escapeHtml(result.error_detail)}</code></pre>
    </section>`;

    // ---- Root Causes ----
    if (result.root_causes && result.root_causes.length > 0) {
        html += '<section class="result-section root-causes"><h2>🔍 Root Causes</h2>';
        for (const cause of result.root_causes) {
            html += buildRootCauseBar(cause);
        }
        html += '</section>';
    }

    // ---- Fix Suggestions (v3.0: structured component, no raw HTML) ----
    if (result.fix_suggestions && result.fix_suggestions.length > 0) {
        const normalized = normalizeFixSuggestions(result.fix_suggestions);
        html += '<section class="result-section fix-suggestions"><h2>🛠️ Fix Suggestions</h2>';
        html += buildFixSuggestionList(normalized);
        html += '</section>';
    }

    // ---- Debug Commands ----
    if (result.debug_commands && result.debug_commands.length > 0) {
        html += '<section class="result-section debug-commands"><h2>🔧 Debug Commands</h2>';
        for (const cmd of result.debug_commands) {
            html += buildCopyableCommand(cmd);
        }
        html += '</section>';
    }

    // ---- Prevention Tips ----
    if (result.prevention && result.prevention.length > 0) {
        html += '<section class="result-section prevention"><h2>🛡️ Prevention Tips</h2><ul>';
        for (const tip of result.prevention) {
            html += `<li>${escapeHtml(tip)}</li>`;
        }
        html += '</ul></section>';
    }

    // ---- Security Warning (if any) ----
    if (result.security_warning) {
        html += `
        <section class="result-section security-warning">
            <h2>⚠️ Security Warning</h2>
            <p>${escapeHtml(result.security_warning)}</p>
        </section>`;
    }

    return html;
}

function buildSeverityBadge(severity: string): string {
    const config: Record<string, { icon: string; color: string; bg: string; label: string }> = {
        critical: { icon: '🔴', color: '#dc2626', bg: '#fef2f2', label: 'CRITICAL' },
        high: { icon: '🟠', color: '#ea580c', bg: '#fff7ed', label: 'HIGH' },
        medium: { icon: '🟡', color: '#ca8a04', bg: '#fefce8', label: 'MEDIUM' },
        low: { icon: '🟢', color: '#16a34a', bg: '#f0fdf4', label: 'LOW' },
    };
    const cfg = config[severity] || config.medium;
    return `
    <div class="severity-badge" style="background:${cfg.bg}; border-left: 4px solid ${cfg.color};">
        <span class="severity-icon">${cfg.icon}</span>
        <span class="severity-label" style="color:${cfg.color};">Severity: ${cfg.label}</span>
    </div>`;
}

function buildRootCauseBar(cause: RootCause): string {
    const pct = Math.max(cause.probability, 2);
    return `
    <div class="root-cause">
        <div class="rc-header">
            <span class="rc-probability">${cause.probability}%</span>
            <span class="rc-description">${escapeHtml(cause.description)}</span>
        </div>
        <div class="rc-bar-track">
            <div class="rc-bar-fill" style="width:${pct}%;"></div>
        </div>
    </div>`;
}

// ---- v3.0: Structured Fix Suggestion List (CSS-driven, no inline style strings) ----
function buildFixSuggestionList(suggestions: FixSuggestion[]): string {
    let html = '<div class="fix-list">';
    for (let i = 0; i < suggestions.length; i++) {
        const item = suggestions[i];
        const riskLevel = item.riskLevel || 'safe';
        const riskCfg = RISK_CONFIG[riskLevel] || RISK_CONFIG.safe;
        const riskLabel = escapeHtml(item.riskLabel || '安全');
        const title = escapeHtml(item.title || '方案');
        const desc = escapeHtml(item.description || '');
        const isRec = item.recommended || false;

        html += `
        <div class="fix-item">
            <div class="fix-header">
                <span class="fix-num">${i + 1}</span>
                <span class="fix-title">${title}</span>
                <span class="fix-risk" style="color:${riskCfg.color};background:${riskCfg.bg};">
                    ${riskCfg.icon} ${riskLabel}
                </span>
                ${isRec ? '<span class="fix-recommend-badge">⭐ 推荐</span>' : ''}
            </div>
            <div class="fix-desc">${desc}</div>`;

        if (item.command) {
            const escapedCmd = escapeHtml(item.command);
            html += `
            <div class="fix-command">
                <code>${escapedCmd}</code>
                <button class="copy-btn fix-copy-btn" onclick="copyCommand('${escapedCmd.replace(/'/g, "\\'")}')">📋 复制</button>
            </div>`;
        }

        html += '</div>';
    }
    html += '</div>';
    return html;
}

function buildCopyableCommand(command: string): string {
    const escapedCmd = escapeHtml(command);
    return `
    <div class="command-block">
        <pre class="command-code"><code>${escapedCmd}</code></pre>
        <button class="copy-btn" onclick="copyCommand('${escapedCmd.replace(/'/g, "\\'")}')">
            📋 复制
        </button>
    </div>`;
}
