#!/usr/bin/env python3
"""Extract detailed design specifications from Figma file.

This script extracts comprehensive design information including:
- UI element specifications (dimensions, colors, typography)
- Layout specifications (spacing, padding, alignment)
- Component hierarchy
- Design tokens

Usage:
    python3 scripts/extract_figma_design.py
    python3 scripts/extract_figma_design.py --file-key <key> --node-id <id>
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ui-design"))

from figma_client_enhanced import FigmaClientEnhanced, FigmaRateLimitError


def load_env_file(path: Path) -> dict:
    """Load environment variables from .env file."""
    env = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def parse_figma_url(url: str) -> tuple:
    """Parse Figma URL to extract file key and node ID.
    
    Args:
        url: Figma file URL
        
    Returns:
        Tuple of (file_key, node_id)
    """
    file_key = ""
    node_id = ""
    
    # Extract file key
    for prefix in ("/design/", "/file/"):
        if prefix in url:
            after = url.split(prefix)[1]
            file_key = after.split("/")[0].split("?")[0]
            break
    
    # Extract node ID
    if "node-id=" in url:
        raw = url.split("node-id=")[1].split("&")[0]
        node_id = raw.replace("-", ":").replace("%3A", ":").replace("%3a", ":")
    
    return file_key, node_id


def format_ui_spec(spec: dict) -> str:
    """Format UI specification for readable output."""
    lines = []
    lines.append(f"  {spec.get('name', 'Unnamed')} ({spec.get('type', 'Unknown')})")
    
    # Dimensions
    dims = spec.get("dimensions", {})
    if dims.get("width") and dims.get("height"):
        lines.append(f"    Size: {dims['width']:.1f} x {dims['height']:.1f}")
    
    # Position
    pos = spec.get("position", {})
    if pos.get("x") is not None and pos.get("y") is not None:
        lines.append(f"    Position: ({pos['x']:.1f}, {pos['y']:.1f})")
    
    # Colors
    colors = spec.get("colors", {})
    if colors.get("fills"):
        for fill in colors["fills"]:
            lines.append(f"    Fill: {fill.get('hex', 'N/A')}")
    if colors.get("strokes"):
        for stroke in colors["strokes"]:
            lines.append(f"    Stroke: {stroke.get('hex', 'N/A')} ({stroke.get('weight', 1)}px)")
    
    # Typography
    typo = spec.get("typography", {})
    if typo.get("fontFamily"):
        font_info = f"{typo['fontFamily']}"
        if typo.get("fontSize"):
            font_info += f" {typo['fontSize']}px"
        if typo.get("fontWeight"):
            font_info += f" {typo['fontWeight']}"
        lines.append(f"    Font: {font_info}")
    
    # Layout
    layout = spec.get("layout", {})
    if layout.get("mode"):
        lines.append(f"    Layout: {layout['mode']}")
        if layout.get("itemSpacing"):
            lines.append(f"    Spacing: {layout['itemSpacing']}px")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-key", help="Figma file key")
    parser.add_argument("--node-id", help="Specific node ID to extract")
    parser.add_argument("--url", help="Figma file URL (alternative to file-key)")
    parser.add_argument("--output", default="docs/figma_design_specs.json",
                       help="Output file path")
    parser.add_argument("--max-depth", type=int, default=5,
                       help="Maximum depth for node traversal")
    parser.add_argument("--use-cache", action="store_true", default=True,
                       help="Use cached data if available (default: True)")
    parser.add_argument("--no-cache", action="store_true",
                       help="Force fresh API call, ignore cache")
    parser.add_argument("--cache-ttl", type=int, default=86400,
                       help="Cache TTL in seconds (default: 86400 = 24 hours)")
    args = parser.parse_args()
    
    # Load environment
    env = load_env_file(PROJECT_ROOT / "tests" / ".env")
    token = env.get("TEST_FIGMA_TOKEN") or os.environ.get("FIGMA_TOKEN")
    
    if not token:
        print("❌ Error: FIGMA_TOKEN not found")
        print("   Set TEST_FIGMA_TOKEN in tests/.env or FIGMA_TOKEN environment variable")
        return 1
    
    # Determine file key and node ID
    file_key = ""
    node_id = ""
    
    if args.url:
        file_key, node_id = parse_figma_url(args.url)
        if not file_key:
            print(f"❌ Error: Could not parse file key from URL: {args.url}")
            return 1
    else:
        file_key = args.file_key or ""
        if not file_key:
            # Try to parse from URL in env
            figma_url = env.get("TEST_FIGMA_FILE_URL", "")
            if figma_url:
                file_key, node_id = parse_figma_url(figma_url)
        if args.node_id:
            node_id = args.node_id
    
    if not file_key:
        print("❌ Error: No file key provided")
        print("   Use --file-key, --url, or set TEST_FIGMA_FILE_URL in tests/.env")
        return 1
    
    print(f"📄 Figma File: {file_key}")
    if node_id:
        print(f"🎯 Node ID: {node_id}")
    print()
    
    # Initialize client
    client = FigmaClientEnhanced(token)
    
    try:
        # Fetch file data
        print("⏳ Fetching file data...")
        file_data, status = client.get_file_detailed(file_key)
        
        if status == "rate_limited":
            print("⚠️  Rate limit exceeded. Please wait 60 seconds and try again.")
            return 1
        elif status != "ok":
            print(f"❌ Error fetching file: {status}")
            print(f"   {file_data}")
            return 1
        
        print(f"✅ File: {file_data.get('name')}")
        print(f"   Last modified: {file_data.get('lastModified')}")
        print(f"   Version: {file_data.get('version')}")
        print()
        
        # Extract design tokens
        print("⏳ Extracting design tokens...")
        tokens = client.get_design_tokens(file_data)
        print(f"✅ Design tokens:")
        print(f"   Colors: {len(tokens['colors'])}")
        print(f"   Typography: {len(tokens['typography'])}")
        print(f"   Effects: {len(tokens['effects'])}")
        print()
        
        # Extract UI specifications
        print(f"⏳ Extracting UI specifications (max depth: {args.max_depth})...")
        document = file_data.get("document", {})
        
        if not document:
            print("❌ Error: No document found in file data")
            return 1
        
        all_specs = client.traverse_and_extract(document, max_depth=args.max_depth)
        print(f"✅ Extracted {len(all_specs)} UI elements")
        print()
        
        # Show summary by type
        type_counts = {}
        for spec in all_specs:
            node_type = spec.get("type", "Unknown")
            type_counts[node_type] = type_counts.get(node_type, 0) + 1
        
        print("📊 Element types:")
        for node_type, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"   {node_type}: {count}")
        print()
        
        # Show sample elements
        print("📋 Sample elements (first 5):")
        for spec in all_specs[:5]:
            print(format_ui_spec(spec))
            print()
        
        # If specific node requested, show detailed info
        if node_id:
            print(f"⏳ Fetching specific node: {node_id}...")
            node_data, node_status = client.get_node_detailed(file_key, node_id)
            
            if node_status == "ok":
                nodes = node_data.get("nodes", {})
                if node_id in nodes:
                    node = nodes[node_id]
                    node_doc = node.get("document", {})
                    print(f"✅ Node: {node_doc.get('name')}")
                    
                    # Extract detailed specs for this node
                    node_specs = client.traverse_and_extract(node_doc, max_depth=3)
                    print(f"   Contains {len(node_specs)} elements")
                    print()
                    
                    # Add to output
                    all_specs.extend(node_specs)
        
        # Prepare output
        output = {
            "file_info": {
                "name": file_data.get("name"),
                "key": file_key,
                "lastModified": file_data.get("lastModified"),
                "version": file_data.get("version"),
                "thumbnailUrl": file_data.get("thumbnailUrl")
            },
            "design_tokens": tokens,
            "ui_elements": all_specs,
            "summary": {
                "total_elements": len(all_specs),
                "element_types": type_counts
            }
        }
        
        # Save to file
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        
        print(f"💾 Saved to: {output_path}")
        print(f"   File size: {output_path.stat().st_size / 1024:.1f} KB")
        print()
        print("✅ Extraction complete!")
        
        return 0
        
    except FigmaRateLimitError as e:
        print(f"⚠️  {e}")
        print("   Please wait 60 seconds and try again.")
        return 1
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
