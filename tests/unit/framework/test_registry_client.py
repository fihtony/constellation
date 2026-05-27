"""Unit tests for framework/registry_client.py — v2 Registry client abstraction."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from framework.registry_client import RegistryClient, ServiceInstance


class TestRegistryClientInit:
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("CONSTELLATION_REGISTRY_URL", "http://registry:9000")
        client = RegistryClient.from_config()
        assert client.url == "http://registry:9000"

    def test_fallback_env(self, monkeypatch):
        monkeypatch.delenv("CONSTELLATION_REGISTRY_URL", raising=False)
        monkeypatch.setenv("REGISTRY_URL", "http://fallback:9000")
        client = RegistryClient.from_config()
        assert client.url == "http://fallback:9000"

    def test_constellation_takes_priority(self, monkeypatch):
        monkeypatch.setenv("REGISTRY_URL", "http://low-priority:9000")
        monkeypatch.setenv("CONSTELLATION_REGISTRY_URL", "http://high-priority:9000")
        client = RegistryClient.from_config()
        assert client.url == "http://high-priority:9000"

    def test_empty_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("CONSTELLATION_REGISTRY_URL", raising=False)
        monkeypatch.delenv("REGISTRY_URL", raising=False)
        # Patch config loader where it is imported
        with patch("framework.config.load_global_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(get=MagicMock(return_value=""))
            client = RegistryClient.from_config()
        assert client.url == ""


class TestRegistryClientDiscover:
    def test_discover_returns_empty_when_unconfigured(self):
        client = RegistryClient("")
        assert client.discover("jira.ticket.fetch") == ""

    def test_discover_caches_result(self):
        client = RegistryClient("http://registry:9000", cache_ttl_seconds=60)
        response = json.dumps([{"serviceUrl": "http://jira:8080"}]).encode()

        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp

            # First call — hits registry
            url = client.discover("jira.ticket.fetch")
            assert url == "http://jira:8080"
            assert mock_open.call_count == 1

            # Second call — cached
            url2 = client.discover("jira.ticket.fetch")
            assert url2 == "http://jira:8080"
            assert mock_open.call_count == 1  # no additional call

    def test_discover_reads_nested_instances(self):
        client = RegistryClient("http://registry:9000")
        response = json.dumps([
            {
                "agent_id": "jira",
                "capabilities": ["jira.ticket.fetch"],
                "instances": [{"service_url": "http://jira:8010", "status": "idle"}],
            }
        ]).encode()

        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp

            assert client.discover("jira.ticket.fetch") == "http://jira:8010"

    def test_discover_falls_back_to_persistent_card_url(self):
        client = RegistryClient("http://registry:9000")
        response = json.dumps([
            {
                "agent_id": "jira",
                "capabilities": ["jira.ticket.fetch"],
                "execution_mode": "persistent",
                "card_url": "http://jira:8010/.well-known/agent-card.json",
                "instances": [],
            }
        ]).encode()

        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp

            assert client.discover("jira.ticket.fetch") == "http://jira:8010"

    def test_discover_does_not_fallback_to_per_task_card_url(self):
        client = RegistryClient("http://registry:9000")
        response = json.dumps([
            {
                "agent_id": "web-dev",
                "capabilities": ["web-dev.task.execute"],
                "execution_mode": "per-task",
                "card_url": "http://web-dev:8050/.well-known/agent-card.json",
                "instances": [],
            }
        ]).encode()

        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp

            assert client.discover("web-dev.task.execute") == ""

    def test_invalidate_clears_cache(self):
        client = RegistryClient("http://registry:9000", cache_ttl_seconds=60)
        # Seed cache
        import time
        client._cache["jira.ticket.fetch"] = (time.time() + 999, "http://cached:8080")

        assert client.discover("jira.ticket.fetch") == "http://cached:8080"

        client.invalidate("jira.ticket.fetch")
        # After invalidation, discover without network → empty
        assert client.discover("jira.ticket.fetch") == ""

    def test_discover_handles_network_error(self):
        client = RegistryClient("http://registry:9000")

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            url = client.discover("jira.ticket.fetch")
        assert url == ""


class TestRegistryClientRepr:
    def test_repr(self):
        client = RegistryClient("http://registry:9000")
        assert "http://registry:9000" in repr(client)
