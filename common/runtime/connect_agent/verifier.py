"""Execution verifier for connect-agent results."""

from __future__ import annotations

from dataclasses import dataclass


_MUTATING_TOOLS = {
    "write_file",
    "edit_file",
    "jira_add_comment",
    "scm_create_branch",
    "scm_push_files",
    "scm_create_pr",
}

_VERIFICATION_TOOLS = {
    "read_file",
    "glob",
    "grep",
    "bash",
}


@dataclass
class VerificationResult:
    passed: bool
    evidence: list[dict]
    summary: str


class ExecutionVerifier:
    def __init__(self, *, required: bool = True) -> None:
        self._required = required

    def verify(self, tool_calls: list[dict]) -> VerificationResult:
        evidence: list[dict] = []
        last_mutation_index = -1
        verified_after_mutation = False

        for index, tool_call in enumerate(tool_calls):
            name = str(tool_call.get("name") or "")
            result = str(tool_call.get("result") or "")
            evidence.append({
                "tool": name,
                "args": tool_call.get("args") or {},
                "resultPreview": result[:200],
                "index": index,
            })
            if name in _MUTATING_TOOLS:
                last_mutation_index = index
                verified_after_mutation = False
                continue
            if last_mutation_index >= 0 and index > last_mutation_index and name in _VERIFICATION_TOOLS:
                verified_after_mutation = True

        if last_mutation_index < 0:
            return VerificationResult(True, evidence, "No mutating tool calls were recorded.")
        if verified_after_mutation:
            return VerificationResult(True, evidence, "Post-change verification observed after the last mutation.")

        passed = not self._required
        return VerificationResult(
            passed,
            evidence,
            "Mutations were recorded but no explicit post-change verification was observed after the last mutation.",
        )