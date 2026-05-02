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

_DESIGN_DIR = os.path.join(_REPO_ROOT, "reference", "stitch_open_english_study_hub")
_TESTS_DATA_DIR = os.path.join(_REPO_ROOT, "tests", "data")
_BASE_PROJECT_NAME = "open-english-study-hub"
_REFERENCE_SCREENSHOT = os.path.join(_DESIGN_DIR, "screen.png")

# Compiled Tailwind output for this small landing page should be present but not bloated.
_MIN_CSS_SIZE_BYTES = 8_000
_MAX_CSS_SIZE_BYTES = 120_000

_STRUCTURE_RULES = {
    "src/App.jsx": {
        "required": [
            "bg-background",
            "text-on-background",
            "font-body-ui",
            "min-h-screen",
            "flex flex-col",
            "<NavBar />",
            "<HeroSection />",
            "<Footer />",
        ],
        "forbidden": ["dark:"],
    },
    "src/components/NavBar.jsx": {
        "required": [
            "<header",
            "bg-white",
            "border-b",
            "border-gray-100",
            "max-w-[1120px]",
            "text-xl",
            "font-bold",
            "tracking-tighter",
            "text-blue-900",
            "font-['Work_Sans']",
            "hidden md:flex items-center space-x-gutter",
            "Lessons",
            "Flashcards",
            "Progress",
            "Library",
            "Sign In",
        ],
        "forbidden": ["dark:"],
    },
    "src/components/HeroSection.jsx": {
        "required": [
            "flex-grow flex flex-col items-center justify-center",
            "px-margin-mobile",
            "py-section-padding",
            "font-h1",
            "text-h1",
            "text-primary",
            "Master Academic English with Scholarly Precision.",
            "bg-on-tertiary-container",
            "text-on-tertiary",
            "Start Learning Now",
            "Advanced Grammar",
            "Research Writing",
            "Formal Vocabulary",
            "arrow_forward",
            "bg-outline-variant",
        ],
        "forbidden": ["dark:"],
    },
    "src/components/Footer.jsx": {
        "required": [
            "<footer",
            "bg-slate-50",
            "border-t",
            "border-gray-200",
            "max-w-[1120px]",
            "© 2024 Linguist Library. Premium Academic English Study.",
            "Terms of Service",
            "Privacy Policy",
            "Contact Support",
            "font-semibold",
            "text-slate-900",
        ],
        "forbidden": ["dark:"],
    },
}

_TAILWIND_CONFIG_REQUIRED_SNIPPETS = [
    "#002045",
    "#13696a",
    "#f57d32",
    "#f9f9ff",
    "Work Sans",
    "Newsreader",
    "section-padding",
    "container-max",
]

_TAILWIND_CONFIG_FORBIDDEN_PATTERNS = [
    r"safelist\\s*:",
    r"pattern\\s*:\\s*/\\.\\*/",
    r"raw\\s*:",
]


def _read_file_safe(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _load_design_content() -> dict[str, str]:
    return {
        "design_md": _read_file_safe(os.path.join(_DESIGN_DIR, "DESIGN.md")),
        "code_html": _read_file_safe(os.path.join(_DESIGN_DIR, "code.html")),
    }


def _save_reference_screenshot(project_dir: str) -> str | None:
    if not os.path.isfile(_REFERENCE_SCREENSHOT):
        return None
    reference_copy_path = os.path.join(project_dir, "reference-screenshot.png")
    shutil.copy2(_REFERENCE_SCREENSHOT, reference_copy_path)
    return reference_copy_path


# ---------------------------------------------------------------------------
# Independent validation — do NOT trust agent self-report
# ---------------------------------------------------------------------------

def _validate_css_compilation(project_dir: str) -> dict:
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
    has_required_tokens = all(snippet in tailwind_config for snippet in _TAILWIND_CONFIG_REQUIRED_SNIPPETS)
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
    if not has_required_tokens:
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
        "forbidden_config_matches": forbidden_config_matches,
        "css_file": css_files[0],
    }


def _validate_structure(project_dir: str) -> dict:
    issues: list[str] = []
    files_checked: list[str] = []

    for rel_path, rule in _STRUCTURE_RULES.items():
        file_path = os.path.join(project_dir, rel_path)
        content = _read_file_safe(file_path)
        files_checked.append(rel_path)
        if not content:
            issues.append(f"{rel_path}: file missing or empty")
            continue
        missing = [snippet for snippet in rule["required"] if snippet not in content]
        forbidden = [snippet for snippet in rule["forbidden"] if snippet in content]
        for snippet in missing:
            issues.append(f"{rel_path}: missing `{snippet}`")
        for snippet in forbidden:
            issues.append(f"{rel_path}: contains forbidden `{snippet}`")

    return {
        "passed": not issues,
        "files_checked": files_checked,
        "issues": issues,
    }


