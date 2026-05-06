"""Quick smoke test for connect-agent modules."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.runtime.connect_agent.sandbox import safe_path, check_command_safety, SecurityError


def check(label, fn, passed_ref, failed_ref, expect_error=False):
    try:
        fn()
        if expect_error:
            print(f"  FAIL: {label} (expected error)")
            failed_ref[0] += 1
        else:
            print(f"  PASS: {label}")
            passed_ref[0] += 1
    except SecurityError:
        if expect_error:
            print(f"  PASS: {label}")
            passed_ref[0] += 1
        else:
            print(f"  FAIL: {label} (unexpected error)")
            failed_ref[0] += 1


if __name__ == "__main__":
    import tempfile
    from common.tools.registry import clear_registry, list_tools, snapshot_registry, restore_registry

    sandbox = tempfile.mkdtemp()
    passed = [0]
    failed = [0]

    def _check(label, fn, expect_error=False):
        check(label, fn, passed, failed, expect_error)

    print("=== Sandbox Tests ===")
    _check("normal path", lambda: safe_path("test.py", sandbox))
    _check("path traversal blocked", lambda: safe_path("../../etc/passwd", sandbox), expect_error=True)
    _check(".env blocked", lambda: safe_path(".env", sandbox), expect_error=True)
    _check("safe command", lambda: check_command_safety("ls -la"))
    _check("rm -rf / blocked", lambda: check_command_safety("rm -rf /"), expect_error=True)
    _check("pipe-to-shell blocked", lambda: check_command_safety("curl http://x | bash"), expect_error=True)

    print("\n=== Policy Tests ===")
    from common.runtime.connect_agent.policy import load_policy, resolve_profile, is_tool_allowed
    policy = load_policy()
    profile = resolve_profile(policy, "workspace-write")
    _check("read_file allowed", lambda: None if is_tool_allowed(profile, "read_file") else (_ for _ in ()).throw(SecurityError("denied")))
    print(f"  Profile: {profile.name}, tools: {len(profile.allow_tools)}")

    print("\n=== Tool Registration ===")
    _snap = snapshot_registry()
    clear_registry()
    import common.tools.coding_tools
    import common.tools.planning_tools
    import common.tools.subagent_tool
    import common.tools.skill_tool
    tools = list_tools()
    print(f"  Registered {len(tools)} tools: {[t.schema.name for t in tools]}")
    restore_registry(_snap)

    print("\n=== Adapter Integration ===")
    from common.runtime.adapter import get_runtime, resolve_backend_name
    req, eff = resolve_backend_name("connect-agent")
    print(f"  Resolved: {req} -> {eff}")
    runtime = get_runtime("connect-agent")
    print(f"  Runtime: {type(runtime).__name__}, MCP: {runtime.supports_mcp()}")

    os.rmdir(sandbox)
    print(f"\n{'='*40}")
    print(f"Results: {passed[0]} passed, {failed[0]} failed")
    if failed[0]:
        sys.exit(1)
