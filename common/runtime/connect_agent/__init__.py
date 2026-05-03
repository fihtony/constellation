"""Connect Agent runtime package.

All Connect-Agent orchestration and Copilot Connect transport logic lives in
this package so other runtimes can depend on it without opening a second
direct integration path to Copilot Connect.
"""

from common.runtime.connect_agent.adapter import ConnectAgentAdapter

__all__ = ["ConnectAgentAdapter"]