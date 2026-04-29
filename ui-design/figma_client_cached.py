#!/usr/bin/env python3
"""Cached Figma REST API client with persistent storage.

This module extends the enhanced Figma client with caching capabilities
to minimize API calls and avoid rate limiting.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from figma_client_enhanced import FigmaClientEnhanced, FigmaRateLimitError


class FigmaCache:
    """Simple file-based cache for Figma API responses."""
    
    def __init__(self, cache_dir: str = ".figma_cache", ttl: int = 3600):
        """Initialize cache.
        
        Args:
            cache_dir: Directory to store cache files
            ttl: Time-to-live in seconds (default: 1 hour)
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
    
    def _get_cache_key(self, key: str) -> str:
        """Generate cache file name from key."""
        # Use hash to avoid filesystem issues with special characters
        hash_key = hashlib.md5(key.encode()).hexdigest()
        return f"{hash_key}.json"
    
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get cached data if available and not expired.
        
        Args:
            key: Cache key
            
        Returns:
            Cached data or None if not found/expired
        """
        cache_file = self.cache_dir / self._get_cache_key(key)
        
        if not cache_file.exists():
            return None
        
        # Check if expired
        age = time.time() - cache_file.stat().st_mtime
        if age > self.ttl:
            # Expired, remove it
            cache_file.unlink()
            return None
        
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            # Corrupted cache file, remove it
            cache_file.unlink()
            return None
    
    def set(self, key: str, data: Dict[str, Any]) -> None:
        """Store data in cache.
        
        Args:
            key: Cache key
            data: Data to cache
        """
        cache_file = self.cache_dir / self._get_cache_key(key)
        try:
            cache_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            print(f"Warning: Failed to write cache: {e}")
    
    def clear(self) -> None:
        """Clear all cached data."""
        for cache_file in self.cache_dir.glob("*.json"):
            cache_file.unlink()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        cache_files = list(self.cache_dir.glob("*.json"))
        total_size = sum(f.stat().st_size for f in cache_files)
        
        return {
            "files": len(cache_files),
            "total_size_bytes": total_size,
            "total_size_mb": total_size / (1024 * 1024),
            "cache_dir": str(self.cache_dir)
        }


class FigmaClientCached(FigmaClientEnhanced):
    """Figma client with caching support."""
    
    def __init__(
        self,
        token: str | None = None,
        cache_dir: str = ".figma_cache",
        cache_ttl: int = 3600,
        use_cache: bool = True
    ):
        """Initialize cached Figma client.
        
        Args:
            token: Figma personal access token
            cache_dir: Directory for cache storage
            cache_ttl: Cache time-to-live in seconds
            use_cache: Whether to use caching
        """
        super().__init__(token)
        self.use_cache = use_cache
        self.cache = FigmaCache(cache_dir, cache_ttl) if use_cache else None
    
    def get_file_detailed(
        self,
        file_key: str,
        force_refresh: bool = False
    ) -> Tuple[Dict[str, Any], str]:
        """Fetch file with caching support.
        
        Args:
            file_key: Figma file key
            force_refresh: Skip cache and fetch fresh data
            
        Returns:
            Tuple of (file_data, status)
        """
        cache_key = f"file_{file_key}"
        
        # Try cache first
        if self.use_cache and not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                print(f"✓ Using cached data for file {file_key}")
                return cached, "ok"
        
        # Cache miss or force refresh, fetch from API
        print(f"⏳ Fetching file {file_key} from API...")
        file_data, status = super().get_file_detailed(file_key)
        
        # Cache successful responses
        if status == "ok" and self.use_cache:
            self.cache.set(cache_key, file_data)
            print(f"✓ Cached file {file_key}")
        
        return file_data, status
    
    def get_node_detailed(
        self,
        file_key: str,
        node_id: str,
        force_refresh: bool = False
    ) -> Tuple[Dict[str, Any], str]:
        """Fetch node with caching support.
        
        Args:
            file_key: Figma file key
            node_id: Node ID
            force_refresh: Skip cache and fetch fresh data
            
        Returns:
            Tuple of (node_data, status)
        """
        cache_key = f"node_{file_key}_{node_id}"
        
        # Try cache first
        if self.use_cache and not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                print(f"✓ Using cached data for node {node_id}")
                return cached, "ok"
        
        # Cache miss or force refresh, fetch from API
        print(f"⏳ Fetching node {node_id} from API...")
        node_data, status = super().get_node_detailed(file_key, node_id)
        
        # Cache successful responses
        if status == "ok" and self.use_cache:
            self.cache.set(cache_key, node_data)
            print(f"✓ Cached node {node_id}")
        
        return node_data, status
    
    def clear_cache(self) -> None:
        """Clear all cached data."""
        if self.cache:
            self.cache.clear()
            print("✓ Cache cleared")
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        if self.cache:
            return self.cache.get_stats()
        return {"enabled": False}


def main():
    """Example usage of cached Figma client."""
    import os
    import sys
    
    token = os.environ.get("FIGMA_TOKEN") or os.environ.get("TEST_FIGMA_TOKEN")
    if not token:
        print("Error: FIGMA_TOKEN or TEST_FIGMA_TOKEN environment variable not set")
        sys.exit(1)
    
    # Initialize cached client with 1-hour TTL
    client = FigmaClientCached(token, cache_ttl=3600)
    
    file_key = "gxd2LNayM2hh3V3qTlcyPF"
    
    print("=" * 60)
    print("Figma Cached Client Demo")
    print("=" * 60)
    print()
    
    # First fetch (will hit API)
    print("First fetch:")
    file_data, status = client.get_file_detailed(file_key)
    
    if status == "ok":
        print(f"✓ File: {file_data.get('name')}")
        print(f"  Version: {file_data.get('version')}")
    else:
        print(f"✗ Error: {status}")
    
    print()
    
    # Second fetch (will use cache)
    print("Second fetch (should use cache):")
    file_data2, status2 = client.get_file_detailed(file_key)
    
    if status2 == "ok":
        print(f"✓ File: {file_data2.get('name')}")
    
    print()
    
    # Cache stats
    stats = client.get_cache_stats()
    print("Cache statistics:")
    print(f"  Files: {stats.get('files', 0)}")
    print(f"  Size: {stats.get('total_size_mb', 0):.2f} MB")
    print(f"  Location: {stats.get('cache_dir', 'N/A')}")


if __name__ == "__main__":
    main()
