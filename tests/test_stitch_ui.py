#!/usr/bin/env python3
"""Stitch UI test: build Linguist Library landing page with React + Tailwind.

This test drives the connect-agent runtime to create a full React + Tailwind
project from the Google Stitch design reference in:
  reference/stitch_open_english_study_hub/

The agent is expected to:
  1. Scaffold a Vite + React project with Tailwind v3 + postcss.config.js
  2. Configure Tailwind with design tokens from the spec
  3. Implement all design sections as React components
  4. Run `npm run build` and fix any errors
  5. Screenshot is captured and compared against reference design

Validation (independent of agent self-report):
  - CSS compilation check: dist CSS file must be > 30 KB (compiled Tailwind)
  - Screenshot capture via Playwright headless Chromium
  - Visual similarity score vs reference/stitch_open_english_study_hub/screen.png
  - Structural checks: correct colors, fonts, layout elements present

Usage:
  python3 tests/test_stitch_ui.py
  python3 tests/test_stitch_ui.py --attempt 2   # use _2 suffix
  python3 tests/test_stitch_ui.py --max-turns 80 --timeout 3600
  python3 tests/test_stitch_ui.py --attempts 5  # run 5 iterations
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from html.parser import HTMLParser

# Ensure project root is on path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Environment setup — must happen before importing runtime
# ---------------------------------------------------------------------------
os.environ["AGENT_RUNTIME"] = "connect-agent"
os.environ["AGENT_MODEL"] = os.environ.get("AGENT_MODEL", "gpt-5-mini")
os.environ["OPENAI_BASE_URL"] = os.environ.get("OPENAI_BASE_URL", "http://localhost:1288/v1")
os.environ["ALLOW_MOCK_FALLBACK"] = "0"

_DEFAULT_DESIGN_DIR = os.path.join(_REPO_ROOT, "reference", "stitch_open_english_study_hub")
_TESTS_DATA_DIR = os.path.join(_REPO_ROOT, "tests", "data")

# Compiled Tailwind output for this small landing page should be present but not bloated.
_MIN_CSS_SIZE_BYTES = 8_000
_MAX_CSS_SIZE_BYTES = 120_000

_TAILWIND_CONFIG_FORBIDDEN_PATTERNS = [
    r"safelist\\s*:",
    r"pattern\\s*:\\s*/\\.\\*/",
    r"raw\\s*:",
]

_UI_TAGS_TO_AUDIT = (
    "header",
    "nav",
    "main",
    "footer",
    "form",
    "label",
    "button",
    "a",
    "input",
    "h1",
    "h2",
    "h3",
)


def _read_file_safe(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "stitch-ui"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


class _ReferenceHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._ignored_stack: list[str] = []
        self._in_title = False
        self.title = ""
        self.visible_texts: list[str] = []
        self.tag_counts = {tag: 0 for tag in _UI_TAGS_TO_AUDIT}
        self.has_dark_classes = False

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._ignored_stack.append(tag)
        elif tag == "title":
            self._in_title = True

        if self._ignored_stack:
            return

        if tag in self.tag_counts:
            self.tag_counts[tag] += 1

        attrs_dict = dict(attrs)
        if "dark:" in str(attrs_dict.get("class", "")):
            self.has_dark_classes = True

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if self._ignored_stack and tag == self._ignored_stack[-1]:
            self._ignored_stack.pop()

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._ignored_stack:
            return
        text = _normalize_text(data)
        if not text or re.fullmatch(r"[-–—|•·:]+", text):
            return
        if self._in_title:
            self.title = text
            return
        self.visible_texts.append(text)


def _extract_required_config_snippets(design_md: str, code_html: str) -> list[str]:
    combined = f"{design_md}\n{code_html}"
    snippets: list[str] = []
    for color_name in ("primary", "secondary", "on-tertiary-container", "background"):
        match = re.search(
            rf"{re.escape(color_name)}\s*[:=]\s*['\"]?(#[0-9a-fA-F]{{6}})",
            combined,
            re.IGNORECASE,
        )
        if match:
            snippets.append(match.group(1))

    for font_name in ("Work Sans", "Newsreader"):
        if font_name.lower() in combined.lower():
            snippets.append(font_name)

    for spacing_token in ("section-padding", "container-max"):
        if spacing_token in combined:
            snippets.append(spacing_token)

    if len(snippets) < 4:
        snippets.extend(re.findall(r"#[0-9a-fA-F]{6}", combined))
    return _dedupe(snippets)[:8]


def _build_design_profile(design_dir: str) -> dict:
    design_md = _read_file_safe(os.path.join(design_dir, "DESIGN.md"))
    code_html = _read_file_safe(os.path.join(design_dir, "code.html"))

    parser = _ReferenceHtmlParser()
    parser.feed(code_html)

    page_title = parser.title or os.path.basename(os.path.normpath(design_dir))
    design_name = os.path.basename(os.path.normpath(design_dir))
    salient_texts = [text for text in _dedupe(parser.visible_texts) if len(text) >= 3][:18]

    return {
        "design_dir": design_dir,
        "design_name": design_name,
        "page_title": page_title,
        "base_project_name": _slugify(f"{design_name}-{page_title}")[:64],
        "reference_screenshot": os.path.join(design_dir, "screen.png"),
        "design_md": design_md,
        "code_html": code_html,
        "required_config_snippets": _extract_required_config_snippets(design_md, code_html),
        "expected_texts": salient_texts,
        "expected_tag_counts": {tag: count for tag, count in parser.tag_counts.items() if count > 0},
        "reference_allows_dark_mode": parser.has_dark_classes,
    }


def _load_design_content(design_profile: dict) -> dict[str, str]:
    return {
        "design_md": design_profile.get("design_md", ""),
        "code_html": design_profile.get("code_html", ""),
    }


def _save_reference_screenshot(project_dir: str, reference_screenshot: str) -> str | None:
    if not os.path.isfile(reference_screenshot):
        return None
    reference_copy_path = os.path.join(project_dir, "reference-screenshot.png")
    shutil.copy2(reference_screenshot, reference_copy_path)
    return reference_copy_path


# ---------------------------------------------------------------------------
# Independent validation — do NOT trust agent self-report
# ---------------------------------------------------------------------------

def _validate_css_compilation(project_dir: str, design_profile: dict) -> dict:
    """Check if Tailwind was properly compiled and kept minimal for this page."""
    dist_assets = os.path.join(project_dir, "dist", "assets")
    if not os.path.isdir(dist_assets):
        return {
            "compiled": False,
            "minimal_bundle": False,
            "valid": False,
            "reason": "dist/assets missing",
            "css_size_bytes": 0,
        }

    css_files = [f for f in os.listdir(dist_assets) if f.endswith(".css")]
    if not css_files:
        return {
            "compiled": False,
            "minimal_bundle": False,
            "valid": False,
            "reason": "no CSS file in dist/assets",
            "css_size_bytes": 0,
        }

    css_path = os.path.join(dist_assets, css_files[0])
    css_size = os.path.getsize(css_path)
    with open(css_path, encoding="utf-8", errors="replace") as fh:
        css_content = fh.read()
    tailwind_config = _read_file_safe(os.path.join(project_dir, "tailwind.config.js"))

    # Tailwind directives left raw means PostCSS did NOT run
    has_raw_directives = "@tailwind base" in css_content or "@tailwind components" in css_content
    has_real_utilities = (
        ".bg-" in css_content
        or ".text-" in css_content
        or ".flex{" in css_content
        or ".flex {" in css_content
        or "display:flex" in css_content
        or "display: flex" in css_content
    )
    compiled = css_size >= _MIN_CSS_SIZE_BYTES and not has_raw_directives and has_real_utilities
    required_snippets = design_profile.get("required_config_snippets", [])
    has_required_tokens = all(snippet in tailwind_config for snippet in required_snippets)
    forbidden_config_matches = [
        pattern for pattern in _TAILWIND_CONFIG_FORBIDDEN_PATTERNS if re.search(pattern, tailwind_config)
    ]
    minimal_bundle = (
        css_size <= _MAX_CSS_SIZE_BYTES
        and not forbidden_config_matches
        and has_required_tokens
    )
    valid = compiled and minimal_bundle

    reasons: list[str] = []
    if has_raw_directives:
        reasons.append("@tailwind directives were NOT processed (Tailwind PostCSS never ran)")
    elif css_size < _MIN_CSS_SIZE_BYTES:
        reasons.append(
            f"CSS too small ({css_size} bytes) — Tailwind utilities likely were not compiled"
        )
    elif not has_real_utilities:
        reasons.append("CSS has no Tailwind utility classes — build misconfigured")

    if css_size > _MAX_CSS_SIZE_BYTES:
        reasons.append(
            f"CSS too large ({css_size} bytes) — small page should not include megabytes of unused Tailwind output"
        )
    if forbidden_config_matches:
        reasons.append("tailwind.config.js contains broad safelist/raw content patterns")
    if required_snippets and not has_required_tokens:
        reasons.append("tailwind.config.js is missing required design tokens")

    reason = "OK" if not reasons else " | ".join(reasons)

    return {
        "compiled": compiled,
        "minimal_bundle": minimal_bundle,
        "valid": valid,
        "reason": reason,
        "css_size_bytes": css_size,
        "has_raw_directives": has_raw_directives,
        "has_real_utilities": has_real_utilities,
        "has_required_tokens": has_required_tokens,
        "required_snippets": required_snippets,
        "forbidden_config_matches": forbidden_config_matches,
        "css_file": css_files[0],
    }


def _iter_ui_source_files(project_dir: str) -> list[str]:
    src_dir = os.path.join(project_dir, "src")
    rel_paths: list[str] = []
    if not os.path.isdir(src_dir):
        return rel_paths
    for root, _, files in os.walk(src_dir):
        for file_name in files:
            if not file_name.endswith((".js", ".jsx", ".ts", ".tsx", ".css")):
                continue
            rel_paths.append(os.path.relpath(os.path.join(root, file_name), project_dir))
    return sorted(rel_paths)


def _page_snapshot_from_html(html: str) -> dict:
    parser = _ReferenceHtmlParser()
    parser.feed(html)
    return {
        "title": _normalize_text(parser.title),
        "body_text": _normalize_text(" ".join(parser.visible_texts)),
        "tag_counts": {tag: parser.tag_counts.get(tag, 0) for tag in _UI_TAGS_TO_AUDIT},
    }


def _find_browser_binary() -> str:
    return (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or ""
    )


def _capture_with_node_playwright(url: str, screenshot_path: str, project_dir: str) -> dict | None:
    if not shutil.which("node"):
        return None

    script = r"""
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1280 } });
  await page.goto(process.env.SNAPSHOT_URL, { waitUntil: 'networkidle', timeout: 15000 });
  await page.waitForTimeout(2500);
  const payload = await page.evaluate(() => {
    const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const counts = {};
    for (const tag of ['header', 'nav', 'main', 'footer', 'form', 'label', 'button', 'a', 'input', 'h1', 'h2', 'h3']) {
      counts[tag] = document.querySelectorAll(tag).length;
    }
    return {
      title: normalize(document.title),
      bodyText: normalize(document.body.innerText),
      tagCounts: counts,
    };
  });
  await page.screenshot({ path: process.env.SNAPSHOT_OUT });
  await browser.close();
  process.stdout.write(JSON.stringify(payload));
})().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
"""

    env = os.environ.copy()
    env["SNAPSHOT_URL"] = url
    env["SNAPSHOT_OUT"] = screenshot_path
    result = subprocess.run(
        ["node", "-e", script],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=45,
        env=env,
    )
    if result.returncode != 0 or not os.path.isfile(screenshot_path):
        return None

    payload = json.loads((result.stdout or "{}").strip() or "{}")
    return {
        "screenshot_path": screenshot_path,
        "page": {
            "title": _normalize_text(str(payload.get("title", ""))),
            "body_text": _normalize_text(str(payload.get("bodyText", ""))),
            "tag_counts": payload.get("tagCounts", {}),
        },
    }


def _capture_with_browser_binary(url: str, screenshot_path: str) -> dict | None:
    browser_bin = _find_browser_binary()
    if not browser_bin:
        return None

    screenshot_result = subprocess.run(
        [
            browser_bin,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--virtual-time-budget=5000",
            f"--screenshot={screenshot_path}",
            "--window-size=1600,1280",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if screenshot_result.returncode != 0 or not os.path.isfile(screenshot_path):
        return None

    dom_result = subprocess.run(
        [
            browser_bin,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--virtual-time-budget=5000",
            "--dump-dom",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if dom_result.returncode != 0:
        return None

    return {
        "screenshot_path": screenshot_path,
        "page": _page_snapshot_from_html(dom_result.stdout),
    }


def _validate_structure(project_dir: str, design_profile: dict, rendered_page: dict) -> dict:
    issues: list[str] = []
    files_checked = _iter_ui_source_files(project_dir)
    rendered_text = _normalize_text(rendered_page.get("body_text", ""))
    rendered_counts = rendered_page.get("tag_counts", {}) or {}

    if not rendered_text:
        issues.append("Rendered page text could not be captured for audit")
    else:
        for expected_text in design_profile.get("expected_texts", []):
            if expected_text not in rendered_text:
                issues.append(f"Rendered page missing text `{expected_text}`")

    expected_title = _normalize_text(str(design_profile.get("page_title", "")))
    actual_title = _normalize_text(str(rendered_page.get("title", "")))
    if expected_title and actual_title and expected_title not in actual_title:
        issues.append(f"Document title `{actual_title}` does not match reference `{expected_title}`")

    for tag_name, expected_count in (design_profile.get("expected_tag_counts", {}) or {}).items():
        actual_count = int(rendered_counts.get(tag_name, 0) or 0)
        if actual_count != expected_count:
            issues.append(
                f"Rendered page has {actual_count} `{tag_name}` element(s); expected {expected_count}"
            )

    if not design_profile.get("reference_allows_dark_mode"):
        for rel_path in files_checked:
            content = _read_file_safe(os.path.join(project_dir, rel_path))
            if "dark:" in content:
                issues.append(f"{rel_path}: contains unrequested `dark:` classes")

    return {
        "passed": not issues,
        "files_checked": files_checked,
        "issues": issues,
    }


def _capture_page_snapshot(project_dir: str, port: int = 17900) -> dict:
    """Serve dist/ and capture both a screenshot and a minimal DOM audit."""
    dist_dir = os.path.join(project_dir, "dist")
    if not os.path.isdir(dist_dir):
        print("  [screenshot] dist/ not found — skipping screenshot")
        return {
            "screenshot_path": None,
            "page": {"title": "", "body_text": "", "tag_counts": {tag: 0 for tag in _UI_TAGS_TO_AUDIT}},
        }

    screenshot_path = os.path.join(project_dir, "screenshot.png")
    server_proc = None
    try:
        server_proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            cwd=dist_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)  # give server time to start

        url = f"http://127.0.0.1:{port}/"

        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import]

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1600, "height": 1280})
                page.goto(url, timeout=15000)
                page.wait_for_timeout(2500)
                page_state = page.evaluate(
                    r"""() => {
                        const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
                        const counts = {};
                        for (const tag of ['header', 'nav', 'main', 'footer', 'form', 'label', 'button', 'a', 'input', 'h1', 'h2', 'h3']) {
                            counts[tag] = document.querySelectorAll(tag).length;
                        }
                        return {
                            title: normalize(document.title),
                            bodyText: normalize(document.body.innerText),
                            tagCounts: counts,
                        };
                    }"""
                )
                page.screenshot(path=screenshot_path, full_page=False)
                browser.close()

            print(f"  [screenshot] Saved to {screenshot_path} (python playwright)")
            return {
                "screenshot_path": screenshot_path,
                "page": {
                    "title": _normalize_text(str(page_state.get("title", ""))),
                    "body_text": _normalize_text(str(page_state.get("bodyText", ""))),
                    "tag_counts": page_state.get("tagCounts", {}),
                },
            }
        except ModuleNotFoundError:
            pass
        except Exception as exc:
            print(f"  [screenshot] Python Playwright failed: {exc}")

        node_snapshot = _capture_with_node_playwright(url, screenshot_path, project_dir)
        if node_snapshot:
            print(f"  [screenshot] Saved to {screenshot_path} (node playwright)")
            return node_snapshot

        browser_snapshot = _capture_with_browser_binary(url, screenshot_path)
        if browser_snapshot:
            print(f"  [screenshot] Saved to {screenshot_path} (browser binary)")
            return browser_snapshot

        print("  [screenshot] No supported screenshot backend available")
        return {
            "screenshot_path": None,
            "page": {"title": "", "body_text": "", "tag_counts": {tag: 0 for tag in _UI_TAGS_TO_AUDIT}},
        }

    except Exception as exc:
        print(f"  [screenshot] Failed: {exc}")
        return {
            "screenshot_path": None,
            "page": {"title": "", "body_text": "", "tag_counts": {tag: 0 for tag in _UI_TAGS_TO_AUDIT}},
        }
    finally:
        if server_proc is not None:
            server_proc.terminate()
            server_proc.wait(timeout=5)


def _compare_screenshots(impl_path: str, reference_path: str) -> dict:
    """Pixel-level similarity score between two screenshots (0–100)."""
    if not impl_path or not os.path.isfile(impl_path):
        return {"similarity": 0.0, "error": "implementation screenshot missing"}
    if not os.path.isfile(reference_path):
        return {"similarity": 0.0, "error": "reference screenshot missing"}

    try:
        from PIL import Image, ImageChops, ImageStat  # type: ignore[import]

        # Resize both to a consistent size for comparison
        size = (800, 640)
        img1 = Image.open(impl_path).convert("RGB").resize(size)
        img2 = Image.open(reference_path).convert("RGB").resize(size)

        diff = ImageChops.difference(img1, img2)
        stat = ImageStat.Stat(diff)
        total_diff = sum(stat.sum)
        max_diff = 255 * 3 * size[0] * size[1]
        similarity = round(100.0 * (1.0 - total_diff / max_diff), 1)

        # Also sample dominant background color to detect "unstyled white page"
        corner = img1.crop((0, 0, 100, 100))
        avg_color = tuple(int(v) for v in corner.resize((1, 1)).getpixel((0, 0)))

        return {
            "similarity": similarity,
            "avg_top_left_color": avg_color,
            "is_white_page": avg_color == (255, 255, 255),
        }
    except Exception as exc:
        return {"similarity": 0.0, "error": str(exc)}


def _build_task_prompt(
    design_profile: dict,
    design: dict[str, str],
    project_dir: str,
    prev_validation: dict | None = None,
) -> str:
    """Build the task prompt. prev_validation is the validation report from the previous attempt."""

    feedback_block = ""
    if prev_validation:
        css = prev_validation.get("css", {})
        structure = prev_validation.get("structure", {})
        screenshot = prev_validation.get("screenshot_comparison", {})
        similarity = screenshot.get("similarity", 0)
        css_bytes = css.get("css_size_bytes", 0)
        css_reason = css.get("reason", "unknown")
        is_white = screenshot.get("is_white_page", False)
        structure_issues = structure.get("issues", [])[:10]
        structure_block = "\n".join(f"- {issue}" for issue in structure_issues)

        feedback_block = f"""
