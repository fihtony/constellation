from __future__ import annotations

import unittest
from unittest import mock

from jira.providers.mcp import JiraMCPProvider


class JiraMCPProviderUnitTests(unittest.TestCase):
    def test_add_comment_with_adf_uses_rest_fallback(self):
        provider = JiraMCPProvider("https://example.atlassian.net", "token", "bot@example.com")
        provider._rest = mock.Mock()
        provider._rest.add_comment.return_value = ("123", "added")

        result = provider.add_comment(
            "CSTL-1",
            "plain text",
            adf_body={"type": "doc", "version": 1, "content": []},
        )

        provider._rest.add_comment.assert_called_once_with(
            "CSTL-1",
            "plain text",
            {"type": "doc", "version": 1, "content": []},
        )
        self.assertEqual(result, ("123", "added"))

    def test_update_comment_with_adf_uses_rest_fallback(self):
        provider = JiraMCPProvider("https://example.atlassian.net", "token", "bot@example.com")
        provider._rest = mock.Mock()
        provider._rest.update_comment.return_value = ("456", "updated")

        result = provider.update_comment(
            "CSTL-1",
            "456",
            "plain text",
            adf_body={"type": "doc", "version": 1, "content": []},
        )

        provider._rest.update_comment.assert_called_once_with(
            "CSTL-1",
            "456",
            "plain text",
            {"type": "doc", "version": 1, "content": []},
        )
        self.assertEqual(result, ("456", "updated"))


if __name__ == "__main__":
    unittest.main()