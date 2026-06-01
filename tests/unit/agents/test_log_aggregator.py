"""Tests for LogAggregator."""
import os
import tempfile
import time
import pytest
from agents.log_store.log_aggregator import LogAggregator, parse_log_line


class TestLogAggregator:
    def test_parse_log_line(self):
        line = "2026-05-20 10:30:00 [INFO ] [team-lead] Task started step=gather_context"
        result = parse_log_line(line)
        assert result["timestamp"] == "2026-05-20 10:30:00"
        assert result["level"] == "INFO"
        assert result["agent"] == "team-lead"
        assert result["message"] == "Task started step=gather_context"

    def test_parse_error_line(self):
        line = "2026-05-20 10:30:00 [ERROR] [scm] Access denied exit_code=403"
        result = parse_log_line(line)
        assert result["level"] == "ERROR"
        assert result["agent"] == "scm"

    def test_aggregate_from_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create agent log file
            agent_dir = os.path.join(tmpdir, "task-123", "team-lead")
            os.makedirs(agent_dir)
            log_file = os.path.join(agent_dir, "agent.log")
            with open(log_file, "w") as f:
                f.write("2026-05-20 10:30:00 [INFO ] [team-lead] Task started\n")

            aggregator = LogAggregator(tmpdir)
            logs = aggregator.aggregate_task("task-123")
            assert len(logs) == 1
            assert logs[0]["agent"] == "team-lead"

    # ------------------------------------------------------------------
    # UTC ISO timestamp format — every new line emitted by
    # framework.devlog.AgentLogger MUST carry an explicit timezone
    # offset so the Compass UI can convert to the viewer's local
    # clock without ambiguity. The legacy naive format is still
    # accepted for backward compatibility with existing log files.
    # ------------------------------------------------------------------

    def test_parse_log_line_accepts_utc_iso_with_colon_offset(self):
        line = "2026-06-01T12:34:56+00:00 [INFO ] [team-lead] UTC ISO line"
        result = parse_log_line(line)
        assert result is not None
        assert result["timestamp"] == "2026-06-01T12:34:56+00:00"
        assert result["level"] == "INFO"
        assert result["agent"] == "team-lead"
        assert result["message"] == "UTC ISO line"

    def test_parse_log_line_accepts_utc_iso_with_z_suffix(self):
        line = "2026-06-01T12:34:56Z [INFO ] [scm] Z-suffix line"
        result = parse_log_line(line)
        assert result is not None
        assert result["timestamp"] == "2026-06-01T12:34:56Z"
        assert result["agent"] == "scm"

    def test_parse_log_line_accepts_utc_iso_with_milliseconds(self):
        line = "2026-06-01T12:34:56.789+00:00 [DEBUG] [jira] With millis"
        result = parse_log_line(line)
        assert result is not None
        assert result["timestamp"] == "2026-06-01T12:34:56.789+00:00"
        assert result["level"] == "DEBUG"

    def test_parse_log_line_accepts_legacy_naive_format(self):
        # The pre-fix format must still parse so existing log files
        # remain readable after the devlog change.
        line = "2026-05-20 10:30:00 [INFO ] [team-lead] Legacy line"
        result = parse_log_line(line)
        assert result is not None
        assert result["timestamp"] == "2026-05-20 10:30:00"

    def test_aggregate_mixes_legacy_and_iso_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = os.path.join(tmpdir, "task-mix", "compass")
            os.makedirs(agent_dir)
            log_file = os.path.join(agent_dir, "agent.log")
            with open(log_file, "w") as f:
                f.write("2026-05-20 10:30:00 [INFO ] [compass] legacy line\n")
                f.write("2026-06-01T12:34:56+00:00 [INFO ] [compass] iso line\n")

            aggregator = LogAggregator(tmpdir)
            logs = aggregator.aggregate_task("task-mix")
            assert len(logs) == 2
            timestamps = [entry["timestamp"] for entry in logs]
            assert "2026-05-20 10:30:00" in timestamps
            assert "2026-06-01T12:34:56+00:00" in timestamps


class TestDevlogEmitsLocalTimezoneIso:
    """framework.devlog._ts() is the single source of truth for the
    timestamp string every agent writes to its agent.log. It MUST
    emit a timezone-aware ISO-8601 string with an explicit ``±HH:MM``
    offset (not the legacy naive ``YYYY-MM-DD HH:MM:SS`` form) so the
    Compass UI ``parseTimestamp`` can convert it to the viewer's
    local clock.

    The wall-clock zone is determined by the process's ``TZ`` env var
    (set in ``docker-compose-v2.yml`` for every agent service), so the
    test simply asserts that SOME offset is present — not a specific
    one — and that the wall-clock hour matches the local hour.
    """

    def test_devlog_ts_uses_explicit_offset(self, tmp_path):
        from framework.devlog import AgentLogger

        log = AgentLogger(task_id="task-iso-test", agent_name="compass")
        log.info("hello world")
        from framework.devlog import _artifact_root
        log_path = os.path.join(_artifact_root(), "task-iso-test", "compass", "agent.log")
        try:
            with open(log_path, "r", encoding="utf-8") as fh:
                line = fh.readline()
        finally:
            import shutil
            shutil.rmtree(os.path.join(_artifact_root(), "task-iso-test"), ignore_errors=True)

        # The line must start with an ISO-8601 timestamp carrying a
        # colon-delimited offset.
        assert line.startswith("20"), line
        ts = line.split(" ", 1)[0]
        assert "T" in ts, f"timestamp must use T separator: {line!r}"
        # Must have a ±HH:MM offset suffix (any zone)
        import re as _re
        assert _re.search(r"[+-]\d{2}:\d{2}$", ts), (
            f"timestamp must carry an explicit ±HH:MM offset: {line!r}"
        )
        # And the whole line must still be parseable by the aggregator
        parsed = parse_log_line(line)
        assert parsed is not None, f"aggregator must accept new format: {line!r}"
        assert parsed["agent"] == "compass"

    def test_devlog_ts_offset_matches_local_zone(self):
        """The emitted offset must match the process's local zone (driven
        by the ``TZ`` env var). On a host set to ``America/Los_Angeles``
        during tests, the offset will be -07:00 or -08:00 depending on
        DST. We assert it matches whatever Python reports for the same
        instant, so any TZ override propagates correctly.
        """
        from datetime import datetime
        from framework.devlog import _ts

        now_str = _ts()
        offset_in_str = now_str[-6:]
        offset_in_python = datetime.now().astimezone().isoformat(timespec="seconds")[-6:]
        assert offset_in_str == offset_in_python, (
            f"devlog offset {offset_in_str!r} does not match the process's "
            f"local zone {offset_in_python!r}"
        )

    def test_devlog_ts_reflects_tz_env_var(self, monkeypatch):
        """When the ``TZ`` env var changes, ``_ts()`` must follow. This
        is the mechanism docker-compose-v2.yml relies on to propagate
        the host's timezone into every agent container.
        """
        import os
        from datetime import datetime
        from framework import devlog

        for tz_name, expected_offset in [
            ("America/New_York", "-04:00"),  # EDT in June
            ("America/Los_Angeles", "-07:00"),  # PDT in June
            ("UTC", "+00:00"),
        ]:
            monkeypatch.setenv("TZ", tz_name)
            # Python caches timezone info via time.tzset; force a refresh
            if hasattr(time, "tzset"):
                time.tzset()
            stamp = devlog._ts()
            assert stamp.endswith(expected_offset), (
                f"with TZ={tz_name!r}, expected offset {expected_offset!r} "
                f"but _ts() produced {stamp!r}"
            )