## CRITICAL ISSUES FROM PREVIOUS ATTEMPT (you MUST fix all of these)

Previous attempt had these failures — do NOT repeat them:

    ### CSS / Bundle Validation
    - Compiled CSS size: {css_bytes} bytes
- Reason: {css_reason}
- Visual similarity with reference design: {similarity}%
{"- Screenshot shows an unstyled white page — no Tailwind styles were applied" if is_white else ""}

    ### Structural Mismatches
    {structure_block or '- None recorded'}

### Root Cause
The main bugs in the previous attempt:
1. **package.json was overwritten** by write_file — this deletes the `build` script.
   FIX: NEVER write package.json from scratch. Use `npm install -D <pkg>` to add deps.
   If "npm run build" says "Missing script: build", restore with `echo | npm create vite@latest . -- --template react`.
2. **Wrong Tailwind version** — must be tailwindcss@3, NOT tailwindcss (which is v4).
3. **Missing postcss.config.js** — run `npx tailwindcss init -p` to create it.
    4. **CSS bundle bloat** — broad `safelist`, `pattern: /.*/`, raw content padding, or fake filler CSS are NOT allowed.
    5. **Design fidelity gap** — the rendered output still differs from the supplied design source.

### MANDATORY FIXES
1. NEVER write package.json — the Vite scaffold is already there
2. Install Tailwind v3: `npm install -D tailwindcss@3 postcss autoprefixer`
3. Run `npx tailwindcss init -p` to generate BOTH tailwind.config.js AND postcss.config.js
4. Install React Vite plugin: `npm install -D @vitejs/plugin-react`
5. Create vite.config.js with the React plugin (see Step 1 below)
    6. After build, VERIFY: `wc -c dist/assets/*.css` — for this page it should be in the low tens of KB, not under 8000 bytes and not over 120000 bytes
    7. Compare each component against the reference HTML attribute by attribute until there are zero missing, redundant, or wrong items
    8. Regenerate the implementation screenshot inside the project directory and check it against the reference screenshot before reporting completion

