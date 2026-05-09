"""Local agentic test for the Android agent using pre-fetched test data.

Runs the connect-agent agentic runtime directly against the local test repo
at tests/data/android/android-test/ using Jira and Figma context from
tests/data/android/{jira,ui-design}/.

Usage:
    python3 tests/test_android_agentic.py [--assess-only] [--timeout 3600]

Modes:
    default    : run the agentic implementation then self-assess
    --assess-only : skip the agentic run; only assess an existing implementation
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

# ── path bootstrap ─────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:1288/v1")
os.environ.setdefault("AGENT_RUNTIME", "connect-agent")

from common.env_utils import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / "common" / ".env")
load_dotenv(REPO_ROOT / "android" / ".env")

from common.runtime.adapter import get_runtime  # noqa: E402
from common.runtime.connect_agent.verifier import detect_suspicious_binary_artifact_mutations  # noqa: E402
from common.prompt_builder import build_system_prompt_from_manifest  # noqa: E402
from android.agentic_workflow import build_android_task_prompt  # noqa: E402

# ── paths ───────────────────────────────────────────────────────────────────
DATA_DIR = REPO_ROOT / "tests" / "data" / "android"
JIRA_DIR = DATA_DIR / "jira"
FIGMA_DIR = DATA_DIR / "ui-design"
REPO_DIR = DATA_DIR / "android-test"

JIRA_SUMMARY_FILE = JIRA_DIR / "jira-summary.md"
JIRA_ISSUE_FILE = JIRA_DIR / "jira-issue.json"
FIGMA_DATA_FILE = next(FIGMA_DIR.glob("figma-data-*.json"), None)


# ---------------------------------------------------------------------------
# Context loading helpers
# ---------------------------------------------------------------------------

def load_jira_context() -> dict:
    """Load Jira ticket context from pre-fetched files."""
    summary_text = ""
    if JIRA_SUMMARY_FILE.exists():
        summary_text = JIRA_SUMMARY_FILE.read_text(encoding="utf-8")

    issue_data: dict = {}
    if JIRA_ISSUE_FILE.exists():
        try:
            issue_data = json.loads(JIRA_ISSUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Extract key + title from summary markdown
    ticket_key = ""
    ticket_title = ""
    ticket_status = ""
    key_match = re.search(r"Key:\s+(\S+)", summary_text)
    title_match = re.search(r"Summary:\s+(.+)", summary_text)
    status_match = re.search(r"Status:\s+(.+)", summary_text)
    if key_match:
        ticket_key = key_match.group(1).strip()
    if title_match:
        ticket_title = title_match.group(1).strip()
    if status_match:
        ticket_status = status_match.group(1).strip()

    # Fallback to raw JSON fields
    if not ticket_key and issue_data:
        ticket_key = str(issue_data.get("key") or "")
    if not ticket_title and issue_data:
        fields = issue_data.get("fields") or {}
        ticket_title = str(fields.get("summary") or "")

    return {
        "key": ticket_key,
        "title": ticket_title,
        "status": ticket_status,
        "summary_text": summary_text,
        "issue_data": issue_data,
    }


def _hex_color(fills: list) -> str:
    """Convert a Figma fills list to a #RRGGBB hex string."""
    if not fills:
        return ""
    c = (fills[0].get("color") or {})
    r = int(round((c.get("r") or 0) * 255))
    g = int(round((c.get("g") or 0) * 255))
    b = int(round((c.get("b") or 0) * 255))
    return f"#{r:02X}{g:02X}{b:02X}"