def _capture_screenshot(project_dir: str, port: int = 17900) -> str | None:
    """Serve dist/ with http.server and capture a screenshot via Playwright.

    Returns the path to the screenshot PNG, or None on failure.
    """
    dist_dir = os.path.join(project_dir, "dist")
    if not os.path.isdir(dist_dir):
        print("  [screenshot] dist/ not found — skipping screenshot")
        return None

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

        from playwright.sync_api import sync_playwright  # type: ignore[import]

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 1280})
            page.goto(f"http://127.0.0.1:{port}/", timeout=15000)
            # Wait for fonts / layout to settle
            page.wait_for_timeout(2500)
            page.screenshot(path=screenshot_path, full_page=False)
            browser.close()

        print(f"  [screenshot] Saved to {screenshot_path}")
        return screenshot_path

    except Exception as exc:
        print(f"  [screenshot] Failed: {exc}")
        return None
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
        from PIL import Image, ImageChops  # type: ignore[import]

        # Resize both to a consistent size for comparison
        size = (800, 640)
        img1 = Image.open(impl_path).convert("RGB").resize(size)
        img2 = Image.open(reference_path).convert("RGB").resize(size)

        diff = ImageChops.difference(img1, img2)
        pixels = list(diff.getdata())
        total_diff = sum(r + g + b for r, g, b in pixels)
        max_diff = 255 * 3 * len(pixels)
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


def _build_task_prompt(design: dict[str, str], project_dir: str, prev_validation: dict | None = None) -> str:
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
    5. **Light-theme fidelity** — for this test, `dark:` classes are redundant/wrong because the reference screenshot is the light theme only.

### MANDATORY FIXES
1. NEVER write package.json — the Vite scaffold is already there
2. Install Tailwind v3: `npm install -D tailwindcss@3 postcss autoprefixer`
3. Run `npx tailwindcss init -p` to generate BOTH tailwind.config.js AND postcss.config.js
4. Install React Vite plugin: `npm install -D @vitejs/plugin-react`
5. Create vite.config.js with the React plugin (see Step 1 below)
    6. After build, VERIFY: `wc -c dist/assets/*.css` — for this page it should be in the low tens of KB, not under 8000 bytes and not over 120000 bytes
    7. Remove every `dark:` utility unless the task explicitly asks for a dark theme (this test does not)
    8. Compare each component against the reference HTML attribute by attribute until there are zero missing, redundant, or wrong items

"""

    return f"""\
You are building a React + Tailwind CSS landing page for "Linguist Library" — \
a premium academic English study platform.

## Project Directory
Work entirely inside this directory: {project_dir}
All bash commands must be run inside this directory.
{feedback_block}
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

**IMPORTANT**: Copy the EXACT color/spacing/font values from the reference HTML's tailwind.config \
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

Translate the reference HTML EXACTLY into React components. Match every class name, \
every text, every structural element.

**src/components/NavBar.jsx** — translate the `<header>` block:
- White background, border-bottom, sticky top
- Max-width 1120px centered container
- Left: "Linguist Library" — `text-xl font-bold tracking-tighter text-blue-900 font-['Work_Sans']`
- Center: nav links (Lessons, Flashcards, Progress, Library) hidden on mobile, flex on md
- Right: "Sign In" text button
- Match exact classes from reference HTML

