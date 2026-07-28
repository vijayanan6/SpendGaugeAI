#!/usr/bin/env python3
"""PreToolUse hook: block direct edits to real secret files.

.env holds SPENDGAUGEAI_API_KEY and other live secrets — block it (and its
.local/.production variants) from direct Edit/Write, while leaving
.env.example free to edit, since new config vars still need documenting there.
"""
import json
import sys
from pathlib import Path

BLOCKED_NAMES = {".env", ".env.local", ".env.production"}

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
                f"Blocked: direct edits to {name} are disabled to prevent "
                "accidental exposure of SPENDGAUGEAI_API_KEY and other real "
                "secrets. Edit .env.example instead to document a new "
                "config variable, or ask the user to edit this file by hand."
            ),
        }
    }))

sys.exit(0)
