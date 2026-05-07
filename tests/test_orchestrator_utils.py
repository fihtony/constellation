from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from common.orchestrator import derive_service_base_url, resolve_orchestrator_base_url
from common.tools.progress_tools import ReportProgressTool


class _FakeAgentDirectory:
    def __init__(self, service_url: str = "", error: Exception | None = None):
        self.service_url = service_url
        self.error = error
        self.calls: list[str] = []

    def resolve_capability(self, capability: str):
        self.calls.append(capability)
        if self.error is not None:
            raise self.error
        return {"agent_id": "compass-agent"}, {"service_url": self.service_url}


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b""


class OrchestratorUtilsTests(unittest.TestCase):
    def test_derive_service_base_url_returns_origin_only(self):
        self.assertEqual(
            derive_service_base_url("http://compass:8080/tasks/task-1/callbacks?instance=abc"),
            "http://compass:8080",
        )

    def test_resolve_orchestrator_base_url_prefers_callback_url(self):
        directory = _FakeAgentDirectory(service_url="http://registry-discovered:8080")

        resolved = resolve_orchestrator_base_url(
            {
                "orchestratorCallbackUrl": "http://compass:8080/tasks/task-1/callbacks?instance=abc",
                "orchestratorUrl": "http://legacy:8080",
            },
            agent_directory=directory,
        )

        self.assertEqual(resolved, "http://compass:8080")
        self.assertEqual(directory.calls, [])

    def test_resolve_orchestrator_base_url_falls_back_to_capability_discovery(self):
        directory = _FakeAgentDirectory(service_url="http://discovered-orchestrator:8080")

        resolved = resolve_orchestrator_base_url({}, agent_directory=directory)

        self.assertEqual(resolved, "http://discovered-orchestrator:8080")
        self.assertEqual(directory.calls, ["orchestrator.progress.report"])

    def test_resolve_orchestrator_base_url_returns_empty_when_no_sources(self):
        directory = _FakeAgentDirectory(error=RuntimeError("missing capability"))

        resolved = resolve_orchestrator_base_url(
            {"orchestratorUrl": "http://legacy-orchestrator:8080"},
            agent_directory=directory,
        )

        self.assertEqual(resolved, "http://legacy-orchestrator:8080")


class ProgressToolTests(unittest.TestCase):
    def test_report_progress_prefers_callback_url(self):
        tool = ReportProgressTool()

        with patch("common.tools.progress_tools.urlopen", return_value=_FakeResponse()) as mocked_urlopen:
            result = tool.execute(
                {
                    "message": "Planning complete",
                    "step": "plan",
                    "task_id": "task-1",
                    "orchestrator_callback_url": "http://compass:8080/tasks/task-1/callbacks?instance=abc",
                }
            )

        self.assertFalse(result["isError"])
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://compass:8080/tasks/task-1/progress")

    def test_report_progress_discovers_orchestrator_by_capability(self):
        tool = ReportProgressTool()
        directory = _FakeAgentDirectory(service_url="http://discovered-orchestrator:8080")

        with patch("common.tools.progress_tools._get_agent_directory", return_value=directory):
            with patch("common.tools.progress_tools.urlopen", return_value=_FakeResponse()) as mocked_urlopen:
                result = tool.execute(
                    {
                        "message": "Review started",
                        "task_id": "task-2",
                    }
                )

        self.assertFalse(result["isError"])
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://discovered-orchestrator:8080/tasks/task-2/progress")
        self.assertEqual(directory.calls, ["orchestrator.progress.report"])

    def test_report_progress_logs_locally_without_orchestrator_url(self):
        tool = ReportProgressTool()

        with patch("common.tools.progress_tools._get_agent_directory", return_value=None):
            with patch("common.tools.progress_tools.urlopen") as mocked_urlopen:
                with patch.dict(os.environ, {"COMPASS_URL": ""}, clear=False):
                    result = tool.execute(
                        {
                            "message": "Validation finished",
                            "task_id": "task-3",
                        }
                    )

        self.assertFalse(result["isError"])
        self.assertIn("no orchestrator URL", result["content"][0]["text"])
        mocked_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()