"""

    salient_texts = "\n".join(
        f"- {text}" for text in design_profile.get("expected_texts", [])
    ) or "- Preserve the visible text content from the reference HTML exactly."
    theme_rule = (
        "The reference HTML already includes theme-specific classes. Keep only what is necessary to match the supplied design source."
        if design_profile.get("reference_allows_dark_mode")
        else "Do not add dark-mode classes or alternate theme variants that are absent from the supplied design source."
    )

    return f"""\
You are building a React + Tailwind CSS page from a Google Stitch design export.

## Project Directory
Work entirely inside this directory: {project_dir}
All bash commands must be run inside this directory.
{feedback_block}
## Design Bundle
- Design folder: {design_profile['design_dir']}
- Page title: {design_profile['page_title']}
- Base project name: {design_profile['base_project_name']}
- Reference screenshot: {design_profile['reference_screenshot']}
- Salient visible texts that MUST appear in the rendered page:
{salient_texts}

## Design Specification
{design["design_md"]}

## Reference HTML Implementation
The following HTML is the EXACT reference implementation from the design tool. \
Use it as the SINGLE source of truth for structure, class names, colors, and content. \
Every element in this HTML MUST be present in your React implementation:

```html
{design["code_html"]}
```

## Your Task
Build a pixel-faithful React + Tailwind v3 implementation of the above design.

