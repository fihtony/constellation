"""Tests for Compass UI templates."""
import pytest
from agents.compass.ui.templates import render_compass_ui, render_task_tab, render_chat_message


class TestCompassUITemplates:
    def test_render_chat_message_user(self):
        html = render_chat_message("USER", "Hello Compass", style="normal")
        assert "USER" in html
        assert "Hello Compass" in html
        assert "bubble-label" not in html

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
        assert '<div class="step-digest">' not in html
        assert "step-agent" in html
        assert "Plan" in html
        assert "Self-check" in html

    def test_render_ui_uses_compact_log_columns(self):
        html = render_compass_ui()
        assert "grid-template-columns: 96px 44px minmax(58px, 72px) 1fr;" in html

    def test_render_ui_contains_development_phase_semantics(self):
        html = render_compass_ui()
        assert "deriveMajorPhases" in html
        assert "Review & Deliver" in html
        assert "phase-rail" in html
        assert "Fix" in html
        assert "Workflow Timeline" in html
        assert "phase-count" in html
        assert "Show all steps" in html
        assert "Current step only" in html
        assert "phaseExpandedByTask" in html

    def test_render_ui_removes_separate_active_phase_banner(self):
        html = render_compass_ui()
        assert "Active Phase" not in html
        assert 'phase-banner' not in html
        assert 'const currentPhase = semanticPhases ? (semanticPhases.phases.find(phase => phase.key === semanticPhases.currentKey) || semanticPhases.phases[0]) : null;' not in html

    def test_render_ui_reduces_original_request_title_font_size_by_twenty_percent(self):
        html = render_compass_ui()
        assert ".detail-request-title {" in html
        assert "font-size: 16px;" in html
        assert "font-size: 22px;" not in html

    def test_render_ui_optimistically_shows_new_requests_before_snapshot_sync(self):
        html = render_compass_ui()
        assert "function createOptimisticTask(text)" in html
        assert "function replaceTaskCollection(tasks)" in html
        assert "const optimisticTask = createOptimisticTask(text);" in html
        assert "state.selectedTaskId = optimisticTask.task_id;" in html
        assert "promoteOptimisticTask(optimisticTask.task_id, newId);" in html
        assert "replaceTaskCollection(data.tasks || []);" in html

    def test_render_ui_supports_generic_timeline_collapse_for_office_tasks(self):
        html = render_compass_ui()
        assert "function timelineHtmlForGeneric(task, steps, kind, expanded)" in html
        assert "timeline-list${expanded ? '' : ' collapsed'}" in html
        assert "const hasTimelineToggle = semanticPhases ? semanticPhases.phases.length > 1 : steps.length > 1;" in html
        assert "const currentMark = currentClass === 'failed' ? '✕' : (currentClass === 'warn' ? '!' : '✓');" in html

    def test_render_ui_prioritizes_fix_phase_before_test_match(self):
        html = render_compass_ui()
        assert "value.includes('fix')" in html
        assert "value.includes('test')" in html
        assert html.index("value.includes('fix')") < html.index("value.includes('test')")

    def test_render_ui_contains_dashboard_shell(self):
        html = render_compass_ui()
        assert 'id="dashboard"' in html
        assert "Total" in html
        assert "Waiting for Input" in html
        assert "In Progress" in html
        assert "Completed" in html

    def test_render_ui_contains_spotlight_and_next_action_shell(self):
        html = render_compass_ui()
        assert 'id="task-spotlight"' in html
        assert "detail-section" in html
        assert "Original Request" in html
        assert "detail-request-kicker" in html
        assert "Completion Summary" in html
        assert "Error Summary" in html
        assert "Current Owner" not in html
        assert "detail-type-pill" in html
        assert "Next Action" not in html
        assert "Current Focus" not in html
        assert "Task ID" not in html
        assert "Started At" not in html
        assert "Elapsed" not in html

    def test_render_ui_keeps_task_type_visible_in_list_and_detail(self):
        html = render_compass_ui()
        assert "task-type" in html
        assert "detail-type-pill" in html

    def test_render_ui_contains_responsive_breakpoints(self):
        html = render_compass_ui()
        assert "@media (max-width: 1500px)" in html
        assert "@media (max-width: 1100px)" in html

    def test_render_ui_removes_extra_chat_context_shell(self):
        html = render_compass_ui()
        assert "Compass Chat" in html
        assert 'id="chat-head-sub"' not in html
        assert 'id="chat-context-bar"' not in html
        assert "Reply Route" not in html

    def test_render_ui_supports_task_deselect_overview_mode(self):
        html = render_compass_ui()
        assert "const OVERVIEW_ID = '__overview__';" in html
        assert "if (tid === state.selectedTaskId && tid !== NEW_REQUEST_ID)" in html
        assert "Send a request to create a new Compass task." in html
        assert "Select a task to inspect it, or send a new request." in html
        assert "No task selected yet" in html
        assert "task-info-empty" in html

    def test_render_ui_dims_non_selected_tasks_like_spec(self):
        html = render_compass_ui()
        assert ".task-list.has-selection .task-item:not(.active)" in html
        assert "opacity: 0.54" in html
        assert "filter: saturate(0.72) brightness(0.82)" not in html
        assert ".task-list.is-overview .task-item {" in html
        assert "filter: saturate(0.82) brightness(0.86)" in html
        assert "root.classList.toggle('has-selection', hasSelection);" in html
        assert "root.classList.toggle('is-overview', isOverview);" in html

    def test_render_ui_selected_task_shadow_is_more_focused(self):
        html = render_compass_ui()
        assert "0 20px 40px rgba(0, 0, 0, 0.24)" in html
        assert "0 6px 16px rgba(8, 16, 24, 0.18)" in html

    def test_render_ui_task_titles_use_three_line_clamp(self):
        html = render_compass_ui()
        assert "-webkit-line-clamp: 3;" in html
        assert "text-wrap: pretty;" in html
        assert "overflow-wrap: anywhere;" in html

    def test_render_ui_aligns_shell_density_with_spec(self):
        html = render_compass_ui()
        assert "border-radius: 26px;" in html
        assert "padding: 14px 16px;" in html
        assert "font-size: 26px;" in html
        assert "grid-template-columns: clamp(255px, 22vw, 320px) minmax(340px, 1.02fr) minmax(370px, 1.12fr);" in html
        assert "border-right: 1px solid rgba(145, 171, 189, 0.08);" in html

    def test_render_ui_flattens_task_info_and_uses_spec_log_density(self):
        html = render_compass_ui()
        assert "background: rgba(14, 23, 33, 0.8);" in html
        assert "box-shadow: none;" in html
        assert "max-height: 250px;" in html
        assert "min-width: 92px;" in html
        assert "white-space: normal;" in html
        assert "overflow-wrap: anywhere;" in html

    def test_render_ui_contains_log_toolbar_shell(self):
        html = render_compass_ui()
        assert 'id="log-toolbar"' in html
        assert 'id="filter-agent-trigger"' in html
        assert 'id="filter-level-trigger"' in html
        assert 'id="filter-agent-menu"' in html
        assert 'id="filter-level-menu"' in html

    def test_render_ui_uses_independent_scroll_regions(self):
        html = render_compass_ui()
        assert "html, body { height: 100%; overflow: hidden;" in html
        assert ".app-shell {" in html
        assert "grid-template-rows: auto minmax(0, 1fr);" in html
        assert "#task-list-scroll {" in html
        assert "overflow-y: auto;" in html
        assert "#task-chat-panel .panel-body {" in html
        assert "grid-template-rows: minmax(0, 1fr) auto;" in html
        assert "#chat-scroll {" in html
        assert "#task-info-panel .panel-body {" in html
        assert ".log-box {" in html

    def test_render_ui_has_clean_scrollbars_on_all_scrollable_regions(self):
        html = render_compass_ui()
        # Task List scrollbar
        assert "#task-list-scroll::-webkit-scrollbar" in html
        assert "#task-list-scroll::-webkit-scrollbar-thumb" in html
        # Chat scrollbar
        assert "#chat-scroll::-webkit-scrollbar" in html
        assert "#chat-scroll::-webkit-scrollbar-thumb" in html
        # Log box scrollbar
        assert ".log-box::-webkit-scrollbar" in html
        assert ".log-box::-webkit-scrollbar-thumb" in html

    def test_render_ui_formats_log_timestamps_in_local_time(self):
        html = render_compass_ui()
        assert "function fmtLogTimestamp" in html
        assert "hour: '2-digit'" in html
        assert "minute: '2-digit'" in html
        assert "month: '2-digit'" in html

    def test_render_ui_has_pending_timeline_marker_hollow_circle(self):
        html = render_compass_ui()
        assert ".timeline-row.pending .timeline-mark {" in html
        assert "background: transparent" in html

    def test_render_ui_timeline_markers_have_correct_symbols(self):
        html = render_compass_ui()
        # done = checkmark, failed = X, warn = exclamation
        assert '"done")' in html or "'done')" in html
        assert "phaseMarkForClass" in html

    def test_render_ui_user_bubble_uses_ink_cyan_colors(self):
        html = render_compass_ui()
        assert "--user-bubble-bg-top: #355967" in html
        assert "--user-bubble-bg-bottom: #284652" in html
        assert "--user-bubble-text: #edf8fb" in html

    def test_render_ui_office_type_tag_bronze_ledger(self):
        html = render_compass_ui()
        # Bronze Ledger = amber/brown tones
        assert "--office-type-tag-bg: rgba(161, 112, 76, 0.82)" in html
        assert "--office-type-tag-text: #fff0e2" in html

    def test_render_ui_dev_type_tag_cyan_glass(self):
        html = render_compass_ui()
        # Cyan Glass = cyan tones
        assert "--dev-type-tag-bg: rgba(118, 204, 222, 0.28)" in html
        assert "--dev-type-tag-text: #e0f8fc" in html

    def test_render_ui_matches_spec_color_tokens_and_plain_chat_bubbles(self):
        html = render_compass_ui()
        assert "--bg: #071018" in html
        assert "--panel: rgba(12, 21, 31, 0.94)" in html
        assert "--office-card-bg-top: rgba(102, 125, 98, 0.9)" in html
        assert "--dev-card-bg-top: rgba(32, 78, 95, 0.90)" in html
        assert ".bubble .bubble-label" not in html

    def test_render_ui_uses_task_id_title_and_terminal_summary_helpers(self):
        html = render_compass_ui()
        assert 'id="task-info-head-task-id"' in html
        assert "const orchestratorTaskId = String(t.orchestratorTaskId || t.task_id || t.id || '').trim();" in html
        assert "const detailTitle = originalRequest || tid;" in html
        assert "const detailTitleLabel = originalRequest ? 'Original Request' : 'Task';" in html
        assert "function summarizeTaskOutcome(task, kind, currentStep)" in html
        assert "function latestLogField(taskId, fieldNames)" in html
        assert "function latestOfficeResultSummary(taskId)" in html
        assert "PR: ${prUrl}" in html
        assert "Branch: ${branch}" in html
        assert "Repo: ${repoUrl}" in html
        assert "Output: ${workspacePath}" in html
        assert "Report: ${reportPath}" in html
        assert "Raw Output: ${rawOutputPath}" in html
        assert "const metadataSummary = String(meta.summary || '').trim();" in html
        assert 'class="detail-card spotlight ${escapeAttr(kind)}"' in html
        assert "value === 'office task completed'" in html
        assert "value === 'office task returned a terminal result'" in html
        assert 'value === "office dispatch complete status=\'completed\'"' in html
        assert 'value.startsWith("office execution completed capability=")' in html
        assert "task_report_path" in html
        assert "raw_output_path" in html
        assert "Summary: Office output was verified and written successfully." in html
        assert "if (tid === state.selectedTaskId) renderDetail();" in html

    def test_render_ui_uses_semantic_spotlight_backgrounds(self):
        html = render_compass_ui()
        assert ".detail-card.spotlight.completed {" in html
        assert ".detail-card.spotlight.failed {" in html
