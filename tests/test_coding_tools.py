from __future__ import annotations

import os
import tempfile
import unittest

import common.tools.coding_tools as coding_tools
from common.tools.registry import get_tool


class CodingToolAliasTests(unittest.TestCase):
    def setUp(self):
        self._sandbox = tempfile.TemporaryDirectory(prefix="coding_tools_")
        self.addCleanup(self._sandbox.cleanup)
        coding_tools.configure_coding_tools(sandbox_root=self._sandbox.name)

    def test_runtime_first_local_workspace_aliases_are_registered(self):
        for tool_name in (
            "run_local_command",
            "read_local_file",
            "write_local_file",
            "edit_local_file",
            "list_local_dir",
            "search_local_files",
        ):
            with self.subTest(tool=tool_name):
                self.assertIsNotNone(get_tool(tool_name))

    def test_list_local_dir_lists_relative_entries(self):
        os.makedirs(os.path.join(self._sandbox.name, "logs"), exist_ok=True)
        with open(os.path.join(self._sandbox.name, "logs", "summary.txt"), "w", encoding="utf-8") as handle:
            handle.write("done\n")

        tool = get_tool("list_local_dir")
        result = tool.execute({"path": ".", "recursive": True})

        self.assertFalse(result["isError"])
        output = result["content"][0]["text"]
        self.assertIn("logs/", output)
        self.assertIn("logs/summary.txt", output)


if __name__ == "__main__":
    unittest.main()