### Step 1 — Scaffold the project (CRITICAL: use Tailwind v3, NOT v4)

**The Vite scaffold already exists** — `package.json` has the `build` script.
**DO NOT write package.json** — you will delete the build script!
To verify the scaffold: `cat {project_dir}/package.json | grep scripts`

MANDATORY: Install **Tailwind v3** (NOT v4 — v4 uses different syntax and will NOT work):
```bash
cd {project_dir}
npm install -D tailwindcss@3 postcss autoprefixer
npx tailwindcss init -p
```
The `npx tailwindcss init -p` command creates BOTH `tailwind.config.js` AND `postcss.config.js`.
Verify both files exist: `ls tailwind.config.js postcss.config.js`

MANDATORY: Install React Vite plugin:
```bash
npm install -D @vitejs/plugin-react
```

MANDATORY: Create vite.config.js:
```js
import {{ defineConfig }} from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig({{ plugins: [react()] }})
```

Install all dependencies:
```bash
npm install --no-fund --no-audit
```

### Step 2 — Configure Tailwind

**IMPORTANT**: Copy the EXACT color/spacing/font values from the reference HTML's `tailwind.config` \
block above. Do NOT guess or add values not in the reference.

Write tailwind.config.js (Tailwind v3 format — `module.exports = {{...}}`):
```js
module.exports = {{
  content: ["./index.html", "./src/**/*.{{js,ts,jsx,tsx}}"],
  theme: {{
    extend: {{
      colors: {{
        // EXACT colors from reference HTML
        primary: '#002045',
        secondary: '#13696a',
        'on-tertiary-container': '#f57d32',
        'on-tertiary': '#ffffff',
        background: '#f9f9ff',
        'on-background': '#111c2c',
        'on-surface': '#111c2c',
        'on-surface-variant': '#43474e',
        'outline-variant': '#c4c6cf',
        // ... all other colors from reference
      }},
      fontFamily: {{
        'h1': ['"Work Sans"', 'sans-serif'],
        'h2': ['"Work Sans"', 'sans-serif'],
        'h3': ['"Work Sans"', 'sans-serif'],
        'button': ['"Work Sans"', 'sans-serif'],
        'body-ui': ['"Work Sans"', 'sans-serif'],
        'label-caps': ['"Work Sans"', 'sans-serif'],
        'body-reading': ['"Newsreader"', 'serif'],
      }},
      fontSize: {{
        'h1': ['48px', {{ lineHeight: '1.2', letterSpacing: '-0.02em', fontWeight: '700' }}],
        'h2': ['32px', {{ lineHeight: '1.3', fontWeight: '600' }}],
        'h3': ['24px', {{ lineHeight: '1.4', fontWeight: '600' }}],
        'button': ['16px', {{ lineHeight: '1', fontWeight: '500' }}],
        'body-ui': ['16px', {{ lineHeight: '1.5', fontWeight: '400' }}],
        'label-caps': ['12px', {{ lineHeight: '1', letterSpacing: '0.05em', fontWeight: '600' }}],
        'body-reading': ['20px', {{ lineHeight: '1.7', fontWeight: '400' }}],
      }},
      spacing: {{
        'stack-sm': '8px',
        'stack-md': '24px',
        'stack-lg': '48px',
        'section-padding': '80px',
        'gutter': '24px',
        'margin-mobile': '16px',
        'unit': '8px',
        'container-max': '1120px',
      }},
      borderRadius: {{
        'DEFAULT': '0.125rem',
        'lg': '0.25rem',
        'xl': '0.5rem',
        'full': '0.75rem',
      }},
    }},
  }},
  plugins: [],
}}
```

