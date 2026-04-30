"""Integration test for Connect Agent runtime.

Runs a real agentic task using connect-agent backend with Copilot Connect.
Tests: tool registration, agent loop, LLM interaction, file operations.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force connect-agent backend and gpt-5-mini model
os.environ["AGENT_RUNTIME"] = "connect-agent"
os.environ["AGENT_MODEL"] = "gpt-5-mini"
os.environ["OPENAI_BASE_URL"] = "http://localhost:1288/v1"

def main():
    # Create a temporary workspace
    workspace = tempfile.mkdtemp(prefix="connect-agent-test-")
    os.environ["CONNECT_AGENT_SANDBOX_ROOT"] = workspace
    os.environ["CONNECT_AGENT_MAX_TURNS"] = "15"
    os.environ["CONNECT_AGENT_TIMEOUT"] = "300"

    print(f"Workspace: {workspace}")
    print(f"Model: {os.environ.get('AGENT_MODEL')}")
    print(f"Endpoint: {os.environ.get('OPENAI_BASE_URL')}")
    print()

    # Seed a simple Python file for the agent to work with
    os.makedirs(os.path.join(workspace, "src"), exist_ok=True)
    with open(os.path.join(workspace, "src", "calculator.py"), "w") as f:
        f.write("""# Simple calculator module

def add(a, b):
    return a + b

def subtract(a, b):
    return a - b
""")

    # Clear tool registry to avoid duplicates
    from common.tools.registry import clear_registry
    clear_registry()

    # Get the runtime
    from common.runtime.adapter import get_runtime
    runtime = get_runtime("connect-agent")
    print(f"Runtime: {type(runtime).__name__}")
    print(f"MCP support: {runtime.supports_mcp()}")
    print()

    # Define a simple task
    task = (
        "Read the file src/calculator.py in the current workspace. "
        "Add a multiply(a, b) function and a divide(a, b) function "
        "(handle division by zero). Then read the file again to verify "
        "your changes. Finally, summarize what you did."
    )

    print(f"Task: {task}")
    print()
    print("=" * 60)
    print("Starting agentic execution...")
    print("=" * 60)

    def on_progress(msg):
        print(f"  [progress] {msg[:200]}")

    result = runtime.run_agentic(
        task=task,
        cwd=workspace,
        max_turns=15,
        timeout=300,
        on_progress=on_progress,
    )

    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    print(f"  Success: {result.success}")
    print(f"  Turns: {result.turns_used}")
    print(f"  Backend: {result.backend_used}")
    print(f"  Tool calls: {len(result.tool_calls)}")
    if result.tool_calls:
        print("  Tool call details:")
        for tc in result.tool_calls:
            print(f"    - {tc['name']}: {json.dumps(tc.get('args', {}))[:100]}")
    print()
    print(f"  Summary:\n{result.summary[:500]}")

    # Verify the file was actually modified
    print()
    print("=" * 60)
    print("VERIFICATION")
    print("=" * 60)
    calc_path = os.path.join(workspace, "src", "calculator.py")
    if os.path.exists(calc_path):
        with open(calc_path) as f:
            content = f.read()
        has_multiply = "def multiply" in content
        has_divide = "def divide" in content
        has_zero_check = "zero" in content.lower() or "ZeroDivisionError" in content
        print(f"  File exists: True")
        print(f"  Has multiply(): {has_multiply}")
        print(f"  Has divide(): {has_divide}")
        print(f"  Handles division by zero: {has_zero_check}")
        print()
        print("  Final file content:")
        print("  " + "-" * 40)
        for line in content.split("\n"):
            print(f"  {line}")
        print("  " + "-" * 40)

        if has_multiply and has_divide:
            print("\n  VERDICT: PASS - Agent successfully modified the file")
        else:
            print("\n  VERDICT: FAIL - Missing expected functions")
    else:
        print(f"  File exists: False")
        print("\n  VERDICT: FAIL - File not found")

    # Cleanup
    shutil.rmtree(workspace, ignore_errors=True)

if __name__ == "__main__":
    main()
