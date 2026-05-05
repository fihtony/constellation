"""Execution verifier for connect-agent results."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


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

_BINARY_ARTIFACT_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
}

_BINARY_FORMAT_SUCCESS_MARKERS = (
    "png image data",
    "jpeg image data",
    "jpg image data",
    "gif image data",
    "web/p image",
    "webp image",
)

_BINARY_FORMAT_FAILURE_MARKERS = (
    "ascii text",
    "utf-8 unicode text",
    "unicode text",
    "empty",
    "cannot open",
    "no such file",
    "unknown-format",
)

_BINARY_FORMAT_COMMAND_HINTS = (
    "file ",
    "xxd ",
    "hexdump",
    "identify ",
    "magick identify",
    "ffprobe",
)

_BINARY_ARTIFACT_PATH_RE = re.compile(r"[\w./-]+\.(?:png|jpe?g|gif|webp)\b", re.IGNORECASE)

_BINARY_MUTATION_COMMAND_HINTS = (
    "cp ",
    "mv ",
    "install ",
    "tee ",
    "screencap",
    "screenshot",
    "screenrecord",
    "img.save(",
    ".save(",
)

_SUSPICIOUS_BINARY_GENERATION_HINTS = (
    "base64",
    "printf",
    "echo -n",
    "from pil",
    "image.new",
    "python - <<",
    "python3 - <<",
    "/usr/share/",
    "gnome-logo",
    "placeholder",
    "dummy",
)


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
        last_binary_mutation_index = -1
        binary_artifact_paths: list[str] = []
        binary_verification_observed = False
        binary_verification_passed = False
        last_binary_mutation_suspicious = False
        last_binary_mutation_reason = ""

        for index, tool_call in enumerate(tool_calls):
            name = str(tool_call.get("name") or "")
            result = str(tool_call.get("result") or "")
            args = tool_call.get("args") or {}
            evidence.append({
                "tool": name,
                "args": args,
                "resultPreview": result[:200],
                "index": index,
            })
            artifact_paths, suspicious, suspicious_reason = _inspect_binary_artifact_mutation(name, args)
            if artifact_paths:
                last_mutation_index = index
                verified_after_mutation = False
                for artifact_path in artifact_paths:
                    if artifact_path not in binary_artifact_paths:
                        binary_artifact_paths.append(artifact_path)
                last_binary_mutation_index = index
                binary_verification_observed = False
                binary_verification_passed = False
                last_binary_mutation_suspicious = suspicious
                last_binary_mutation_reason = suspicious_reason
                continue
            if name in _MUTATING_TOOLS:
                last_mutation_index = index
                verified_after_mutation = False
                path = _extract_path(args)
                if _is_binary_artifact_path(path):
                    if path not in binary_artifact_paths:
                        binary_artifact_paths.append(path)
                    last_binary_mutation_index = index
                    binary_verification_observed = False
                    binary_verification_passed = False
                    last_binary_mutation_suspicious = False
                    last_binary_mutation_reason = ""
                continue
            if last_mutation_index >= 0 and index > last_mutation_index and name in _VERIFICATION_TOOLS:
                verified_after_mutation = True
            if last_binary_mutation_index >= 0 and index > last_binary_mutation_index:
                observed, passed = _classify_binary_artifact_check(name, args, result)
                if observed:
                    binary_verification_observed = True
                    binary_verification_passed = passed

        if last_mutation_index < 0:
            return VerificationResult(True, evidence, "No mutating tool calls were recorded.")
        if binary_artifact_paths and last_binary_mutation_suspicious:
            passed = not self._required
            return VerificationResult(
                passed,
                evidence,
                last_binary_mutation_reason or (
                    "Binary artifact paths were written through a suspicious generation path rather than a real "
                    "capture, export, or render step."
                ),
            )
        if binary_artifact_paths and not binary_verification_observed:
            passed = not self._required
            return VerificationResult(
                passed,
                evidence,
                "Image-like artifact paths were mutated but no explicit binary-format verification (for example `file`) "
                "was observed after the last image mutation.",
            )
        if binary_artifact_paths and not binary_verification_passed:
            passed = not self._required
            return VerificationResult(
                passed,
                evidence,
                "Binary artifact verification ran after the last image mutation, but it did not confirm a real image format.",
            )
        if verified_after_mutation:
            return VerificationResult(True, evidence, "Post-change verification observed after the last mutation.")

        passed = not self._required
        return VerificationResult(
            passed,
            evidence,
            "Mutations were recorded but no explicit post-change verification was observed after the last mutation.",
        )


def _extract_path(args: dict) -> str:
    path = args.get("path")
    return path if isinstance(path, str) else ""


def _is_binary_artifact_path(path: str) -> bool:
    if not path:
        return False
    return Path(path).suffix.lower() in _BINARY_ARTIFACT_EXTENSIONS


def detect_suspicious_binary_artifact_mutations(tool_calls: list[dict]) -> list[dict]:
    suspicious_mutations: list[dict] = []
    for index, tool_call in enumerate(tool_calls):
        name = str(tool_call.get("name") or "")
        args = tool_call.get("args") or {}
        artifact_paths, suspicious, reason = _inspect_binary_artifact_mutation(name, args)
        if artifact_paths and suspicious:
            suspicious_mutations.append({
                "index": index,
                "tool": name,
                "paths": artifact_paths,
                "reason": reason,
                "command": str(args.get("command") or "")[:400],
            })
    return suspicious_mutations


def _inspect_binary_artifact_mutation(name: str, args: dict) -> tuple[list[str], bool, str]:
    path = _extract_path(args)
    if name in {"write_file", "edit_file"} and _is_binary_artifact_path(path):
        return [path], True, (
            "Binary artifact paths were written with a text-file editing tool. "
            "Image evidence must come from a real capture, export, or render step."
        )

    if name != "bash":
        return [], False, ""

    command = str(args.get("command") or "")
    if not command:
        return [], False, ""

    artifact_paths = _extract_binary_artifact_paths(command)
    if not artifact_paths or not _looks_like_binary_artifact_mutation(command):
        return [], False, ""

    if _looks_like_suspicious_binary_generation(command):
        return artifact_paths, True, (
            "Binary artifact paths were generated via inline bytes, placeholder graphics, or unrelated sample/system assets. "
            "Evidence images must come from an actual capture, exported design asset, or deterministic render of the requested UI."
        )
    return artifact_paths, False, ""


def _extract_binary_artifact_paths(text: str) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for match in _BINARY_ARTIFACT_PATH_RE.finditer(text):
        candidate = match.group(0)
        if candidate not in seen:
            seen.append(candidate)
    return seen


def _looks_like_binary_artifact_mutation(command: str) -> bool:
    lower_command = command.lower()
    if re.search(r">\s*[\w./-]+\.(?:png|jpe?g|gif|webp)\b", lower_command):
        return True
    if any(hint in lower_command for hint in _BINARY_MUTATION_COMMAND_HINTS):
        return True
    if any(keyword in lower_command for keyword in _SUSPICIOUS_BINARY_GENERATION_HINTS):
        return True
    return False


def _looks_like_suspicious_binary_generation(command: str) -> bool:
    lower_command = command.lower()
    if any(hint in lower_command for hint in _SUSPICIOUS_BINARY_GENERATION_HINTS):
        return True
    if re.search(r"\bcp\b[^\n;|&]*(?:/usr/share/|gnome-logo|placeholder|dummy)[^\n;|&]*\.(?:png|jpe?g|gif|webp)\b", lower_command):
        return True
    return False


def _classify_binary_artifact_check(name: str, args: dict, result: str) -> tuple[bool, bool]:
    if name != "bash":
        return False, False

    command = str(args.get("command") or "").lower()
    output = result.lower()
    mentions_binary_check = any(hint in command for hint in _BINARY_FORMAT_COMMAND_HINTS)
    mentions_binary_result = any(marker in output for marker in _BINARY_FORMAT_SUCCESS_MARKERS + _BINARY_FORMAT_FAILURE_MARKERS)
    if not mentions_binary_check and not mentions_binary_result:
        return False, False

    if any(marker in output for marker in _BINARY_FORMAT_SUCCESS_MARKERS):
        return True, True
    return True, False