**src/components/HeroSection.jsx** — translate the `<main>` block:
- `flex-grow flex flex-col items-center justify-center px-margin-mobile py-section-padding`
- h1: exact text "Master Academic English with Scholarly Precision."
- CTA button: `bg-on-tertiary-container text-on-tertiary` (orange #f57d32)
- Three category links with Material Symbol `arrow_forward` icons
- Vertical dividers between links on desktop

**src/components/Footer.jsx** — translate the `<footer>` block:
- `bg-slate-50 border-t border-gray-200`
- Left: copyright text
- Right: Terms of Service, Privacy Policy, Contact Support (bold) links

**src/App.jsx** — assemble components:
```jsx
import React from 'react'
import NavBar from './components/NavBar'
import HeroSection from './components/HeroSection'
import Footer from './components/Footer'

export default function App() {{
  return (
    <div className="bg-background text-on-background font-body-ui min-h-screen flex flex-col">
      <NavBar />
      <HeroSection />
      <Footer />
    </div>
  )
}}
```

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
- Compare NavBar, HeroSection, and Footer to the reference HTML ONE COMPONENT AT A TIME.
- For each component, compare: exact tag names, text content, href/button/icon attributes, class tokens, colors, spacing, typography, and child order.
- Treat any redundant or wrong attribute/class as a failure — not just missing items.
- Because this test targets the light-theme screenshot, remove all `dark:` classes and any dark-only styling.
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
# Linguist Library

React + Tailwind CSS implementation of the Linguist Library landing page.

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
- [ ] NavBar with logo, nav links (Lessons/Flashcards/Progress/Library), Sign In button
- [ ] HeroSection with h1, orange CTA button (#f57d32), three category links
- [ ] Footer with light grey background, copyright and Terms/Privacy/Contact links
- [ ] No `dark:` classes or dark-only styling remain in the implementation
- [ ] Component-by-component audit finds zero missing, redundant, or wrong attributes/classes
- [ ] tailwind.config.js has ALL design color tokens from reference HTML
- [ ] Work Sans + Newsreader fonts loaded via Google Fonts @import
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


def _validate_full(project_dir: str, attempt: int) -> dict:
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
    css = _validate_css_compilation(project_dir)
    css_icon = "✅" if css["valid"] else ("⚠️" if css["compiled"] else "❌")
    print(f"\nCSS compilation: {css_icon}")
    print(f"  CSS file: {css.get('css_file', 'N/A')}")
    print(f"  CSS size: {css.get('css_size_bytes', 0):,} bytes")
    print(f"  Reason:   {css.get('reason', 'N/A')}")

    structure = _validate_structure(project_dir)
    structure_icon = "✅" if structure["passed"] else "❌"
    print(f"\nStructure audit: {structure_icon}")
    if structure["passed"]:
        print("  All required component attributes matched the test rules.")
    else:
        for issue in structure["issues"][:12]:
            print(f"  - {issue}")

    # Screenshot
    print("\nCapturing screenshot of built page...")
    reference_copy_path = _save_reference_screenshot(project_dir)
    screenshot_path = _capture_screenshot(project_dir)

    # Visual comparison
    comparison: dict = {}
    if screenshot_path:
        comparison = _compare_screenshots(screenshot_path, _REFERENCE_SCREENSHOT)
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
    prev_validation: dict | None = None,
) -> tuple[bool, dict]:
    """Run one attempt of the stitch UI test.

    Returns (is_complete, validation_report).
    prev_validation is the full validation dict from the previous iteration.
    """
    project_name = f"{_BASE_PROJECT_NAME}_{attempt}"
    project_dir = os.path.join(_TESTS_DATA_DIR, project_name)
    os.makedirs(project_dir, exist_ok=True)

    _print_separator(f"ATTEMPT {attempt}")
    print(f"Project directory: {project_dir}")
    print(f"Model: {os.environ['AGENT_MODEL']}")
    print(f"Max turns: {max_turns}, Timeout: {timeout}s")

    design = _load_design_content()
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
    task_prompt = _build_task_prompt(design, project_dir, prev_validation=prev_validation)

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
    validation = _validate_full(project_dir, attempt)

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
    parser = argparse.ArgumentParser(description="Stitch UI test — Linguist Library landing page")
    parser.add_argument(
        "--attempt", type=int, default=2,
        help="Starting attempt number (default: 2, since attempt 1 already exists)",
    )
    parser.add_argument(
        "--attempts", type=int, default=5,
        help="Number of iterations to run (default: 5)",
    )
    parser.add_argument("--max-turns", type=int, default=80, help="Max agentic turns per iteration")
    parser.add_argument("--timeout", type=int, default=3600, help="Max total timeout in seconds per iteration")
    args = parser.parse_args()

    print("=" * 70)
    print("Stitch UI Test — Linguist Library landing page")
    print(f"Model:          {os.environ['AGENT_MODEL']}")
    print(f"Design source:  {_DESIGN_DIR}")
    print(f"Reference:      {_REFERENCE_SCREENSHOT}")
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
        project_dir = os.path.join(_TESTS_DATA_DIR, f"{_BASE_PROJECT_NAME}_{best_attempt}")
        print(f"Best project:  {project_dir}")
        screenshot = os.path.join(project_dir, "screenshot.png")
        if os.path.isfile(screenshot):
            print(f"Screenshot:    {screenshot}")
            print(f"Reference:     {_REFERENCE_SCREENSHOT}")
            print(f"  Compare: open {screenshot} {_REFERENCE_SCREENSHOT}")
    print()

    if best_quality >= 75:
        print("✅ TEST SUITE PASSED")
        sys.exit(0)
    else:
        print(f"⚠️  TEST SUITE: Best quality {best_quality}/100 did not reach threshold (75/100)")
        sys.exit(1)


if __name__ == "__main__":
    main()