Write src/index.css with Google Fonts @import AND Tailwind directives:
```css
@import url('https://fonts.googleapis.com/css2?family=Work+Sans:wght@400;500;600;700&family=Newsreader:ital,wght@0,400;1,400&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap');
@tailwind base;
@tailwind components;
@tailwind utilities;
.material-symbols-outlined {{
  font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
  display: inline-block;
  line-height: 1;
}}
```

### Step 3 — Implement Components

Translate the reference HTML EXACTLY into React components.
- Match every class token, visible text, semantic tag, and structural grouping.
- Match the reference HTML document title exactly in `index.html` or the final rendered document head.
- Derive component/file names from the ACTUAL page structure in the reference HTML. Do not force the old landing-page component template onto a different page.
- Preserve forms, labels, inputs, buttons, links, icons, and section order exactly as shown.
- {theme_rule}

### Step 4 — Build

```bash
cd {project_dir} && npm run build
```

**CRITICAL CSS VALIDATION** — run this after every build:
```bash
wc -c {project_dir}/dist/assets/*.css
```
- If the CSS file is LESS than 8000 bytes, Tailwind likely did NOT compile properly.
- If the CSS file is MORE than 120000 bytes for this single landing page, you likely added unused Tailwind output. Remove safelist/raw/filler bloat.
- If you see `@tailwind base` literally in the CSS output, PostCSS did NOT run.
- Common fix: verify `postcss.config.js` exists and has `tailwindcss` and `autoprefixer` plugins.
- NEVER use `safelist`, `pattern: /.*/`, large `raw:` content blocks, or dummy CSS rules/comments to inflate bundle size.

Check for errors and fix them. Re-run build after each fix.

### Step 5 — CSS Verification

After a successful build:
```bash
# Check CSS is compiled and minimal (should be > 8KB and < 120KB for this page)
wc -c {project_dir}/dist/assets/*.css

# Check for raw @tailwind directives (should show nothing)
grep "@tailwind" {project_dir}/dist/assets/*.css && echo "CSS NOT COMPILED" || echo "CSS OK"

# Sample the CSS to confirm utility classes are present
head -c 500 {project_dir}/dist/assets/*.css
```

If CSS is not compiled or is bloated, fix the Tailwind setup and rebuild.

### Step 6 — Design Comparison

After a successful build with compiled CSS:
- Compare the page ONE COMPONENT / SECTION AT A TIME using the actual structure from the reference HTML.
- For each component, compare: exact tag names, text content, href/button/icon/data attributes, class tokens, colors, spacing, typography, and child order.
- Treat any redundant or wrong attribute/class as a failure — not just missing items.
- List each design requirement: ✅ implemented / ❌ missing / ❌ redundant / ❌ wrong
- Fix all missing items, rebuild
- Repeat until there are ZERO missing, redundant, or wrong items

## Additional Test-Only Requirements
- Save the final implementation screenshot to `{project_dir}/screenshot.png`.
- Do not write screenshots or validation artifacts anywhere outside `{project_dir}`.
- Keep the generated CSS minimal: only include utilities actually used by this page.
- Do not add files outside the project directory.

### Step 7 — Write README.md

```markdown
# {design_profile['page_title']}

React + Tailwind CSS implementation of the Stitch reference page.

## Tech Stack
- React 18
- Vite
- Tailwind CSS v3

## Setup
npm install

## Development
npm run dev

## Build
npm run build
```

