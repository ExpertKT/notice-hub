#!/usr/bin/env python3
"""开发者发公告工具。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANNOUNCEMENTS_FILE = PROJECT_ROOT / "announcements.json"


def _slug(text: str) -> str:
    """Convert title to slug: lowercase, alphanumeric + dash."""
    import re
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)  # Remove non-word chars except dash
    text = re.sub(r"[\s]+", "-", text)  # Spaces to dash
    return text.strip("-")


def _generate_id(title: str, date: str, existing_ids: set[str]) -> str:
    """Generate unique ID: <date>-<slug>[-N if conflict]."""
    slug = _slug(title)
    base_id = f"{date}-{slug}"
    if base_id not in existing_ids:
        return base_id
    n = 2
    while f"{base_id}-{n}" in existing_ids:
        n += 1
    return f"{base_id}-{n}"


def _load_announcements() -> dict[str, Any]:
    """Load announcements.json, return dict."""
    if not ANNOUNCEMENTS_FILE.is_file():
        return {"version": 1, "items": []}
    try:
        return json.loads(ANNOUNCEMENTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "items": []}


def _save_announcements(data: dict) -> None:
    """Save announcements.json with proper formatting."""
    ANNOUNCEMENTS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )


def _validate_date(date_str: str) -> bool:
    """Check YYYY-MM-DD format."""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def cmd_list(args) -> int:
    """List all announcements."""
    data = _load_announcements()
    items = data.get("items", [])
    if not items:
        print("No announcements found.")
        return 0
    for i, item in enumerate(items, 1):
        print(f"{i}. [{item.get('date', 'N/A')}] {item.get('id', 'N/A')} - {item.get('title', 'N/A')}")
    return 0


def cmd_add(args) -> int:
    """Add new announcement."""
    if not args.title or not args.body:
        print("Error: --title and --body are required.", file=sys.stderr)
        return 1
    
    if args.level and args.level not in ("info", "warn", "critical"):
        print("Error: --level must be info, warn, or critical.", file=sys.stderr)
        return 1
    
    date = args.date if args.date else datetime.utcnow().strftime("%Y-%m-%d")
    if not _validate_date(date):
        print(f"Error: invalid date format: {date} (use YYYY-MM-DD)", file=sys.stderr)
        return 1
    
    data = _load_announcements()
    items = data.get("items", [])
    existing_ids = {item.get("id") for item in items}
    
    announcement_id = args.id if args.id else _generate_id(args.title, date, existing_ids)
    if announcement_id in existing_ids:
        print(f"Error: announcement ID already exists: {announcement_id}", file=sys.stderr)
        return 1
    
    item = {
        "id": announcement_id,
        "date": date,
        "level": args.level if args.level else "info",
        "title": args.title,
        "body": args.body,
        "url": args.url if args.url else "",
        "min_version": args.min_version if args.min_version else "",
        "max_version": args.max_version if args.max_version else "",
        "channel": args.channel if args.channel else "",
        "sticky": args.sticky,
    }
    
    items.insert(0, item)  # Insert at beginning
    data["items"] = items
    _save_announcements(data)
    
    print(json.dumps(item, ensure_ascii=False, indent=2))
    return 0


def cmd_remove(args) -> int:
    """Remove announcement by ID."""
    if not args.id:
        print("Error: --id is required.", file=sys.stderr)
        return 1
    
    data = _load_announcements()
    items = data.get("items", [])
    original_count = len(items)
    items = [item for item in items if item.get("id") != args.id]
    
    if len(items) == original_count:
        print(f"Error: announcement not found: {args.id}", file=sys.stderr)
        return 1
    
    data["items"] = items
    _save_announcements(data)
    print(f"Removed announcement: {args.id}")
    return 0


def cmd_validate(args) -> int:
    """Validate announcements.json structure."""
    data = _load_announcements()
    
    if not isinstance(data, dict):
        print("Error: root is not a dict")
        return 1
    
    if "version" not in data:
        print("Error: missing 'version' field")
        return 1
    
    if "items" not in data or not isinstance(data["items"], list):
        print("Error: 'items' must be a list")
        return 1
    
    items = data["items"]
    errors = []
    seen_ids = set()
    
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"Item {i}: not a dict")
            continue
        
        # Required fields
        if not item.get("id"):
            errors.append(f"Item {i}: missing 'id'")
        elif item.get("id") in seen_ids:
            errors.append(f"Item {i}: duplicate id '{item.get('id')}'")
        else:
            seen_ids.add(item.get("id"))
        
        if not item.get("title"):
            errors.append(f"Item {i}: missing 'title'")
        
        # Type checks
        level = item.get("level", "info")
        if level not in ("info", "warn", "critical"):
            errors.append(f"Item {i}: invalid level '{level}'")
        
        date_str = item.get("date", "")
        if date_str and not _validate_date(date_str):
            errors.append(f"Item {i}: invalid date format '{date_str}'")
        
        if not isinstance(item.get("sticky", False), bool):
            errors.append(f"Item {i}: 'sticky' must be bool")
    
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    
    print("OK")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Announcement management tool")
    subparsers = parser.add_subparsers(dest="command", help="Command")
    
    # list
    subparsers.add_parser("list", help="List all announcements")
    
    # add
    add_parser = subparsers.add_parser("add", help="Add new announcement")
    add_parser.add_argument("--title", required=True, help="Announcement title")
    add_parser.add_argument("--body", required=True, help="Announcement body (supports \\n and - lists)")
    add_parser.add_argument("--level", choices=["info", "warn", "critical"], help="Severity level")
    add_parser.add_argument("--url", help="Optional URL")
    add_parser.add_argument("--min-version", help="Min version (e.g., 1.0.0)")
    add_parser.add_argument("--max-version", help="Max version (e.g., 2.0.0)")
    add_parser.add_argument("--channel", choices=["stable", "beta"], help="Target channel")
    add_parser.add_argument("--sticky", action="store_true", help="Show on every startup")
    add_parser.add_argument("--id", help="Custom ID (auto-generated if not provided)")
    add_parser.add_argument("--date", help="Date YYYY-MM-DD (defaults to today)")
    
    # remove
    remove_parser = subparsers.add_parser("remove", help="Remove announcement")
    remove_parser.add_argument("--id", required=True, help="Announcement ID")
    
    # validate
    subparsers.add_parser("validate", help="Validate announcements.json")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    if args.command == "list":
        return cmd_list(args)
    elif args.command == "add":
        return cmd_add(args)
    elif args.command == "remove":
        return cmd_remove(args)
    elif args.command == "validate":
        return cmd_validate(args)
    
    return 1


if __name__ == "__main__":
    sys.exit(main())
