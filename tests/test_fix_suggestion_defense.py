"""Test fix suggestion HTML stripping and rendering defense layers."""
import pytest
import sys
sys.path.insert(0, '.')
from app import _strip_html, _html_escape, _normalize_fix_suggestions, _build_fix_suggestions_html


class TestStripHtml:
    def test_plain_text_passes_through(self):
        assert _strip_html('降级 react 到 17.x') == '降级 react 到 17.x'

    def test_strips_div_tags(self):
        assert _strip_html('<div class="fix-desc">方案描述</div>') == '方案描述'

    def test_strips_br_tags(self):
        result = _strip_html('text<br/>more')
        assert 'text' in result
        assert 'more' in result

    def test_strips_code_tags(self):
        assert _strip_html('<code>npm install</code>') == 'npm install'

    def test_handles_empty(self):
        assert _strip_html('') == ''

    def test_handles_none(self):
        assert _strip_html(None) == ''

    def test_handles_double_escaped_entities(self):
        result = _strip_html('&lt;div&gt;test&lt;/div&gt;')
        assert '<div' not in result
        assert 'test' in result

    def test_collapses_whitespace(self):
        assert _strip_html('  hello   world  ') == 'hello world'


class TestNormalizeFixSuggestions:
    def test_clean_data_passes_through(self):
        result = {'fix_suggestions': [
            {'title': '降级 react', 'description': '方案描述', 'command': 'npm install',
             'riskLevel': 'warning', 'riskLabel': '谨慎', 'recommended': False}
        ]}
        normalized = _normalize_fix_suggestions(result)
        assert len(normalized) == 1
        assert normalized[0]['title'] == '降级 react'

    def test_html_in_fields_stripped(self):
        result = {'fix_suggestions': [
            {'title': '<div>降级 react</div>',
             'description': '<div class="fix-desc">方案描述</div>',
             'command': '<code>npm install</code>',
             'riskLevel': 'warning', 'riskLabel': '谨慎', 'recommended': False}
        ]}
        normalized = _normalize_fix_suggestions(result)
        assert normalized[0]['title'] == '降级 react'
        assert normalized[0]['description'] == '方案描述'
        assert normalized[0]['command'] == 'npm install'

    def test_old_format_migrated_and_stripped(self):
        result = {'fix_suggestions': [
            {'title': '<span>fix</span>', 'description': 'desc',
             'command': '', 'safety_level': 'safe'}
        ]}
        normalized = _normalize_fix_suggestions(result)
        assert normalized[0]['title'] == 'fix'
        assert normalized[0]['riskLevel'] == 'safe'
        assert normalized[0]['riskLabel'] == '安全'

    def test_html_string_fallback(self):
        result = {'fix_suggestions': (
            '<div class="fix-list">'
            '<div class="fix-item">'
            '<div class="fix-header">'
            '<span class="fix-num">1</span>'
            '<span class="fix-title">降级 react</span>'
            '<span class="fix-risk">谨慎</span>'
            '</div>'
            '<div class="fix-desc">方案描述</div>'
            '<div class="fix-command"><code>npm install</code></div>'
            '</div></div>'
        )}
        normalized = _normalize_fix_suggestions(result)
        assert len(normalized) >= 1
        assert normalized[0]['title'] == '降级 react'

    def test_empty_suggestions(self):
        assert _normalize_fix_suggestions({}) == []
        assert _normalize_fix_suggestions({'fix_suggestions': []}) == []

    def test_missing_title_field(self):
        result = {'fix_suggestions': [{'riskLevel': 'safe', 'riskLabel': '安全'}]}
        normalized = _normalize_fix_suggestions(result)
        assert normalized[0]['title'] == ''


class TestBuildFixSuggestionsHtml:
    def test_no_raw_html_in_output(self):
        suggestions = [
            {'title': '<div>fix</div>', 'description': '<span>desc</span>',
             'command': '', 'riskLevel': 'safe', 'riskLabel': '安全', 'recommended': False}
        ]
        html = _build_fix_suggestions_html(suggestions)
        assert '&lt;div&gt;' not in html
        assert '&lt;span&gt;' not in html

    def test_css_defense_present(self):
        suggestions = [
            {'title': 'fix', 'description': 'desc', 'command': '',
             'riskLevel': 'safe', 'riskLabel': '安全', 'recommended': False}
        ]
        html = _build_fix_suggestions_html(suggestions)
        assert 'min-width:4em' in html
        assert 'word-break:break-word' in html
        assert 'overflow-wrap:break-word' in html

    def test_short_title_renders(self):
        suggestions = [
            {'title': '修复', 'description': '测试', 'command': '',
             'riskLevel': 'safe', 'riskLabel': '安全', 'recommended': True}
        ]
        html = _build_fix_suggestions_html(suggestions)
        assert '修复' in html

    def test_long_mixed_language_title(self):
        suggestions = [
            {'title': '降级 react 到 17.x 以匹配 testing-library 的 peer dependency 要求',
             'description': '', 'command': '',
             'riskLevel': 'warning', 'riskLabel': '谨慎', 'recommended': False}
        ]
        html = _build_fix_suggestions_html(suggestions)
        assert '降级 react 到 17.x' in html

    def test_command_rendered(self):
        suggestions = [
            {'title': 'fix', 'description': 'desc', 'command': 'npm install react',
             'riskLevel': 'safe', 'riskLabel': '安全', 'recommended': False}
        ]
        html = _build_fix_suggestions_html(suggestions)
        assert 'npm install react' in html
        assert 'fix-command' in html

    def test_no_indentation_code_block_issue(self):
        """Ensure f-string indentation doesn't create markdown code blocks."""
        suggestions = [
            {'title': 'test', 'description': 'desc', 'command': '',
             'riskLevel': 'safe', 'riskLabel': '安全', 'recommended': False}
        ]
        html = _build_fix_suggestions_html(suggestions)
        # Check that each line that starts a new HTML element does NOT have
        # leading whitespace that would trigger markdown code blocks
        for line in html.split('\n'):
            if line.strip().startswith('<div') or line.strip().startswith('<span'):
                # Line should not have 4+ leading spaces
                leading = len(line) - len(line.lstrip())
                assert leading < 4, (
                    f'Line may trigger markdown code block '
                    f'(4+ spaces indent): "{line[:60]}..."'
                )

    def test_empty_title_fallback(self):
        suggestions = [
            {'title': '', 'description': 'desc', 'command': '',
             'riskLevel': 'safe', 'riskLabel': '安全', 'recommended': False}
        ]
        html = _build_fix_suggestions_html(suggestions)
        # Should render "方案" as fallback
        assert '方案' in html
