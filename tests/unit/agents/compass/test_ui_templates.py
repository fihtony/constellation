"""Tests for Compass UI templates."""
import pytest
from agents.compass.ui.templates import render_compass_ui, render_task_tab, render_chat_message


class TestCompassUITemplates:
    def test_render_chat_message_user(self):
        html = render_chat_message("USER", "Hello Compass", style="normal")
        assert "USER" in html
        assert "Hello Compass" in html

    def test_render_chat_message_compass(self):
        html = render_chat_message("COMPASS", "Task dispatched", style="normal")
        assert "COMPASS" in html
        assert "Task dispatched" in html

    def test_render_chat_message_input_required(self):
        html = render_chat_message("COMPASS", "[Task PROJ-123] Awaiting input", style="input-required")
        assert "input-required" in html

    def test_render_task_tab_failed(self):
        html = render_task_tab("PROJ-123", "failed", summary="Failed")
        assert "PROJ-123" in html
        assert "failed" in html

    def test_render_task_tab_completed(self):
        html = render_task_tab("PROJ-125", "completed", summary="PR #456")
        assert "PROJ-125" in html
        assert "PR #456" in html

    def test_render_task_tab_prefers_summary_as_primary_label(self):
        html = render_task_tab("PROJ-125", "completed", summary="Prepare PR for review")
        assert '<div class="task-title">Prepare PR for review</div>' in html
        assert '<div class="task-note">PROJ-125</div>' in html

    def test_render_ui_contains_major_step_digest_copy(self):
        html = render_compass_ui()
        assert "Longer execution detail continues in merged logs." in html
        assert "step-agent" in html
        assert "Plan" in html
        assert "Self-check" in html

    def test_render_ui_uses_compact_log_columns(self):
        html = render_compass_ui()
        assert "grid-template-columns: 70px 42px 64px 1fr;" in html

    def test_render_ui_contains_development_phase_semantics(self):
        html = render_compass_ui()
        assert "deriveMajorPhases" in html
        assert "Review & Deliver" in html
        assert "phase-rail" in html
        assert "Fix" in html
        assert "Workflow Timeline" in html
        assert "Active Phase" in html
        assert "phase-count" in html
        assert "Show all steps" in html
        assert "Current step only" in html
        assert "phaseExpandedByTask" in html

    def test_render_ui_prioritizes_fix_phase_before_test_match(self):
        html = render_compass_ui()
        assert "value.includes('fix')" in html
        assert "value.includes('test')" in html
        assert html.index("value.includes('fix')") < html.index("value.includes('test')")

    def test_render_ui_contains_task_overview_strip(self):
        html = render_compass_ui()
        assert 'id="tasks-overview-strip"' in html
        assert "Needs Attention" in html
        assert "Completed" in html

    def test_render_ui_contains_spotlight_and_next_action_shell(self):
        html = render_compass_ui()
        assert 'id="task-spotlight"' in html
        assert "Task Spotlight" in html
        assert "Next Action" in html
        assert "detail-section" in html
        assert "Request Summary" in html

    def test_render_ui_contains_responsive_breakpoints(self):
        html = render_compass_ui()
        assert "@media (max-width: 1500px)" in html
        assert "@media (max-width: 1100px)" in html

    def test_render_ui_contains_chat_context_shell(self):
        html = render_compass_ui()
        assert 'id="chat-context-bar"' in html
        assert "Reply Route" in html

    def test_render_ui_contains_log_toolbar_shell(self):
        html = render_compass_ui()
        assert 'id="log-toolbar"' in html
        assert "Visible Logs:" in html
        assert "Current Filter:" in html

    def test_render_ui_formats_log_timestamps_in_local_time(self):
        html = render_compass_ui()
        assert "function fmtLogTimestamp" in html
        assert "hour: '2-digit'" in html
        assert "minute: '2-digit'" in html
        assert "month: '2-digit'" in html