def extract_design_spec_from_figma(figma_path: Path) -> str:
    """Parse the Figma JSON into a structured, implementation-ready design specification.

    Produces two sections:
      1. STRUCTURED ITEMS  – a concise list of all TEXT nodes with typography and colour.
         Useful for the agent to directly match text, colour, and font in layouts.
      2. FULL NODE TREE    – detailed hierarchy for spatial and structural reference.
    """
    if not figma_path or not figma_path.exists():
        return "(no Figma design data available)"

    try:
        data = json.loads(figma_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"(could not parse Figma data: {exc})"

    file_meta = data.get("fileMeta") or {}
    figma_url = data.get("figmaUrl") or ""
    file_name = file_meta.get("name") or "Unnamed Figma file"

    # ── Section 1: Collect all TEXT nodes into a flat, structured table ──────
    text_items: list[dict] = []  # {chars, font, size, weight, color, path}
    frame_colors: list[dict] = []  # {name, bg_color, width, height}

    def _collect_text(node: dict, path: str = "") -> None:
        if not isinstance(node, dict):
            return
        ntype = node.get("type") or ""
        name = node.get("name") or ""
        current_path = f"{path}/{name}" if name else path

        if ntype == "TEXT":
            chars = node.get("characters") or ""
            if chars.strip():
                style = node.get("style") or {}
                fills = node.get("fills") or []
                text_items.append({
                    "chars": chars,
                    "font": style.get("fontFamily") or "",
                    "size": style.get("fontSize") or "",
                    "weight": style.get("fontWeight") or "",
                    "color": _hex_color(fills),
                    "path": current_path,
                })
        elif ntype in ("FRAME", "INSTANCE") and name:
            bbox = node.get("absoluteBoundingBox") or {}
            fills = node.get("fills") or []
            bg = _hex_color(fills)
            if bg:
                frame_colors.append({
                    "name": name,
                    "bg": bg,
                    "w": bbox.get("width") or "",
                    "h": bbox.get("height") or "",
                })

        for child in (node.get("children") or []):
            _collect_text(child, current_path)

    # ── Section 2: Full node tree walk for hierarchy reference ───────────────
    def _walk(node: dict, depth: int = 0) -> list[str]:
        lines: list[str] = []
        if not isinstance(node, dict):
            return lines
        ntype = node.get("type") or ""
        name = node.get("name") or ""
        indent = "  " * depth

        if ntype == "TEXT":
            chars = node.get("characters") or ""
            if not chars.strip():
                return lines
            style = node.get("style") or {}
            fills = node.get("fills") or []
            colour = _hex_color(fills)
            font = style.get("fontFamily") or ""
            size = style.get("fontSize") or ""
            weight = style.get("fontWeight") or ""
            lines.append(
                f"{indent}[TEXT] \"{chars}\""
                f"  font={font} size={size}sp w={weight} color={colour}"
            )
        elif ntype in ("FRAME", "GROUP", "SECTION", "COMPONENT", "INSTANCE"):
            bbox = node.get("absoluteBoundingBox") or {}
            w = bbox.get("width") or ""
            h = bbox.get("height") or ""
            size_str = f"  {w}×{h}px" if w and h else ""
            fills = node.get("fills") or []
            bg = _hex_color(fills)
            bg_str = f"  bg={bg}" if bg else ""
            lines.append(f"{indent}[{ntype}] {name}{size_str}{bg_str}")
            for child in (node.get("children") or []):
                lines.extend(_walk(child, depth + 1))
        elif ntype in ("ELLIPSE", "RECTANGLE"):
            bbox = node.get("absoluteBoundingBox") or {}
            w = bbox.get("width") or ""
            h = bbox.get("height") or ""
            fills = node.get("fills") or []
            colour = _hex_color(fills)
            lines.append(f"{indent}[{ntype}] {name}  {w}×{h}px  fill={colour}")
        elif ntype == "VECTOR":
            fills = node.get("fills") or []
            colour = _hex_color(fills)
            lines.append(f"{indent}[VECTOR] {name}  fill={colour}")

        return lines

    # Walk all nodes
    nodes_section = data.get("nodes") or {}
    nodes_inner = nodes_section.get("nodes") or {}
    full_tree_lines: list[str] = []
    for node_id, node_data in nodes_inner.items():
        doc = node_data.get("document") if isinstance(node_data, dict) else node_data
        if doc:
            _collect_text(doc)
            full_tree_lines.append(f"--- Node {node_id} ---")
            full_tree_lines.extend(_walk(doc))
            full_tree_lines.append("")

    spec_lines: list[str] = [
        f"File: {file_name}",
        f"URL:  {figma_url}",
        "",
        "== STRUCTURED TEXT ELEMENTS ==",
        "  (Use these directly for android:text, textSize, textColor values in XML layouts)",
        "",
    ]

    # Emit deduplicated text items grouped by content
    seen_chars: set[str] = set()
    for item in text_items:
        chars = item["chars"]
        if chars in seen_chars:
            continue
        seen_chars.add(chars)
        spec_lines.append(
            f'  TEXT: "{chars}"'
            f'  textSize={item["size"]}sp  textColor={item["color"]}'
            f'  font={item["font"]} weight={item["weight"]}'
        )

    spec_lines.append("")
    spec_lines.append("== KEY FRAME/INSTANCE BACKGROUNDS ==")
    spec_lines.append("  (Use these for android:background attributes)")
    spec_lines.append("")
    seen_names: set[str] = set()
    for fc in frame_colors[:20]:
        key = fc["name"]
        if key in seen_names:
            continue
        seen_names.add(key)
        spec_lines.append(f'  [{fc["name"]}]  bg={fc["bg"]}  {fc["w"]}×{fc["h"]}px')

    spec_lines.append("")
    spec_lines.append("== FULL NODE TREE ==")
    spec_lines.append("")
    spec_lines.extend(full_tree_lines)

    return "\n".join(spec_lines) if spec_lines else "(Figma data present but no parseable nodes)"


def build_repo_info(repo_dir: Path) -> tuple[str, str, str]:
    """Return (package_name, build_file, extra_repo_info) from the local repo."""
    build_file = "app/build.gradle.kts"
    package_name = "com.example.androidtest"
    extra_parts: list[str] = []

    # Find package from AndroidManifest
    manifest = repo_dir / "app" / "src" / "main" / "AndroidManifest.xml"
    if manifest.exists():
        text = manifest.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'package="([^"]+)"', text)
        if m:
            package_name = m.group(1)

    # Detect build file
    for candidate in ("app/build.gradle.kts", "app/build.gradle", "build.gradle.kts", "build.gradle"):
        if (repo_dir / candidate).exists():
            build_file = candidate
            break

    # Existing source structure
    src_root = repo_dir / "app" / "src" / "main" / "java"
    if src_root.exists():
        kotlin_files = sorted(src_root.rglob("*.kt"))[:15]
        if kotlin_files:
            extra_parts.append(
                "Existing Kotlin sources:\n"
                + "\n".join(f"  {f.relative_to(repo_dir)}" for f in kotlin_files)
            )

    # Existing test structure
    test_root = repo_dir / "app" / "src" / "test"
    if test_root.exists():
        test_files = sorted(test_root.rglob("*.kt"))[:10]
        if test_files:
            extra_parts.append(
                "Existing unit tests:\n"
                + "\n".join(f"  {f.relative_to(repo_dir)}" for f in test_files)
            )

    return package_name, build_file, "\n".join(extra_parts)


