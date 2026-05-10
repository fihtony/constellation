#!/usr/bin/env python3
"""Run a quick smoke test of the Compass workflow locally.

Usage:
    python scripts/run_test_workflow.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from agents.compass.agent import compass_workflow

    compiled = compass_workflow.compile()

    print("=== Constellation v2 Workflow Smoke Test ===\n")

    # Test 1: General question
    print("[Test 1] General question flow")
    state = {"user_request": "What is Python?"}
    result = await compiled.invoke(state)
    print(f"  Classification: {result['task_classification']}")
    print(f"  Summary: {result.get('user_summary', 'N/A')[:80]}")
    print()

    # Test 2: Development task (with pre-loaded dev result)
    print("[Test 2] Development task flow")
    state = {
        "user_request": "Fix the login bug in Jira ticket ABC-123",
        "dev_result": {"pr_url": "https://github.com/org/repo/pull/42", "success": True},
    }
    result = await compiled.invoke(state)
    print(f"  Classification: {result['task_classification']}")
    print(f"  Completeness: {result.get('completeness_score', 'N/A')}")
    print(f"  Summary: {result.get('user_summary', 'N/A')[:80]}")
    print()

    # Test 3: Office task
    print("[Test 3] Office task flow")
    state = {"user_request": "Summarize all PDF documents in my folder"}
    result = await compiled.invoke(state)
    print(f"  Classification: {result['task_classification']}")
    print(f"  Summary: {result.get('user_summary', 'N/A')[:80]}")
    print()

    print("=== All smoke tests passed ===")


if __name__ == "__main__":
    asyncio.run(main())
