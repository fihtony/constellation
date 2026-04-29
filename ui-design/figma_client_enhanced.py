#!/usr/bin/env python3
"""Enhanced Figma REST API client with detailed design information extraction.

This module provides comprehensive Figma design data extraction including:
- UI element specifications (size, position, colors, fonts)
- Layout specifications (spacing, alignment, constraints)
- Component hierarchy and structure
- Style definitions and design tokens
- Export-ready assets information

Features:
- Exponential backoff for rate limiting
- Detailed node property extraction
- Design token extraction
- Layout constraint analysis
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FigmaRateLimitError(Exception):
    """Raised when Figma API rate limit is exceeded."""
    pass


class FigmaClientEnhanced:
    """Enhanced Figma REST API client with detailed design extraction."""
    
    def __init__(self, token: str | None = None):
        """Initialize Figma client.
        
        Args:
            token: Figma personal access token. If None, reads from FIGMA_TOKEN env var.
        """
        self.token = token or os.environ.get("FIGMA_TOKEN", "")
        self.api_base = "https://api.figma.com/v1"
        self.last_request_time = 0
        self.min_request_interval = 1.0  # Minimum 1 second between requests
        
    def _wait_for_rate_limit(self):
        """Ensure minimum interval between requests."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self.last_request_time = time.time()
    
    def _request(
        self,
        path: str,
        method: str = "GET",
        max_retries: int = 3,
        base_delay: float = 2.0
    ) -> Tuple[int, Dict[str, Any]]:
        """Make HTTP request to Figma API with retry logic.
        
        Args:
            path: API endpoint path (e.g., "/files/{key}")
            method: HTTP method
            max_retries: Maximum number of retry attempts
            base_delay: Base delay for exponential backoff (seconds)
            
        Returns:
            Tuple of (status_code, response_body)
            
        Raises:
            FigmaRateLimitError: If rate limit exceeded after all retries
        """
        url = f"{self.api_base}{path}"
        headers = {
            "X-Figma-Token": self.token,
            "Accept": "application/json"
        }
        
        for attempt in range(max_retries):
            self._wait_for_rate_limit()
            
            try:
                req = Request(url, headers=headers, method=method)
                with urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8")
                    return resp.status, json.loads(body) if body.strip() else {}
            except HTTPError as e:
                if e.code == 429:
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        print(f"Rate limit hit, waiting {delay}s before retry {attempt + 1}/{max_retries}")
                        time.sleep(delay)
                        continue
                    else:
                        raise FigmaRateLimitError("Rate limit exceeded after all retries")
                else:
                    body = e.read().decode("utf-8", errors="replace")
                    try:
                        return e.code, json.loads(body)
                    except Exception:
                        return e.code, {"error": body[:200]}
            except (URLError, OSError) as e:
                return 0, {"error": str(e)}
        
        return 0, {"error": "Max retries exceeded"}
    
    def get_file_detailed(self, file_key: str) -> Tuple[Dict[str, Any], str]:
        """Fetch complete file information with all nodes.
        
        Args:
            file_key: Figma file key
            
        Returns:
            Tuple of (file_data, status)
        """
        status, body = self._request(f"/files/{file_key}")
        
        if status == 200:
            return body, "ok"
        elif status == 429:
            return {"error": "Rate limit exceeded"}, "rate_limited"
        elif status == 403:
            return {"error": "Access denied"}, "forbidden"
        elif status == 404:
            return {"error": "File not found"}, "not_found"
        else:
            return body, "error"
    
    def get_node_detailed(
        self,
        file_key: str,
        node_id: str
    ) -> Tuple[Dict[str, Any], str]:
        """Fetch detailed information for a specific node.
        
        Args:
            file_key: Figma file key
            node_id: Node ID (e.g., "1:470")
            
        Returns:
            Tuple of (node_data, status)
        """
        status, body = self._request(f"/files/{file_key}/nodes?ids={node_id}")
        
        if status == 200:
            return body, "ok"
        elif status == 429:
            return {"error": "Rate limit exceeded"}, "rate_limited"
        else:
            return body, "error"
    
    def extract_ui_specs(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """Extract UI element specifications from a node.
        
        Args:
            node: Figma node object
            
        Returns:
            Dictionary containing:
            - dimensions: width, height
            - position: x, y coordinates
            - colors: fill, stroke colors
            - typography: font family, size, weight, line height
            - effects: shadows, blurs
            - constraints: layout constraints
        """
        specs = {
            "type": node.get("type"),
            "name": node.get("name"),
            "dimensions": {},
            "position": {},
            "colors": {},
            "typography": {},
            "effects": [],
            "constraints": {},
            "layout": {}
        }
        
        # Dimensions
        if "absoluteBoundingBox" in node:
            bbox = node["absoluteBoundingBox"]
            specs["dimensions"] = {
                "width": bbox.get("width"),
                "height": bbox.get("height")
            }
            specs["position"] = {
                "x": bbox.get("x"),
                "y": bbox.get("y")
            }
        
        # Colors - fills
        if "fills" in node and node["fills"]:
            fills = []
            for fill in node["fills"]:
                if fill.get("type") == "SOLID" and "color" in fill:
                    color = fill["color"]
                    fills.append({
                        "r": color.get("r", 0),
                        "g": color.get("g", 0),
                        "b": color.get("b", 0),
                        "a": color.get("a", 1),
                        "hex": self._rgba_to_hex(color)
                    })
            specs["colors"]["fills"] = fills
        
        # Colors - strokes
        if "strokes" in node and node["strokes"]:
            strokes = []
            for stroke in node["strokes"]:
                if stroke.get("type") == "SOLID" and "color" in stroke:
                    color = stroke["color"]
                    strokes.append({
                        "r": color.get("r", 0),
                        "g": color.get("g", 0),
                        "b": color.get("b", 0),
                        "a": color.get("a", 1),
                        "hex": self._rgba_to_hex(color),
                        "weight": node.get("strokeWeight", 1)
                    })
            specs["colors"]["strokes"] = strokes
        
        # Typography
        if "style" in node:
            style = node["style"]
            specs["typography"] = {
                "fontFamily": style.get("fontFamily"),
                "fontSize": style.get("fontSize"),
                "fontWeight": style.get("fontWeight"),
                "lineHeight": style.get("lineHeightPx"),
                "letterSpacing": style.get("letterSpacing"),
                "textAlign": style.get("textAlignHorizontal"),
                "textDecoration": style.get("textDecoration")
            }
        
        # Effects (shadows, blurs)
        if "effects" in node:
            for effect in node["effects"]:
                if effect.get("visible", True):
                    effect_spec = {
                        "type": effect.get("type"),
                        "radius": effect.get("radius"),
                        "offset": effect.get("offset"),
                        "color": effect.get("color")
                    }
                    specs["effects"].append(effect_spec)
        
        # Layout constraints
        if "constraints" in node:
            constraints = node["constraints"]
            specs["constraints"] = {
                "horizontal": constraints.get("horizontal"),
                "vertical": constraints.get("vertical")
            }
        
        # Auto layout properties
        if node.get("layoutMode"):
            specs["layout"] = {
                "mode": node.get("layoutMode"),
                "primaryAxisSizingMode": node.get("primaryAxisSizingMode"),
                "counterAxisSizingMode": node.get("counterAxisSizingMode"),
                "primaryAxisAlignItems": node.get("primaryAxisAlignItems"),
                "counterAxisAlignItems": node.get("counterAxisAlignItems"),
                "paddingLeft": node.get("paddingLeft"),
                "paddingRight": node.get("paddingRight"),
                "paddingTop": node.get("paddingTop"),
                "paddingBottom": node.get("paddingBottom"),
                "itemSpacing": node.get("itemSpacing")
            }
        
        return specs
    
    def _rgba_to_hex(self, color: Dict[str, float]) -> str:
        """Convert RGBA color to hex string.
        
        Args:
            color: Dict with r, g, b, a values (0-1 range)
            
        Returns:
            Hex color string (e.g., "#FF5733")
        """
        r = int(color.get("r", 0) * 255)
        g = int(color.get("g", 0) * 255)
        b = int(color.get("b", 0) * 255)
        return f"#{r:02X}{g:02X}{b:02X}"
    
    def extract_layout_specs(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """Extract layout specifications from a frame or group.
        
        Args:
            node: Figma node object
            
        Returns:
            Dictionary containing layout information
        """
        layout = {
            "type": node.get("type"),
            "name": node.get("name"),
            "children_count": len(node.get("children", [])),
            "layout_mode": node.get("layoutMode"),
            "spacing": {},
            "padding": {},
            "alignment": {},
            "sizing": {}
        }
        
        # Spacing
        layout["spacing"]["itemSpacing"] = node.get("itemSpacing")
        
        # Padding
        layout["padding"] = {
            "top": node.get("paddingTop"),
            "right": node.get("paddingRight"),
            "bottom": node.get("paddingBottom"),
            "left": node.get("paddingLeft")
        }
        
        # Alignment
        layout["alignment"] = {
            "primaryAxis": node.get("primaryAxisAlignItems"),
            "counterAxis": node.get("counterAxisAlignItems")
        }
        
        # Sizing
        layout["sizing"] = {
            "primaryAxis": node.get("primaryAxisSizingMode"),
            "counterAxis": node.get("counterAxisSizingMode")
        }
        
        return layout
    
    def traverse_and_extract(
        self,
        node: Dict[str, Any],
        depth: int = 0,
        max_depth: int = 10
    ) -> List[Dict[str, Any]]:
        """Recursively traverse node tree and extract all UI specs.
        
        Args:
            node: Root node to start traversal
            depth: Current depth (for recursion)
            max_depth: Maximum depth to traverse
            
        Returns:
            List of extracted UI specifications
        """
        if depth > max_depth:
            return []
        
        specs = []
        
        # Extract current node specs
        node_spec = self.extract_ui_specs(node)
        node_spec["depth"] = depth
        specs.append(node_spec)
        
        # Recursively process children
        if "children" in node:
            for child in node["children"]:
                child_specs = self.traverse_and_extract(child, depth + 1, max_depth)
                specs.extend(child_specs)
        
        return specs
    
    def get_design_tokens(self, file_data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract design tokens (colors, typography, spacing) from file.
        
        Args:
            file_data: Complete Figma file data
            
        Returns:
            Dictionary of design tokens
        """
        tokens = {
            "colors": {},
            "typography": {},
            "spacing": {},
            "effects": {}
        }
        
        # Extract from styles if available
        if "styles" in file_data:
            for style_id, style in file_data["styles"].items():
                style_type = style.get("styleType")
                if style_type == "FILL":
                    tokens["colors"][style.get("name", style_id)] = style
                elif style_type == "TEXT":
                    tokens["typography"][style.get("name", style_id)] = style
                elif style_type == "EFFECT":
                    tokens["effects"][style.get("name", style_id)] = style
        
        return tokens


def main():
    """Example usage of enhanced Figma client."""
    import sys
    
    token = os.environ.get("FIGMA_TOKEN") or os.environ.get("TEST_FIGMA_TOKEN")
    if not token:
        print("Error: FIGMA_TOKEN or TEST_FIGMA_TOKEN environment variable not set")
        sys.exit(1)
    
    client = FigmaClientEnhanced(token)
    
    # Example: Get detailed file information
    file_key = "gxd2LNayM2hh3V3qTlcyPF"
    print(f"Fetching file: {file_key}")
    
    file_data, status = client.get_file_detailed(file_key)
    
    if status == "ok":
        print(f"✓ File name: {file_data.get('name')}")
        print(f"✓ Last modified: {file_data.get('lastModified')}")
        
        # Extract design tokens
        tokens = client.get_design_tokens(file_data)
        print(f"✓ Design tokens extracted: {len(tokens['colors'])} colors, "
              f"{len(tokens['typography'])} text styles")
        
        # Get document root
        document = file_data.get("document", {})
        if document:
            # Extract all UI specs
            all_specs = client.traverse_and_extract(document, max_depth=5)
            print(f"✓ Extracted {len(all_specs)} UI elements")
            
            # Save to file
            output = {
                "file_info": {
                    "name": file_data.get("name"),
                    "lastModified": file_data.get("lastModified"),
                    "version": file_data.get("version")
                },
                "design_tokens": tokens,
                "ui_elements": all_specs
            }
            
            with open("figma_design_specs.json", "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2, ensure_ascii=False)
            print("✓ Saved to figma_design_specs.json")
    else:
        print(f"✗ Error: {status}")
        print(f"  {file_data}")


if __name__ == "__main__":
    main()