def build_task_prompt(jira: dict, design_spec: str, repo_dir: Path) -> str:
    """Build the full agentic task prompt from pre-loaded context."""
    package_name, build_file, extra_repo_info = build_repo_info(repo_dir)

    # Extract structured acceptance criteria and deliverables from the jira summary
    summary_text = jira.get("summary_text") or ""

    # Pull acceptance criteria section from the summary markdown
    deliverables_text = ""
    if "Acceptance criteria" in summary_text or "acceptance criteria" in summary_text:
        ac_match = re.search(
            r"(?:Acceptance criteria[^:]*:)(.*?)(?=\n#|\Z)",
            summary_text,
            re.DOTALL | re.IGNORECASE,
        )
        if ac_match:
            deliverables_text = ac_match.group(1).strip()

    if not deliverables_text:
        # Fall back to the entire hints section
        hints_match = re.search(
            r"(?:Implementation hints[^:]*:)(.*?)(?=\n#|\Z)",
            summary_text,
            re.DOTALL | re.IGNORECASE,
        )
        if hints_match:
            deliverables_text = hints_match.group(1).strip()

    if not deliverables_text:
        deliverables_text = (
            "Implement the feature described in the Jira ticket with unit tests and evidence files."
        )

    description_text = summary_text[:4000] if summary_text else jira.get("title") or ""

    return build_android_task_prompt(
        user_text=description_text,
        workspace=str(REPO_DIR),
        compass_task_id="test-compass",
        android_task_id="test-android",
        acceptance_criteria=deliverables_text,
        target_repo_url="",
        jira_context=jira,
        ticket_key=jira.get("key") or "UNKNOWN",
    )


# ---------------------------------------------------------------------------
# Self-assessment helpers
# ---------------------------------------------------------------------------

