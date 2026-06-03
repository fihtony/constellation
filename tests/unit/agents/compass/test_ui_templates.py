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
        # v0.8 timeline redesign: each row carries an actor-prefixed title and
        # the structured summary is rendered via ``renderTemplate`` (which
        # substitutes ``{name}`` placeholders from ``summary_facts``).
        assert "function renderTemplate(template, facts)" in html
        assert "function pickMarkForVisualState(visualState)" in html
        assert "function deriveMajorTimeline(task)" in html
        assert "function timelineHtmlForMajorSteps(task, timeline" in html

    def test_render_ui_uses_compact_log_columns(self):
        html = render_compass_ui()
        assert "grid-template-columns: 96px 44px minmax(58px, 72px) 1fr;" in html

    def test_render_ui_contains_development_phase_semantics(self):
        # v0.8 redesign: the timeline reads structured ``major_step_rows`` /
        # ``major_steps_skeleton`` data; the legacy keyword-bucketing rail
        # (``phase-rail`` with hard-coded Plan/Implement/Build/Test/...) is
        # no longer emitted.
        html = render_compass_ui()
        assert "function deriveMajorTimeline(task)" in html
        assert "function pickPointerRow(task)" in html
        assert "phaseExpandedByTask" in html
        assert "Show all steps" in html
        assert "Current step only" in html
        assert "Workflow Timeline" in html
        assert 'phase-rail' not in html
        assert 'deriveMajorPhases' not in html
        assert 'developmentPhaseForText' not in html
        assert "Review & Deliver" not in html  # legacy fixed-phase label removed

    def test_render_ui_removes_separate_active_phase_banner(self):
        html = render_compass_ui()
        assert "Active Phase" not in html
        assert 'phase-banner' not in html
        assert 'const currentPhase = semanticPhases' not in html

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
        # The new structured renderer is the primary path; the generic
        # legacy renderer remains as a fallback for tasks that have no
        # ``major_step_rows`` yet.
        assert "function timelineHtmlForGeneric(task, steps, kind, expanded)" in html
        assert "function timelineHtmlForMajorSteps(task, timeline" in html
        assert "timeline-list${expanded ? '' : ' collapsed'}" in html
        # hasTimelineToggle is now driven by the new timeline's ordered rows,
        # not the legacy phase-bucket count.
        assert "majorTimeline.ordered.length > 1" in html
        assert "currentMark = currentClass === 'failed' ? '✕' : (currentClass === 'warn' ? '!' : '✓');" in html

    def test_render_ui_renders_compact_duration_for_v08_timeline(self):
        # §8.1: emit ``Xh Ym Zs`` for hour-bearing spans and ``Ym Zs`` when
        # hours are zero; render ``Not started yet`` for unfired conditional
        # rows.
        html = render_compass_ui()
        assert "function compactDuration(ms)" in html
        assert "function compactStartTime(iso)" in html
        assert "Not started yet" in html
        # Hours branch: ``Xh YYm ZZs``.
        assert "${hours}h ${mm}m ${ss}s" in html
        # No-hours branch: ``YYm ZZs`` (design doc §2.1: zero hours omitted).
        assert "${mm}m ${ss}s" in html

    def test_render_ui_keeps_collapsed_major_timeline_focus_row_visible(self):
        html = render_compass_ui()
        assert "const isFocusedRow = !expanded || (row.fired && visualClass === 'current');" in html
        assert "${isFocusedRow ? ' current' : ''}" in html

    def test_render_ui_migrates_legacy_compass_received_row_to_done_when_later_steps_exist(self):
        html = render_compass_ui()
        assert "const isLegacyCompassReceived = stepKey === 'compass.received';" in html
        assert "const hasLaterFiredStep = ordered.some(candidate => candidate.key !== sik && candidate.fired && !candidate.ignored);" in html
        assert "visualState = 'done';" in html
        assert "lifecycleState = 'done';" in html

    def test_render_ui_renders_unfired_skeleton_rows_as_pending(self):
        html = render_compass_ui()
        assert "visualState: skel.conditional ? 'conditional_pending' : 'pending'" in html
        assert "ordered: normalizedOrdered.filter(row => !row.ignored)" in html

    def test_render_ui_uses_utc_aware_timestamp_parser_for_major_timeline(self):
        html = render_compass_ui()
        assert "const d = parseTimestamp(iso);" in html
        assert "const endDate = row.endedAt" in html
        assert "const startDate = row.startedAt ? parseTimestamp(row.startedAt) : null;" in html

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

    def test_render_ui_resume_send_keeps_current_task_selected(self):
        html = render_compass_ui()
        assert "state.selectedTaskId = targetTaskId;" in html
        assert "selectTask(targetTaskId);" not in html

    def test_render_ui_disables_composer_for_optimistic_placeholder_tasks(self):
        html = render_compass_ui()
        assert "if (t.optimistic === true) {" in html
        assert "Request submission in progress. Please wait for Compass to respond." in html
        assert "composerInput.placeholder = 'Submitting request...';" in html
        assert "composerInput.dataset.mode = 'disabled';" in html

    def test_render_ui_recovers_when_create_request_submission_fails(self):
        html = render_compass_ui()
        assert "async function fetchJsonWithTimeout(url, options, timeoutMs = 15000)" in html
        assert "Compass did not acknowledge the request. Please retry." in html
        assert "delete state.tasks[optimisticId];" in html
        assert "state.selectedTaskId = NEW_REQUEST_ID;" in html

    def test_render_ui_recovers_when_resume_request_submission_fails(self):
        html = render_compass_ui()
        assert "Failed to send reply to Compass. Please retry." in html
        assert "targetTask.statusState = 'TASK_STATE_INPUT_REQUIRED';" in html
        assert "targetTask.status = 'waiting';" in html
        assert "targetTask.currentMajorStep = 'Waiting for output mode selection';" in html
        assert "targetTask.summary = 'Waiting for output mode selection';" in html
        assert "input.value = text;" in html

    def test_render_ui_updates_office_waiting_note_when_resume_starts(self):
        html = render_compass_ui()
        assert "targetTask.summary = 'Office task dispatching in background';" in html
        assert "targetTask.currentMajorStep = 'Office task dispatching in background';" in html

    def test_render_ui_keeps_background_refresh_loop_even_with_sse(self):
        html = render_compass_ui()
        assert "taskRefreshIntervalId: null," in html
        assert "function ensureTaskRefreshLoop()" in html
        assert "state.taskRefreshIntervalId = setInterval(() => loadTasks(false), 5000);" in html
        assert "ensureTaskRefreshLoop();" in html

    def test_render_ui_auto_refresh_never_changes_selected_task(self):
        """Auto-refresh (interval + SSE) must not steal focus to a waiting task.

        Earlier versions hijacked ``state.selectedTaskId`` to the first task in
        ``input-required`` whenever the user was on the New Request composer.
        That stole focus the moment a user clicked "New Request" and started
        typing — the next 5s refresh would yank them onto a waiting task and
        their composer state would disappear.  ``loadTasks`` must therefore
        NEVER auto-promote a waiting task; navigation to a waiting task is the
        user's explicit action via the dashboard card (see the next test).
        """
        html = render_compass_ui()
        # Removed hijack signatures must not reappear.
        assert "Auto-redirect to a waiting task" not in html
        # The old hijack scanned ``state.order`` inline inside ``loadTasks``
        # and assigned the first waiting id to ``state.selectedTaskId``.  The
        # explicit click path uses ``orderedTaskIds()`` instead, so the
        # ``state.order.find`` form is the unique fingerprint of the bug.
        assert "state.order.find(id => {" not in html
        assert (
            "if (state.selectedTaskId === NEW_REQUEST_ID || isOverviewSelection(state.selectedTaskId)) {"
            not in html
        )
        # The only auto-adjustment that survives is the deleted-task rescue:
        # when the previously-selected task vanishes from the server snapshot,
        # we fall back to the newest task or the New Request composer.
        assert (
            "if (autoSelect && !state.tasks[state.selectedTaskId] "
            "&& !isOverviewSelection(state.selectedTaskId) "
            "&& state.selectedTaskId !== NEW_REQUEST_ID) {"
        ) in html

    def test_render_ui_waiting_dashboard_card_jumps_to_latest_waiting_task(self):
        """Clicking the "Waiting for Input" dashboard card must navigate to the
        most recent task that is still waiting for the user.  This is the only
        place where focus moves to a waiting task — auto-refresh never does.
        """
        html = render_compass_ui()
        # Selector helper exists and is invoked from a dashboard click handler.
        assert "function selectLatestWaitingTask()" in html
        assert "selectLatestWaitingTask();" in html
        # The helper must scan tasks in the same order the user sees them
        # (newest activity first) and pick the first one in 'waiting'.
        assert "orderedTaskIds().find(id => {" in html
        # The dashboard click handler is wired at DOMContentLoaded and uses
        # event delegation so renderDashboard's text updates don't detach it.
        assert "const dashboardEl = $('#dashboard');" in html
        assert "dashboardEl.addEventListener('click', event => {" in html
        assert "event.target.closest('.dashboard-card.waiting')" in html
        # Visual affordance: card looks clickable.
        assert ".dashboard-card.waiting {" in html
        assert "cursor: pointer;" in html

    def test_render_ui_promotes_stale_reply_mode_back_to_resume(self):
        html = render_compass_ui()
        assert "function taskNeedsResume(task)" in html
        assert "function resolveComposerMode(mode, targetTaskId)" in html
        assert "const effectiveMode = resolveComposerMode(mode, targetTaskId);" in html
        assert "if ((mode === 'reply' || mode === 'resume') && taskNeedsResume(task)) return 'resume';" in html

    def test_render_ui_keeps_text_when_reply_is_not_supported(self):
        html = render_compass_ui()
        assert "Compass is not waiting for input on this task right now." in html
        assert "} else if (effectiveMode === 'reply' && targetTaskId) {" in html

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
        assert "function parseTimestamp(iso)" in html
        assert "fraction.padEnd(3, '0').slice(0, 3)" in html
        assert "if (!zoneToken && !offsetSign) {" in html

    def test_render_ui_normalizes_python_utc_timestamps_for_local_display(self):
        html = render_compass_ui()
        assert "Date.UTC(" in html
        assert "offsetMinutes * 60 * 1000" in html
        assert "const d = parseTimestamp(iso);" in html

    def test_render_ui_treats_naive_timestamps_as_utc(self):
        """Devlog emits naive "YYYY-MM-DD HH:MM:SS" in the container's UTC clock.
        parseTimestamp must interpret that as UTC (not browser-local) so the
        displayed local time is correct for users in any timezone.
        """
        html = render_compass_ui()
        # The naive branch (no zoneToken, no offsetSign) must call Date.UTC
        # instead of new Date(year, month, ...) which would be browser-local.
        idx = html.index("if (!zoneToken && !offsetSign) {")
        branch = html[idx:idx + 800]
        assert "Date.UTC(" in branch, (
            "Naive-timestamp branch in parseTimestamp must use Date.UTC "
            "so UTC-emitted log lines render in the viewer's local time"
        )
        # Make sure the broken form is not present
        assert "new Date(\n          Number(year)" not in branch
        assert "new Date(" in branch  # outer Date.UTC wrap is still new Date(...)
        # Date.UTC must appear at least twice now (offset branch + naive branch)
        assert html.count("Date.UTC(") >= 2

    def test_render_ui_has_pending_timeline_marker_hollow_circle(self):
        html = render_compass_ui()
        assert ".timeline-row.pending .timeline-mark {" in html
        assert "background: transparent" in html

    def test_render_ui_timeline_markers_have_correct_symbols(self):
        html = render_compass_ui()
        # v0.8 timeline redesign: visual-state → glyph mapping in
        # ``pickMarkForVisualState`` (done=✓, failed=✕, warn=!, current=●,
        # pending=○, conditional_pending=◐).
        assert "function pickMarkForVisualState(visualState)" in html
        assert "case 'done': return '✓';" in html
        assert "case 'failed': return '✕';" in html
        assert "case 'warn': return '!';" in html

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

    def test_render_ui_renders_completion_summary_as_markdown(self):
        """The completion summary should be HTML-rendered via a markdown helper."""
        html = render_compass_ui()
        assert "function renderMarkdown(text)" in html
        assert "function sanitizeMarkdownUrl(url)" in html
        # Outcome block now uses renderMarkdown, not raw esc
        assert "renderMarkdown(outcome.text)" in html
        # CSS class enables markdown styling
        assert "markdown-content" in html

    def test_render_ui_markdown_supports_bold_italic_and_inline_code(self):
        html = render_compass_ui()
        # Bold (must come before italic so ** is not split into two *)
        assert "\\*\\*([^*\\n]+)\\*\\*" in html
        assert "<strong>$1</strong>" in html
        # Italic
        assert "<em>$2</em>" in html
        # Inline code extraction happens before HTML escape
        assert "`([^`\\n]+)`" in html
        assert "<code>" in html

    def test_render_ui_markdown_supports_headings_lists_and_blockquotes(self):
        html = render_compass_ui()
        # Headings # to ######
        assert "^#\\s+" in html
        assert "^##\\s+" in html
        assert "^###\\s+" in html
        assert "^####\\s+" in html
        assert "^#####\\s+" in html
        assert "^######\\s+" in html
        # Unordered list
        assert "[-*+]\\s+" in html
        assert "<ul>" in html
        assert "<li>" in html
        # Ordered list
        assert "\\d+\\.\\s+" in html
        assert "<ol>" in html
        # Blockquote
        assert "&gt;\\s*" in html
        assert "<blockquote>" in html
        # Horizontal rule
        assert "<hr>" in html

    def test_render_ui_markdown_supports_links_and_strikethrough(self):
        html = render_compass_ui()
        # Markdown link pattern (escaped brackets in JS regex)
        assert "\\[([^\\]\\n]+)\\]\\(" in html
        assert "sanitizeMarkdownUrl(url)" in html
        # Auto-link bare URLs
        assert "https?:\\/\\/" in html
        assert "rel=\"noopener noreferrer\"" in html
        # Strikethrough
        assert "~~([^~\\n]+)~~" in html
        assert "<del>" in html

    def test_render_ui_markdown_handles_fenced_code_blocks(self):
        html = render_compass_ui()
        # Fenced code blocks are extracted to placeholders to protect their content
        assert "```([a-zA-Z0-9_-]*)\\n?([\\s\\S]*?)```" in html
        # Restored as <pre><code>...</code></pre>
        assert "<pre><code" in html

    def test_render_ui_markdown_normalizes_literal_whitespace_escape_sequences(self):
        """Completion summaries sometimes arrive with literal escape sequences
        (backslash + n) instead of real newlines — e.g. an agent JSON-encodes
        its output and the envelope re-encodes the already-encoded string, so
        a real "\\n" becomes the two-character sequence backslash+n by the
        time the browser sees it.  Without normalization, markdown headings,
        lists, and paragraph splits never trigger because the grammar anchors
        to real \\n line starts (the ``m`` flag and the paragraph splitter at
        the end of renderMarkdown both depend on real \\n).
        """
        html = render_compass_ui()
        # All four common literal-escape variants must be normalized.
        assert r".replace(/\\r\\n/g, '\n')" in html
        assert r".replace(/\\n/g, '\n')" in html
        assert r".replace(/\\r/g, '\n')" in html
        assert r".replace(/\\t/g, '\t')" in html

    def test_render_ui_markdown_normalization_runs_after_code_extraction(self):
        """The escape-sequence normalization must run AFTER fenced/inline code
        blocks have been swapped out for placeholders.  Otherwise a legitimate
        literal ``\\n`` inside code (e.g. ``print("hi\\n")``) would be turned
        into a real newline and corrupt the code block's contents.  It must
        also run BEFORE the ``esc(html)`` HTML-escape step so that the regex
        is matching simple backslash + n, not HTML entities.
        """
        html = render_compass_ui()
        # Inline-code extraction (must precede normalization)
        inline_extract_pos = html.index("inlineCodes.push(code);")
        # The first replace in the normalization chain
        normalize_pos = html.index(r".replace(/\\r\\n/g, '\n')")
        # HTML escape (must follow normalization)
        esc_pos = html.index("html = esc(html);")
        assert inline_extract_pos < normalize_pos, (
            "Escape-sequence normalization must run after inline-code "
            "extraction so literal \\n inside backticks is preserved"
        )
        assert normalize_pos < esc_pos, (
            "Escape-sequence normalization must run before esc(html) so "
            "the regex matches plain backslash characters, not entities"
        )

    def test_render_ui_markdown_escapes_html_before_applying_patterns(self):
        """XSS safety: HTML must be escaped before markdown patterns are applied."""
        html = render_compass_ui()
        # The escape() call must come BEFORE the inline patterns (bold/italic/etc.)
        # by appearing earlier in the source than the first <strong> replacement.
        escape_pos = html.index("html = esc(html);")
        strong_pos = html.index("<strong>$1</strong>")
        assert escape_pos != -1, "HTML escape call is missing"
        assert strong_pos != -1, "Bold replacement is missing"
        assert escape_pos < strong_pos, (
            "renderMarkdown must escape HTML before applying markdown patterns"
        )

    def test_render_ui_markdown_sanitizer_blocks_dangerous_protocols(self):
        html = render_compass_ui()
        # The sanitizer whitelists http/https/mailto only
        assert "proto === 'http:'" in html
        assert "proto === 'https:'" in html
        assert "proto === 'mailto:'" in html
        # Anything else falls back to '#'
        assert "return '#';" in html

    def test_render_ui_markdown_wraps_remaining_text_in_paragraphs(self):
        html = render_compass_ui()
        # Plain text blocks should be wrapped in <p>
        assert "trimmed.replace(/\\n/g, '<br>')" in html
        # Block elements are not re-wrapped
        assert "h[1-6]|ul|ol|blockquote|pre|hr|p" in html

    def test_render_ui_markdown_css_supports_all_rendered_elements(self):
        """The .markdown-content container must style p, h*, lists, quotes, code, links."""
        html = render_compass_ui()
        assert ".markdown-content {" in html
        assert ".markdown-content > p {" in html
        assert ".markdown-content h1," in html
        assert ".markdown-content h2," in html
        assert ".markdown-content h3," in html
        assert ".markdown-content h4," in html
        assert ".markdown-content h5," in html
        assert ".markdown-content h6 {" in html
        assert ".markdown-content ul," in html
        assert ".markdown-content ol {" in html
        assert ".markdown-content li {" in html
        assert ".markdown-content blockquote {" in html
        assert ".markdown-content code {" in html
        assert ".markdown-content pre {" in html
        assert ".markdown-content pre code {" in html
        assert ".markdown-content a {" in html
        assert ".markdown-content a:hover" in html
        assert ".markdown-content strong" in html
        assert ".markdown-content em" in html
        assert ".markdown-content del" in html
        assert ".markdown-content hr {" in html

    def test_render_ui_markdown_renders_gfm_tables(self):
        """GitHub-flavored markdown tables must be rendered as <table>, not as
        paragraphs of pipe characters.  This is what the task-completion
        summary section relies on (e.g. summarise-documents reports a list of
        files in a 3-column table)."""
        html = render_compass_ui()
        # The replacement builds <table class="markdown-table">…</table>
        assert "table.markdown-table" in html
        assert "<thead>" in html
        assert "<tbody>" in html
        assert "<th" in html
        assert "<td" in html
        # CSS class for styling lives in the .markdown-content block
        assert ".markdown-content table.markdown-table" in html
        assert ".markdown-content table.markdown-table th {" in html
        assert ".markdown-content table.markdown-table td {" in html

    def test_render_ui_markdown_table_paragraph_splitter_skips_table_block(self):
        """The paragraph splitter must not wrap a table block in <p>.  Without
        this, the rendered HTML would put <p><table>…</table></p> in the DOM and
        browsers display a paragraph gap on either side of the table."""
        html = render_compass_ui()
        # Block-level elements including <table> are passed through as-is
        assert "h[1-6]|ul|ol|blockquote|pre|hr|p" in html
        assert "table" in html
        # And the splitter trims/passes any block that starts with one of those
        assert "if (/^<(h[1-6]|ul|ol|blockquote|pre|hr|p|table|div|article|section)\\b/" in html
