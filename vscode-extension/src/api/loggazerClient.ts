// loggazerClient.ts - HTTP client for LogGazer API
//
// Thin wrapper around fetch() for calling the LogGazer FastAPI backend.
// No external HTTP library dependency — uses Node.js built-in fetch (VS Code 1.85+).

export interface AnalyzeRequest {
    log_text: string;
    platform_hint?: string;
    include_rag?: boolean;
    cache_policy?: 'auto' | 'force_refresh' | 'cache_only';
}

export interface AnalyzeResponseMeta {
    duration_ms: number;
    cache_status: 'hit' | 'miss' | 'rag' | 'disabled';
    model_used: string;
    cost_usd: number;
    platform_detected: string;
}

export interface RootCause {
    description: string;
    probability: number;
}

export interface FixSuggestion {
    title: string;
    description: string;
    command: string;
    riskLevel: 'safe' | 'warning' | 'danger';
    riskLabel: string;
    recommended: boolean;
    // [DEPRECATED] 旧字段，向后兼容
    safety_level?: 'safe' | 'review' | 'dangerous';
}

export interface AnalysisResult {
    error_summary: string;
    error_detail: string;
    root_causes: RootCause[];
    fix_suggestions: FixSuggestion[];
    debug_commands: string[];
    severity: 'low' | 'medium' | 'high' | 'critical';
    prevention: string[];
    security_warning?: string;
}

export interface AnalyzeResponse {
    result: AnalysisResult;
    meta: AnalyzeResponseMeta;
    request_id: string;
}

export interface ProblemDetail {
    type: string;
    title: string;
    status: number;
    detail: string;
    instance?: string;
}

export class LogGazerClient {
    /**
     * Analyze a build failure log via the LogGazer API.
     *
     * @param logText - The complete build failure log text
     * @param apiUrl - Base URL of the LogGazer backend (e.g., http://localhost:8000)
     * @param apiKey - Optional API key for cloud mode
     * @param platformHint - Optional platform hint for better analysis
     * @returns The structured analysis response
     * @throws Error with user-friendly message on failure
     */
    static async analyze(
        logText: string,
        apiUrl: string,
        apiKey?: string,
        platformHint?: string,
    ): Promise<AnalyzeResponse> {
        const url = `${apiUrl.replace(/\/$/, '')}/v1/analyze`;

        const body: AnalyzeRequest = {
            log_text: logText,
            platform_hint: platformHint,
            include_rag: true,
            cache_policy: 'auto',
        };

        const headers: Record<string, string> = {
            'Content-Type': 'application/json',
        };
        if (apiKey) {
            headers['X-API-Key'] = apiKey;
        }

        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 180_000); // 3 min timeout

        try {
            const response = await fetch(url, {
                method: 'POST',
                headers,
                body: JSON.stringify(body),
                signal: controller.signal,
            });

            if (response.status === 422) {
                const problem = await response.json() as ProblemDetail;
                throw new Error(`Validation Error: ${problem.detail}`);
            }
            if (response.status === 429) {
                const retryAfter = response.headers.get('Retry-After') || '60';
                throw new Error(`Rate limit exceeded. Please wait ${retryAfter}s and try again.`);
            }
            if (response.status === 503) {
                const problem = await response.json() as ProblemDetail;
                throw new Error(problem.detail || 'Service temporarily unavailable.');
            }
            if (response.status === 401) {
                throw new Error('Authentication failed. Check your LogGazer API Key in VS Code settings.');
            }
            if (!response.ok) {
                const text = await response.text();
                throw new Error(`API Error (${response.status}): ${text.substring(0, 200)}`);
            }

            return await response.json() as AnalyzeResponse;

        } catch (err: unknown) {
            if (err instanceof TypeError && err.message.includes('fetch')) {
                throw new Error(
                    `LogGazer backend is not running at ${apiUrl}.\n\n` +
                    `Run "LogGazer: Start Backend" from the Command Palette (Ctrl+Shift+P)\n` +
                    `or click the LogGazer status bar icon to start it automatically.\n\n` +
                    `To use a remote backend, set loggazer.apiUrl in VS Code settings.`
                );
            }
            if (err instanceof Error && err.name === 'AbortError') {
                throw new Error('Analysis request timed out (180s). The log may be too large or the backend is overloaded.');
            }
            throw err;
        } finally {
            clearTimeout(timeout);
        }
    }

    /**
     * Check if the LogGazer backend is healthy.
     *
     * @param apiUrl - Base URL of the LogGazer backend
     * @returns Health status object or null if unreachable
     */
    static async checkHealth(apiUrl: string): Promise<Record<string, unknown> | null> {
        try {
            const url = `${apiUrl.replace(/\/$/, '')}/v1/health`;
            const response = await fetch(url, {
                method: 'GET',
                signal: AbortSignal.timeout(5000),
            });
            if (!response.ok) {
                return null;
            }
            return await response.json() as Record<string, unknown>;
        } catch {
            return null;
        }
    }
}