def run_gradle_test(repo_dir: Path) -> tuple[bool, str]:
    """Run testDebugUnitTest and return (success, output)."""
    gradle_cmd = "./gradlew"
    if not (repo_dir / "gradlew").exists():
        return False, "gradlew not found"

    # Ensure gradlew is executable
    os.chmod(repo_dir / "gradlew", 0o755)

    env = dict(os.environ)
    env["CI"] = "true"
    android_home = env.get("ANDROID_HOME") or env.get("ANDROID_SDK_ROOT") or ""
    if android_home:
        env.setdefault("ANDROID_HOME", android_home)
        env.setdefault("ANDROID_SDK_ROOT", android_home)

    # Write local.properties if needed
    local_props = repo_dir / "local.properties"
    if android_home and not local_props.exists():
        local_props.write_text(f"sdk.dir={android_home}\n", encoding="utf-8")

    cmd = [
        gradle_cmd,
        "testDebugUnitTest",
        "--no-daemon",
        "--max-workers=1",
        "-Pkotlin.compiler.execution.strategy=in-process",
        "-Dkotlin.daemon.enabled=false",
        "-Dorg.gradle.vfs.watch=false",
        "--console=plain",
    ]

    print(f"\n[assess] Running: {' '.join(cmd)}")
    start = time.time()
    result = subprocess.run(
        cmd,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
    )
    elapsed = time.time() - start
    output = f"{result.stdout}\n{result.stderr}".strip()
    success = result.returncode == 0
    print(f"[assess] Build {'PASSED' if success else 'FAILED'} in {elapsed:.0f}s")
    if not success:
        # Print last 60 lines of failure
        lines = output.splitlines()
        for line in lines[-60:]:
            print(f"  {line}")
    return success, output


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    index = 2
    size = len(data)
    while index + 8 <= size:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xD8, 0xD9}:
            index += 2
            continue
        if index + 4 > size:
            return None
        segment_length = int.from_bytes(data[index + 2:index + 4], "big")
        if segment_length < 2 or index + 2 + segment_length > size:
            return None
        if 0xC0 <= marker <= 0xC3 and index + 9 <= size:
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        index += 2 + segment_length
    return None


def _image_dimensions(path: Path, data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:2] == b"\xff\xd8":
        return _jpeg_dimensions(data)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
    return None


def _image_artifact_status(path: Path) -> tuple[bool, str, tuple[int, int] | None]:
    """Return whether an evidence image exists, is non-empty, and looks readable."""
    if not path.exists():
        return False, "missing", None
    if path.stat().st_size <= 0:
        return False, "empty", None
    try:
        data = path.read_bytes()
    except OSError as exc:
        return False, f"unreadable ({exc})", None

    header = data[:16]
    dimensions = _image_dimensions(path, data)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return True, _format_image_detail("png", dimensions), dimensions
    if header[:2] == b"\xff\xd8":
        return True, _format_image_detail("jpeg", dimensions), dimensions
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return True, _format_image_detail("gif", dimensions), dimensions
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return True, _format_image_detail("webp", dimensions), dimensions
    return False, "unknown-format", dimensions


def _format_image_detail(image_type: str, dimensions: tuple[int, int] | None) -> str:
    if not dimensions:
        return image_type
    return f"{image_type} {dimensions[0]}x{dimensions[1]}"


def _filename_expected_dimensions(path: Path) -> tuple[int, int] | None:
    match = re.search(r"(\d+)x(\d+)", path.stem)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _suspicious_evidence_generation(tool_calls: list[dict]) -> list[dict]:
    suspicious = detect_suspicious_binary_artifact_mutations(tool_calls)
    return [
        item for item in suspicious
        if any(path.startswith("docs/evidence/") for path in item.get("paths") or [])
    ]


def _has_required_entry_path(repo_dir: Path) -> bool:
    """Check whether Favorites navigation is wired to the contributions screen."""
    source_root = repo_dir / "app" / "src" / "main"
    host_files = list(source_root.rglob("*.kt")) + list(source_root.rglob("*.java")) + list(source_root.rglob("*nav*.xml"))
    favorites_tokens = ("Favorites", "Favorite", "FAVORITES")
    destination_tokens = ("ContributionsFragment", '"contributions"', "ui.contributions")
    for path in host_files:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(token in text for token in favorites_tokens) and any(token in text for token in destination_tokens):
                return True
    return False


