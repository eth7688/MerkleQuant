#!/usr/bin/env python3
"""Safely update one value in demo_bot_config.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def parse_value(raw: str):
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: set_demo_config_value.py <config_path> <key> <value>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    key = sys.argv[2]
    value = parse_value(sys.argv[3])
    data = json.loads(path.read_text(encoding="utf-8"))
    old = data.get(key)
    data[key] = value
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "key": key, "old": old, "new": value}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