### Completion Criteria (ALL must be true)
- [ ] postcss.config.js exists (required for Tailwind v3 compilation)
- [ ] vite.config.js exists with @vitejs/plugin-react
- [ ] `npm run build` exits with code 0
- [ ] dist/ contains index.html and bundled JS/CSS
- [ ] dist/assets/*.css is between 8KB and 120KB for this page (compiled and not bloated)
- [ ] CSS contains NO literal `@tailwind` directives
- [ ] tailwind.config.js contains the required design tokens and NO broad safelist/raw content shortcuts
- [ ] All semantic sections, forms, buttons, links, headings, and other visible text from the reference HTML are present in the rendered page
- [ ] The final rendered document title matches the reference HTML `<title>` exactly
- [ ] Theme variants only exist when they are present in the supplied design source
- [ ] Component-by-component audit finds zero missing, redundant, or wrong attributes/classes
- [ ] tailwind.config.js has ALL design color tokens from reference HTML
- [ ] All fonts used by the supplied design are loaded correctly via CSS imports
- [ ] Final screenshot saved to `{project_dir}/screenshot.png`
- [ ] README.md written

When all criteria are met, output:
```
TASK COMPLETE
Files: [list of created/modified files]
Build: PASSED
CSS size: [actual bytes]
Design fidelity: [score]/100
Remaining gaps: [list or "None"]
```
"""



def _check_project_state(project_dir: str) -> dict:
    """Check what state the project is in after the agent run."""
    has_package_json = os.path.isfile(os.path.join(project_dir, "package.json"))
    has_dist = os.path.isdir(os.path.join(project_dir, "dist"))
    has_src = os.path.isdir(os.path.join(project_dir, "src"))
    has_tailwind = os.path.isfile(os.path.join(project_dir, "tailwind.config.js"))
    has_postcss = (
        os.path.isfile(os.path.join(project_dir, "postcss.config.js"))
        or os.path.isfile(os.path.join(project_dir, "postcss.config.cjs"))
        or os.path.isfile(os.path.join(project_dir, "postcss.config.mjs"))
    )
    has_vite_config = (
        os.path.isfile(os.path.join(project_dir, "vite.config.js"))
        or os.path.isfile(os.path.join(project_dir, "vite.config.ts"))
    )
    has_readme = os.path.isfile(os.path.join(project_dir, "README.md"))

    src_files: list[str] = []
    if has_src:
        for root, _, files in os.walk(os.path.join(project_dir, "src")):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), project_dir)
                src_files.append(rel)

    dist_files: list[str] = []
    if has_dist:
        for f in os.listdir(os.path.join(project_dir, "dist")):
            dist_files.append(f)

    return {
        "has_package_json": has_package_json,
        "has_dist": has_dist,
        "has_src": has_src,
        "has_tailwind": has_tailwind,
        "has_postcss": has_postcss,
        "has_vite_config": has_vite_config,
        "has_readme": has_readme,
        "src_files": src_files,
        "dist_files": dist_files,
        "complete": has_package_json and has_dist and has_src and has_tailwind and has_readme,
    }


def _print_separator(title: str = "") -> None:
    if title:
        padding = (70 - len(title) - 2) // 2
        print(f"\n{'=' * padding} {title} {'=' * padding}\n")
    else:
        print(f"\n{'=' * 70}\n")


def _validate_full(project_dir: str, attempt: int, design_profile: dict) -> dict:
    """Run all independent validations after an agent attempt.

    This is the Copilot-side truth check — does NOT trust agent self-report.
    """
    _print_separator(f"COPILOT VALIDATION — ATTEMPT {attempt}")

    state = _check_project_state(project_dir)
    print(f"package.json:    {'✅' if state['has_package_json'] else '❌'}")
    print(f"postcss.config:  {'✅' if state['has_postcss'] else '❌ MISSING (Tailwind v3 requires this!)'}")
    print(f"vite.config.js:  {'✅' if state['has_vite_config'] else '❌ MISSING (@vitejs/plugin-react required!)'}")
    print(f"tailwind.config: {'✅' if state['has_tailwind'] else '❌'}")
    print(f"dist/:           {'✅' if state['has_dist'] else '❌'}")
    print(f"README.md:       {'✅' if state['has_readme'] else '❌'}")

    # CSS compilation check
    css = _validate_css_compilation(project_dir, design_profile)
    css_icon = "✅" if css["valid"] else ("⚠️" if css["compiled"] else "❌")
    print(f"\nCSS compilation: {css_icon}")
    print(f"  CSS file: {css.get('css_file', 'N/A')}")
    print(f"  CSS size: {css.get('css_size_bytes', 0):,} bytes")
    print(f"  Reason:   {css.get('reason', 'N/A')}")

    # Screenshot
    print("\nCapturing screenshot of built page...")
    reference_copy_path = _save_reference_screenshot(project_dir, design_profile["reference_screenshot"])
    page_snapshot = _capture_page_snapshot(project_dir)
    screenshot_path = page_snapshot.get("screenshot_path")
    structure = _validate_structure(project_dir, design_profile, page_snapshot.get("page", {}))
    structure_icon = "✅" if structure["passed"] else "❌"
    print(f"\nRendered audit: {structure_icon}")
    if structure["passed"]:
        print("  Rendered DOM and salient text matched the dynamic reference audit.")
    else:
        for issue in structure["issues"][:12]:
            print(f"  - {issue}")

    # Visual comparison
    comparison: dict = {}
    if screenshot_path:
        comparison = _compare_screenshots(screenshot_path, design_profile["reference_screenshot"])
        sim = comparison.get("similarity", 0)
        sim_icon = "✅" if sim >= 70 else ("⚠️" if sim >= 40 else "❌")
        print(f"\nVisual similarity: {sim_icon} {sim}% (vs reference design screenshot)")
        if comparison.get("is_white_page"):
            print("  ⚠️  Screenshot appears to be a plain white page — no CSS styles applied")
        if comparison.get("error"):
            print(f"  Comparison error: {comparison['error']}")
    else:
        print("\nVisual comparison: ❌ (screenshot could not be captured)")

    # Quality score: weighted combination of CSS + visual + structure + config files
    css_score = 100 if css["valid"] else (60 if css["compiled"] else 0)
    visual_score = comparison.get("similarity", 0)
    structure_score = max(0, 100 - 12 * len(structure["issues"]))
    has_postcss_score = 100 if state["has_postcss"] else 0
    has_dist_score = 100 if state["has_dist"] else 0
    quality_score = round(
        0.28 * css_score
        + 0.34 * visual_score
        + 0.20 * structure_score
        + 0.10 * has_postcss_score
        + 0.08 * has_dist_score
    )

    print(f"\nOverall quality score: {quality_score}/100")
    print(f"  CSS validity:      {css_score}/100 (weight 28%)")
    print(f"  Visual similarity: {visual_score}/100 (weight 34%)")
    print(f"  Structure audit:   {structure_score}/100 (weight 20%)")
    print(f"  PostCSS config:    {has_postcss_score}/100 (weight 10%)")
    print(f"  Build output:      {has_dist_score}/100 (weight 8%)")

    validation = {
        "attempt": attempt,
        "state": state,
        "css": css,
        "structure": structure,
        "reference_screenshot_path": reference_copy_path,
        "screenshot_path": screenshot_path,
        "rendered_page": page_snapshot.get("page", {}),
        "screenshot_comparison": comparison,
        "quality_score": quality_score,
    }

    # Save validation report to project folder
    report_path = os.path.join(project_dir, "validation-report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(validation, fh, indent=2, ensure_ascii=False, default=str)
    print(f"\nValidation report saved: {report_path}")

    return validation


def run_test(
    attempt: int,
    max_turns: int,
    timeout: int,
    design_profile: dict,
    prev_validation: dict | None = None,
) -> tuple[bool, dict]:
    """Run one attempt of the stitch UI test.

    Returns (is_complete, validation_report).
    prev_validation is the full validation dict from the previous iteration.
    """
    project_name = f"{design_profile['base_project_name']}_{attempt}"
    project_dir = os.path.join(_TESTS_DATA_DIR, project_name)
    os.makedirs(project_dir, exist_ok=True)

    _print_separator(f"ATTEMPT {attempt}")
    print(f"Project directory: {project_dir}")
    print(f"Model: {os.environ['AGENT_MODEL']}")
    print(f"Max turns: {max_turns}, Timeout: {timeout}s")

    design = _load_design_content(design_profile)
    print(f"Design spec: {len(design['design_md'])} chars")
    print(f"Reference HTML: {len(design['code_html'])} chars")
    if prev_validation:
        print(f"Previous quality score: {prev_validation['quality_score']}/100 — improving based on failures")

    # Pre-scaffold: create the Vite+React project so the agent doesn't corrupt package.json
    pkg_json = os.path.join(project_dir, "package.json")
    if not os.path.isfile(pkg_json):
        print("Pre-scaffolding Vite+React project...")
        import subprocess
        result_scaffold = subprocess.run(
            "echo | npm create vite@latest . -- --template react",
            shell=True, cwd=project_dir,
            capture_output=True, text=True, timeout=60,
        )
        if result_scaffold.returncode != 0:
            print(f"  WARNING: scaffold failed: {result_scaffold.stderr[:300]}")
        else:
            print("  Vite scaffold created.")
    else:
        # Ensure package.json has a build script (protect against agent overwriting it)
        with open(pkg_json, encoding="utf-8") as fh:
            import json as _json
            pkg = _json.load(fh)
        if "scripts" not in pkg or "build" not in pkg.get("scripts", {}):
            print("  WARNING: package.json missing build script — re-scaffolding...")
            import subprocess
            subprocess.run(
                "echo | npm create vite@latest . -- --template react",
                shell=True, cwd=project_dir, capture_output=True, timeout=60,
            )

    # Clear tool registry to avoid duplicates from previous runs
    from common.tools.registry import clear_registry
    clear_registry()

    # Configure connect-agent to use the project dir as sandbox
    os.environ["CONNECT_AGENT_SANDBOX_ROOT"] = project_dir
    os.environ["CONNECT_AGENT_MAX_TURNS"] = str(max_turns)
    os.environ["CONNECT_AGENT_TIMEOUT"] = str(timeout)
    os.environ["CONNECT_AGENT_PROFILE"] = "design-to-code"

    from common.runtime.adapter import get_runtime
    from common.runtime.connect_agent.adapter import DESIGN_TO_CODE_AGENTIC_SYSTEM

    runtime = get_runtime("connect-agent")

    # Build task prompt — includes feedback from previous attempt's validation
    task_prompt = _build_task_prompt(design_profile, design, project_dir, prev_validation=prev_validation)

    _print_separator("STARTING AGENTIC EXECUTION")
    start = time.time()
    turn_count = [0]

    def on_progress(msg: str) -> None:
        turn_count[0] += 1
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:4d}s | turn {turn_count[0]:2d}] {msg[:120]}")

    result = runtime.run_agentic(
        task=task_prompt,
        system_prompt=DESIGN_TO_CODE_AGENTIC_SYSTEM,
        cwd=project_dir,
        max_turns=max_turns,
        timeout=timeout,
        on_progress=on_progress,
    )

    elapsed = time.time() - start
    _print_separator("AGENT RESULT (self-report — not trusted)")
    print(f"Agent reported success: {result.success}")
    print(f"Turns used: {result.turns_used}")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Backend: {result.backend_used}")
    print()
    print(f"Agent summary (self-report only):\n{result.summary[:800]}")

    # Tool call summary
    _print_separator("TOOL CALLS")
    print(f"Total tool calls: {len(result.tool_calls)}")
    tool_counts: dict[str, int] = {}
    for tc in result.tool_calls:
        name = tc.get("name", "?")
        tool_counts[name] = tool_counts.get(name, 0) + 1
    for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        print(f"  {name}: {count}")

    # -------------------------------------------------------------------
    # INDEPENDENT VALIDATION — Copilot validates, ignoring agent self-report
    # -------------------------------------------------------------------
    validation = _validate_full(project_dir, attempt, design_profile)

    # Save combined test report
    report = {
        "attempt": attempt,
        "project_dir": project_dir,
        "agent_reported_success": result.success,
        "turns_used": result.turns_used,
        "elapsed_seconds": round(elapsed, 1),
        "tool_calls": tool_counts,
        "agent_summary": result.summary[:2000],
        "quality_score": validation["quality_score"],
        "css_compiled": validation["css"]["compiled"],
        "css_valid": validation["css"]["valid"],
        "css_size_bytes": validation["css"].get("css_size_bytes", 0),
        "visual_similarity": validation["screenshot_comparison"].get("similarity", 0),
        "structure_passed": validation["structure"]["passed"],
    }
    report_path = os.path.join(project_dir, "test-report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False, default=str)
    print(f"\nTest report: {report_path}")

    quality = validation["quality_score"]
    is_complete = (
        validation["state"]["complete"]
        and validation["css"]["valid"]
        and validation["structure"]["passed"]
        and validation["screenshot_comparison"].get("similarity", 0) >= 90
        and quality >= 90
    )

    _print_separator()
    if is_complete:
        print(f"✅ ATTEMPT {attempt} PASSED VALIDATION — quality score {quality}/100")
        print(f"   Project:    {project_dir}")
        print(f"   Screenshot: {project_dir}/screenshot.png")
        print(f"   Run:        cd {project_dir} && npm run dev")
    else:
        print(f"⚠️  ATTEMPT {attempt} FAILED VALIDATION — quality score {quality}/100")
        css = validation["css"]
        if not css["valid"]:
            print(f"   ❌ CSS INVALID: {css.get('reason')}")
            print("      → Next attempt: compile Tailwind correctly and remove unused/bloated output")
        sim = validation["screenshot_comparison"].get("similarity", 0)
        if sim < 90:
            print(f"   ❌ VISUAL MISMATCH: {sim}% similarity (needs ≥90%)")
        if not validation["structure"]["passed"]:
            print("   ❌ STRUCTURAL MISMATCHES:")
            for issue in validation["structure"]["issues"][:8]:
                print(f"      - {issue}")
        if not validation["state"]["has_postcss"]:
            print("   ❌ postcss.config.js missing — Tailwind PostCSS plugin never ran")
        if not validation["state"]["has_vite_config"]:
            print("   ❌ vite.config.js missing — React plugin not loaded, JSX may fail")

    return is_complete, validation


def main() -> None:
    parser = argparse.ArgumentParser(description="Stitch UI test — generic React + Tailwind page from a Stitch design folder")
    parser.add_argument(
        "--design-dir",
        default="reference/stitch_open_english_study_hub",
        help="Design folder path relative to repo root or absolute path",
    )
    parser.add_argument(
        "--project-name",
        default="",
        help="Optional override for the tests/data project folder prefix",
    )
    parser.add_argument(
        "--attempt", type=int, default=1,
        help="Starting attempt number (default: 1)",
    )
    parser.add_argument(
        "--attempts", type=int, default=5,
        help="Number of iterations to run (default: 5)",
    )
    parser.add_argument("--max-turns", type=int, default=80, help="Max agentic turns per iteration")
    parser.add_argument("--timeout", type=int, default=3600, help="Max total timeout in seconds per iteration")
    args = parser.parse_args()

    design_dir = args.design_dir
    if not os.path.isabs(design_dir):
        design_dir = os.path.join(_REPO_ROOT, design_dir)
    design_profile = _build_design_profile(os.path.abspath(design_dir))
    if args.project_name:
        design_profile["base_project_name"] = _slugify(args.project_name)[:64]

    print("=" * 70)
    print(f"Stitch UI Test — {design_profile['page_title']}")
    print(f"Model:          {os.environ['AGENT_MODEL']}")
    print(f"Design source:  {design_profile['design_dir']}")
    print(f"Reference:      {design_profile['reference_screenshot']}")
    print(f"Project prefix: {design_profile['base_project_name']}")
    print(f"Iterations:     {args.attempts} (starting from attempt {args.attempt})")
    print(f"Max turns/iter: {args.max_turns}, Timeout: {args.timeout}s")
    print("=" * 70)
    print()

    prev_validation: dict | None = None
    best_quality = 0
    best_attempt = -1
    all_results: list[dict] = []

    for i in range(args.attempts):
        attempt_num = args.attempt + i
        try:
            success, validation = run_test(
                attempt=attempt_num,
                max_turns=args.max_turns,
                timeout=args.timeout,
                design_profile=design_profile,
                prev_validation=prev_validation,
            )
            quality = validation["quality_score"]
            all_results.append({"attempt": attempt_num, "quality": quality, "success": success})

            if quality > best_quality:
                best_quality = quality
                best_attempt = attempt_num

            prev_validation = validation

            if success and i + 1 >= args.attempts:
                break
            elif success:
                print(f"\nAttempt {attempt_num} passed. Continuing for {args.attempts - i - 1} more iteration(s)...")

        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break
        except Exception as exc:
            print(f"\nError in attempt {attempt_num}: {exc}")
            import traceback
            traceback.print_exc()
            prev_validation = None

    # Final summary
    print(f"\n{'=' * 70}")
    print("FINAL SUMMARY")
    print(f"{'=' * 70}")
    for r in all_results:
        status = "✅ PASS" if r["success"] else "❌ FAIL"
        print(f"  Attempt {r['attempt']}: {status}  quality={r['quality']}/100")
    print()
    print(f"Best quality score: {best_quality}/100  (attempt {best_attempt})")

    if best_attempt >= 0:
        project_dir = os.path.join(_TESTS_DATA_DIR, f"{design_profile['base_project_name']}_{best_attempt}")
        print(f"Best project:  {project_dir}")
        screenshot = os.path.join(project_dir, "screenshot.png")
        if os.path.isfile(screenshot):
            print(f"Screenshot:    {screenshot}")
            print(f"Reference:     {design_profile['reference_screenshot']}")
            print(f"  Compare: open {screenshot} {design_profile['reference_screenshot']}")
    print()

    if best_quality >= 75:
        print("✅ TEST SUITE PASSED")
        sys.exit(0)
    else:
        print(f"⚠️  TEST SUITE: Best quality {best_quality}/100 did not reach threshold (75/100)")
        sys.exit(1)


if __name__ == "__main__":
    main()