def assess_implementation(repo_dir: Path, design_spec: str, tool_calls: list[dict] | None = None) -> dict:
    """
    Independently assess the implementation quality.

    Returns a dict with:
      - files_created: list of files added/changed
      - has_fragment: bool
      - has_recyclerview: bool
      - has_adapter: bool
      - has_unit_tests: bool
      - has_evidence: bool
            - has_required_entry_path: bool
            - has_real_evidence_images: bool
      - has_robolectric_config: bool — @RunWith(RobolectricTestRunner) + @Config(sdk=[28])
      - has_viewbinding_or_findviewbyid: bool — no deprecated synthetic imports
      - has_recyclerview_dep: bool — RecyclerView dep in build.gradle
      - uses_synthetic: bool — TRUE means deprecated kotlinx.android.synthetic used
      - design_coverage: list of matched design elements
      - design_colors_matched: list of (element, expected_color, found_color) matches
      - gaps: list of identified gaps
    """
    result: dict = {
        "files_created": [],
        "has_fragment": False,
        "has_recyclerview": False,
        "has_adapter": False,
        "has_unit_tests": False,
        "has_evidence": False,
        "has_required_entry_path": False,
        "has_real_evidence_images": False,
        "evidence_provenance_ok": True,
        "has_robolectric_config": False,
        "has_viewbinding_or_findviewbyid": False,
        "has_recyclerview_dep": False,
        "uses_synthetic": False,
        "design_coverage": [],
        "design_colors_matched": [],
        "evidence_artifacts": {},
        "suspicious_evidence_generation": [],
        "gaps": [],
    }

    src_root = repo_dir / "app" / "src"

    # Collect all Kotlin and layout files
    kt_files = list(src_root.rglob("*.kt")) if src_root.exists() else []
    xml_files = list((repo_dir / "app" / "src" / "main" / "res").rglob("*.xml")) if (repo_dir / "app" / "src" / "main" / "res").exists() else []
    evidence_dir = repo_dir / "docs" / "evidence"

    result["files_created"] = [
        str(f.relative_to(repo_dir))
        for f in kt_files + xml_files
        if f.stat().st_mtime > (time.time() - 86400 * 7)  # created in the last week
    ]

    for kt_file in kt_files:
        content = kt_file.read_text(encoding="utf-8", errors="replace")
        name = kt_file.name.lower()
        if "fragment" in name or ": fragment()" in content or "extends fragment" in content.lower():
            result["has_fragment"] = True
        if "recyclerview" in content.lower() or "recycler_view" in content.lower():
            result["has_recyclerview"] = True
        if "adapter" in name or "recyclerviewadapter" in content.lower() or ": recyclerview.adapter" in content.lower() or ": listadapter" in content.lower():
            result["has_adapter"] = True

        # Detect deprecated synthetic imports (Kotlin 2.0 incompatible)
        if "kotlinx.android.synthetic" in content:
            result["uses_synthetic"] = True

        # Detect ViewBinding or normal findViewByID usage
        if "Binding.inflate" in content or "binding." in content or "findviewbyid" in content.lower():
            result["has_viewbinding_or_findviewbyid"] = True

    for kt_file in kt_files:
        path_str = str(kt_file)
        if "/test/" in path_str or "/androidTest/" in path_str:
            result["has_unit_tests"] = True
            content = kt_file.read_text(encoding="utf-8", errors="replace")
            if "RobolectricTestRunner" in content and "@Config(sdk" in content:
                result["has_robolectric_config"] = True

    # Check RecyclerView dependency in build.gradle.kts
    build_gradle = repo_dir / "app" / "build.gradle.kts"
    if build_gradle.exists():
        bg_content = build_gradle.read_text(encoding="utf-8", errors="replace").lower()
        if "recyclerview" in bg_content:
            result["has_recyclerview_dep"] = True

    result["has_evidence"] = evidence_dir.exists() and any(evidence_dir.iterdir()) if evidence_dir.exists() else False
    required_images = [
        evidence_dir / "design-reference.png",
        evidence_dir / "screenshot-1080x1920.png",
    ]
    evidence_ok = True
    for image_path in required_images:
        ok, detail, dimensions = _image_artifact_status(image_path)
        expected_dimensions = _filename_expected_dimensions(image_path)
        if ok and expected_dimensions and dimensions and dimensions != expected_dimensions:
            ok = False
            detail = f"{detail} (expected {expected_dimensions[0]}x{expected_dimensions[1]})"
        result["evidence_artifacts"][image_path.name] = detail
        evidence_ok = evidence_ok and ok
    suspicious_evidence = _suspicious_evidence_generation(tool_calls or [])
    result["suspicious_evidence_generation"] = suspicious_evidence
    result["evidence_provenance_ok"] = not suspicious_evidence
    result["has_real_evidence_images"] = evidence_ok and result["evidence_provenance_ok"]
    result["has_required_entry_path"] = _has_required_entry_path(repo_dir)

    # ── Design spec element coverage ─────────────────────────────────────────
    # Extract key text values from design_spec (from the STRUCTURED TEXT section)
    design_texts: list[str] = []
    for line in design_spec.splitlines():
        if line.strip().startswith('TEXT:'):
            # Extract the quoted text content
            m = re.search(r'TEXT:\s+"([^"]+)"', line)
            if m:
                design_texts.append(m.group(1))

    # Check if list item texts appear in the implementation
    contribution_texts_found = 0
    for design_text in design_texts:
        if not design_text.strip() or len(design_text) < 5:
            continue
        text_lower = design_text.lower()
        for kt_file in kt_files:
            if design_text in kt_file.read_text(encoding="utf-8", errors="replace"):
                contribution_texts_found += 1
                result["design_coverage"].append(f'text:"{design_text[:40]}"')
                break

    # Legacy fallback: if no structured texts parsed, check raw substring
    if not result["design_coverage"]:
        if "Make a new contribution" in design_spec:
            for kt_file in kt_files:
                content = kt_file.read_text(encoding="utf-8", errors="replace")
                if "contribution" in content.lower():
                    result["design_coverage"].append("contribution model/data")
                    break

    # ── Color verification ────────────────────────────────────────────────────
    # Extract expected colors from design_spec structured section
    expected_colors: dict[str, str] = {}  # text → color
    for line in design_spec.splitlines():
        if line.strip().startswith('TEXT:'):
            m_text = re.search(r'TEXT:\s+"([^"]+)"', line)
            m_color = re.search(r'textColor=(#[0-9A-Fa-f]{6})', line)
            if m_text and m_color:
                expected_colors[m_text.group(1)] = m_color.group(1).upper()

    # Collect all color values used in xml layouts
    xml_colors_used: set[str] = set()
    for xml_file in xml_files:
        xml_content = xml_file.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'#([0-9A-Fa-f]{6})', xml_content):
            xml_colors_used.add(f"#{m.group(1).upper()}")

    # Match primary text color from design spec
    # Collect unique colors from TEXT items at title size (16sp), excluding icon fonts
    title_colors = set()
    for line in design_spec.splitlines():
        if line.strip().startswith('TEXT:') and 'textSize=16' in line:
            # Skip icon fonts (Font Awesome, material icons, etc.)
            if 'font=Font Awesome' in line or 'font=Material' in line:
                continue
            m = re.search(r'textColor=(#[0-9A-Fa-f]{6})', line)
            if m:
                title_colors.add(m.group(1).upper())

    for tc in title_colors:
        if tc in xml_colors_used:
            result["design_colors_matched"].append(f"title color {tc}")
            result["design_coverage"].append(f"title color {tc}")

    subtitle_colors = set()
    for line in design_spec.splitlines():
        if line.strip().startswith('TEXT:') and 'textSize=14' in line:
            m = re.search(r'textColor=(#[0-9A-Fa-f]{6})', line)
            if m:
                subtitle_colors.add(m.group(1).upper())
    for sc in subtitle_colors:
        if sc in xml_colors_used:
            result["design_coverage"].append(f"subtitle color {sc}")

    # ── Identify gaps ─────────────────────────────────────────────────────────
    if result["uses_synthetic"]:
        result["gaps"].append(
            "CRITICAL: kotlinx.android.synthetic imports detected. "
            "This plugin was REMOVED in Kotlin 2.0 (this project uses Kotlin 2.0.21). "
            "Replace with ViewBinding (add viewBinding=true to buildFeatures{}) or "
            "standard view.findViewById<T>(R.id.xxx) calls."
        )
    if not result["has_viewbinding_or_findviewbyid"] and result["has_fragment"]:
        result["gaps"].append(
            "Fragment does not appear to use ViewBinding or findViewById — "
            "verify how views are accessed."
        )
    if not result["has_recyclerview_dep"]:
        result["gaps"].append(
            "RecyclerView dependency not found in app/build.gradle.kts. "
            "Add: implementation(\"androidx.recyclerview:recyclerview:1.3.2\")"
        )
    if not result["has_fragment"]:
        result["gaps"].append("No Fragment class detected. Jira requires Fragment + RecyclerView, NOT Compose.")
    if not result["has_recyclerview"]:
        result["gaps"].append("No RecyclerView found. Use RecyclerView in the Fragment layout.")
    if not result["has_adapter"]:
        result["gaps"].append("No RecyclerView Adapter class detected.")
    if not result["has_unit_tests"]:
        result["gaps"].append("No unit tests created. Jira requires FragmentRenderTest, AdapterBindTest, EmptyStateTest.")
    elif not result["has_robolectric_config"]:
        result["gaps"].append(
            "Unit tests lack proper Robolectric config. "
            "Use @RunWith(RobolectricTestRunner::class) and @Config(sdk = [28]) on each Robolectric test class."
        )
    if not result["has_evidence"]:
        result["gaps"].append("No docs/evidence/ directory. Jira requires evidence files (self-review.md at minimum).")
    if not result["has_real_evidence_images"]:
        result["gaps"].append(
            "Required evidence images are missing, empty, or unreadable. design-reference.png and "
            "screenshot-1080x1920.png must be real non-empty image files."
        )
    if not result["evidence_provenance_ok"]:
        result["gaps"].append(
            "Evidence images were generated through suspicious shell commands (for example inline bytes, placeholder graphics, "
            "or sample/system assets) instead of an actual capture, export, or deterministic UI render."
        )
    if not result["has_required_entry_path"]:
        result["gaps"].append(
            "Required user entry path is not wired. The ticket says tapping Favorites bottom menu "
            "must show the Your contributions page, but host/navigation code does not reference it."
        )
    if not result["design_coverage"]:
        result["gaps"].append("No design spec elements matched in implementation. Check text content and colors against Figma spec.")

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assess-only", action="store_true",
                   help="Skip the agentic run; only assess an existing implementation.")
    p.add_argument("--timeout", type=int, default=3600,
                   help="Agentic run timeout in seconds (default 3600).")
    p.add_argument("--max-turns", type=int, default=80,
                   help="Maximum agent loop turns (default 80).")
    p.add_argument("--no-build", action="store_true",
                   help="Skip the Gradle validation step during assessment.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print(f"[test] Repo root:       {REPO_ROOT}")
    print(f"[test] Android repo:    {REPO_DIR}")
    print(f"[test] Jira data:       {JIRA_SUMMARY_FILE}")
    print(f"[test] Figma data:      {FIGMA_DATA_FILE}")

    if not REPO_DIR.exists():
        print(f"[test] ERROR: Android test repo not found at {REPO_DIR}")
        return 1

    # Load context
    print("\n[test] Loading Jira context…")
    jira = load_jira_context()
    print(f"[test] Ticket: {jira['key']} — {jira['title']}")

    print("[test] Extracting Figma design spec…")
    design_spec = extract_design_spec_from_figma(FIGMA_DATA_FILE) if FIGMA_DATA_FILE else "(no Figma data)"
    print(f"[test] Design spec: {len(design_spec)} chars")

    # ── Phase 1: Agentic run ──────────────────────────────────────────────
    if not args.assess_only:
        task_prompt = build_task_prompt(jira, design_spec, REPO_DIR)
        print(f"\n[test] Task prompt: {len(task_prompt)} chars")
        print("─" * 60)
        print(task_prompt[:800] + ("…" if len(task_prompt) > 800 else ""))
        print("─" * 60)

        runtime = get_runtime()
        print(f"\n[test] Runtime: {runtime.__class__.__name__}")

        progress_log: list[str] = []

        def on_progress(step: str) -> None:
            ts = time.strftime("%H:%M:%S")
            msg = f"[{ts}] {step}"
            print(f"[agent] {msg}")
            progress_log.append(msg)

        print(f"\n[test] Starting agentic run (timeout={args.timeout}s, max_turns={args.max_turns})…")
        start_ts = time.time()

        agentic_result = runtime.run_agentic(
            task=task_prompt,
            system_prompt=build_system_prompt_from_manifest(str(REPO_ROOT / "android")),
            cwd=str(REPO_DIR),
            tools=["bash", "read_file", "write_file", "edit_file", "glob", "grep", "todo_write"],
            max_turns=args.max_turns,
            timeout=args.timeout,
            on_progress=on_progress,
        )

        elapsed = time.time() - start_ts
        print(f"\n[test] Agentic run finished in {elapsed:.0f}s")
        print(f"[test] success={agentic_result.success}  turns={agentic_result.turns_used}  backend={agentic_result.backend_used}")
        print(f"[test] Summary:\n{agentic_result.summary[:2000]}")

        if not agentic_result.success:
            print(f"\n[test] WARNING: agentic run reported failure.")
            if agentic_result.raw_output:
                print(f"[test] Raw output tail:\n{agentic_result.raw_output[-1000:]}")
    else:
        print("\n[test] --assess-only: skipping agentic run.")

    # ── Phase 2: Self-assessment ──────────────────────────────────────────
    print("\n" + "=" * 60)
    print("[assess] SELF-ASSESSMENT")
    print("=" * 60)

    assessment = assess_implementation(
        REPO_DIR,
        design_spec,
        tool_calls=agentic_result.tool_calls if not args.assess_only else [],
    )

    print(f"\n[assess] Files recently created/modified:")
    for f in assessment["files_created"][:20]:
        print(f"  {f}")

    print(f"\n[assess] Structural checks:")
    print(f"  Fragment present:          {assessment['has_fragment']}")
    print(f"  RecyclerView used:         {assessment['has_recyclerview']}")
    print(f"  Adapter created:           {assessment['has_adapter']}")
    print(f"  Unit tests exist:          {assessment['has_unit_tests']}")
    print(f"  Robolectric config ok:     {assessment['has_robolectric_config']}")
    print(f"  ViewBinding/findViewById:  {assessment['has_viewbinding_or_findviewbyid']}")
    print(f"  RecyclerView dep present:  {assessment['has_recyclerview_dep']}")
    print(f"  Uses synthetic (BAD):      {assessment['uses_synthetic']}")
    print(f"  Entry path wired:          {assessment['has_required_entry_path']}")
    print(f"  Evidence files:            {assessment['has_evidence']}")
    print(f"  Real evidence images:      {assessment['has_real_evidence_images']}")
    print(f"  Evidence provenance ok:    {assessment['evidence_provenance_ok']}")
    print(f"  Evidence artifact types:   {assessment['evidence_artifacts']}")
    if assessment["suspicious_evidence_generation"]:
        print(f"  Suspicious evidence cmds:  {assessment['suspicious_evidence_generation'][:3]}")

    print(f"\n[assess] Design coverage: {assessment['design_coverage']}")
    print(f"[assess] Colors matched:   {assessment['design_colors_matched']}")

    build_passed: bool | None = None
    if not args.no_build:
        print("\n[assess] Running Gradle unit tests…")
        build_passed, build_output = run_gradle_test(REPO_DIR)
        print(f"[assess] Gradle result: {'PASS' if build_passed else 'FAIL'}")

    print("\n[assess] Identified gaps:")
    if assessment["gaps"]:
        for gap in assessment["gaps"]:
            print(f"  ✗ {gap}")
    else:
        print("  (none)")

    if build_passed is not None:
        if not build_passed:
            assessment["gaps"].append("Gradle testDebugUnitTest failed — see build output above.")

    # ── Phase 3: Report ───────────────────────────────────────────────────
    # Score breakdown (10 points):
    #   +1 Fragment present
    #   +1 RecyclerView used
    #   +1 Adapter created
    #   +1 Unit tests exist
    #   +1 No synthetic imports (Kotlin 2.0 compatible)
    #   +1 Robolectric configured correctly
    #   +1 Required entry path wired
    #   +1 Evidence files present
    #   +1 Evidence images are real
    #   +1 Gradle build passes
    score = 0
    max_score = 10
    if assessment["has_fragment"]:
        score += 1
    if assessment["has_recyclerview"]:
        score += 1
    if assessment["has_adapter"]:
        score += 1
    if assessment["has_unit_tests"]:
        score += 1
    if not assessment["uses_synthetic"]:
        score += 1
    if assessment["has_robolectric_config"]:
        score += 1
    if assessment["has_required_entry_path"]:
        score += 1
    if assessment["has_evidence"]:
        score += 1
    if assessment["has_real_evidence_images"]:
        score += 1
    if build_passed:
        score += 1

    print(f"\n[assess] Score: {score}/{max_score}")

    if score < max_score or assessment["gaps"]:
        print("\n[assess] GAPS FOUND — improvements needed:")
        for gap in assessment["gaps"]:
            print(f"  → {gap}")
        print(
            "\nSuggested improvements based on gaps:\n"
            "  • If no Fragment: tighten system prompt to emphasize Fragment+RV over Compose\n"
            "  • If no tests:    add explicit test class names to task prompt deliverables\n"
            "  • If no evidence: add docs/evidence/ instructions and real-artifact validation to task template\n"
            "  • If screen is unreachable: add host-navigation wiring and entry-path verification requirements\n"
            "  • If build fails: review system prompt BUILD section and add Robolectric SDK config hints\n"
        )
        return 1
    else:
        print("\n[assess] All checks PASSED — implementation meets acceptance criteria.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
