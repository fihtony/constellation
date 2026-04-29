#!/usr/bin/env python3
"""Figma API Quota Tracker

Tracks Figma API usage to help manage the 6 requests/month limit for free tier users.

Usage:
    python3 scripts/quota_tracker.py status
    python3 scripts/quota_tracker.py use
    python3 scripts/quota_tracker.py reset
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

QUOTA_FILE = Path("cache/figma_quota.json")
DEFAULT_LIMIT = 6  # Free tier Tier 1 endpoint limit


class QuotaTracker:
    def __init__(self, quota_file=QUOTA_FILE, limit=DEFAULT_LIMIT):
        self.quota_file = Path(quota_file)
        self.limit = limit
        self.load()
    
    def load(self):
        """Load quota data from file."""
        if self.quota_file.exists():
            try:
                with open(self.quota_file) as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.data = self._create_new_data()
        else:
            self.data = self._create_new_data()
        
        # Check if we need to reset for new month
        current_month = datetime.now().strftime("%Y-%m")
        if self.data.get("month") != current_month:
            self.reset(current_month)
    
    def _create_new_data(self):
        """Create new quota data structure."""
        return {
            "month": datetime.now().strftime("%Y-%m"),
            "used": 0,
            "limit": self.limit,
            "requests": []
        }
    
    def save(self):
        """Save quota data to file."""
        self.quota_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.quota_file, 'w') as f:
            json.dump(self.data, f, indent=2)
    
    def use(self, endpoint="GET /v1/files/:key", note=""):
        """Record API usage."""
        self.data["used"] += 1
        self.data["requests"].append({
            "timestamp": datetime.now().isoformat(),
            "endpoint": endpoint,
            "note": note
        })
        self.save()
        
        remaining = self.remaining()
        print(f"📊 Quota used: {self.data['used']}/{self.limit}")
        print(f"   Remaining: {remaining} requests")
        
        if remaining == 0:
            print("\n⚠️  WARNING: Monthly quota exhausted!")
            print("   Options:")
            print("   1. Wait until next month (resets on 1st)")
            print("   2. Use cached data")
            print("   3. Upgrade to paid plan")
        elif remaining <= 2:
            print(f"\n⚠️  WARNING: Only {remaining} requests remaining this month!")
    
    def remaining(self):
        """Get remaining quota."""
        return max(0, self.limit - self.data["used"])
    
    def can_use(self):
        """Check if quota is available."""
        return self.remaining() > 0
    
    def status(self):
        """Display quota status."""
        remaining = self.remaining()
        percentage = (self.data["used"] / self.limit) * 100
        
        print("=" * 60)
        print("  Figma API Quota Status")
        print("=" * 60)
        print(f"  Month:     {self.data['month']}")
        print(f"  Used:      {self.data['used']}/{self.limit} ({percentage:.1f}%)")
        print(f"  Remaining: {remaining}")
        print()
        
        # Progress bar
        bar_length = 40
        filled = int(bar_length * self.data["used"] / self.limit)
        bar = "█" * filled + "░" * (bar_length - filled)
        print(f"  [{bar}]")
        print()
        
        if remaining == 0:
            print("  Status: ❌ QUOTA EXHAUSTED")
            next_reset = datetime.now().replace(day=1, hour=0, minute=0, second=0)
            if next_reset.month == 12:
                next_reset = next_reset.replace(year=next_reset.year + 1, month=1)
            else:
                next_reset = next_reset.replace(month=next_reset.month + 1)
            print(f"  Next reset: {next_reset.strftime('%Y-%m-%d')}")
        elif remaining <= 2:
            print(f"  Status: ⚠️  LOW ({remaining} remaining)")
        else:
            print(f"  Status: ✅ OK ({remaining} remaining)")
        
        print()
        
        # Recent requests
        if self.data["requests"]:
            print("  Recent requests:")
            for req in self.data["requests"][-5:]:
                timestamp = datetime.fromisoformat(req["timestamp"])
                print(f"    • {timestamp.strftime('%Y-%m-%d %H:%M')} - {req['endpoint']}")
                if req.get("note"):
                    print(f"      Note: {req['note']}")
        
        print("=" * 60)
    
    def reset(self, month=None):
        """Reset quota for new month."""
        if month is None:
            month = datetime.now().strftime("%Y-%m")
        
        old_month = self.data.get("month")
        self.data = self._create_new_data()
        self.data["month"] = month
        self.save()
        
        print(f"✅ Quota reset for {month}")
        if old_month:
            print(f"   Previous month: {old_month}")
    
    def history(self):
        """Show request history."""
        print("=" * 60)
        print("  Request History")
        print("=" * 60)
        print(f"  Month: {self.data['month']}")
        print(f"  Total requests: {len(self.data['requests'])}")
        print()
        
        if not self.data["requests"]:
            print("  No requests recorded yet.")
        else:
            for i, req in enumerate(self.data["requests"], 1):
                timestamp = datetime.fromisoformat(req["timestamp"])
                print(f"  {i}. {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"     Endpoint: {req['endpoint']}")
                if req.get("note"):
                    print(f"     Note: {req['note']}")
                print()
        
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "use", "reset", "history"],
                       help="Action to perform")
    parser.add_argument("--endpoint", default="GET /v1/files/:key",
                       help="API endpoint (for 'use' action)")
    parser.add_argument("--note", default="",
                       help="Note about the request (for 'use' action)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                       help="Monthly quota limit")
    args = parser.parse_args()
    
    tracker = QuotaTracker(limit=args.limit)
    
    if args.action == "status":
        tracker.status()
    elif args.action == "use":
        if not tracker.can_use():
            print("❌ Error: Monthly quota exhausted!")
            print("   Cannot record new request.")
            return 1
        tracker.use(endpoint=args.endpoint, note=args.note)
    elif args.action == "reset":
        confirm = input("Reset quota for current month? (y/n): ")
        if confirm.lower() == 'y':
            tracker.reset()
        else:
            print("Cancelled.")
    elif args.action == "history":
        tracker.history()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
