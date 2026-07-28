#!/usr/bin/env python3
"""PreToolUse hook: block direct edits to clients/js/package-lock.json.

That file should only change via `npm install`/`npm ci`, never a hand-edit.
"""
import json
import sys
from pathlib import Path

BLOCKED_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

file_path = payload.get("tool_input", {}).get("file_path", "")
name = Path(file_path.replace("\\", "/")).name

if name in BLOCKED_NAMES:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Blocked: {name} should only change via the package manager "
                "(e.g. `npm install`), never a direct hand-edit."
            ),
        }
    }))

sys.exit(0)
