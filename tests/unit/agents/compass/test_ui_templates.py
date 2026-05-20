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