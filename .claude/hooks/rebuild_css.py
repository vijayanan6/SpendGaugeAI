#!/usr/bin/env python3
"""PostToolUse hook: regenerate static/app.css when static/src/input.css is edited.

Keeps the committed build artifact from drifting out of sync with its Tailwind
source (see CLAUDE.md's "static/app.css is committed, not gitignored" note).
"""
import json
import subprocess
import sys
from pathlib import Path

TARGET_SUFFIX = "src/spendgaugeai/static/src/input.css"

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

file_path = payload.get("tool_input", {}).get("file_path", "")
normalized = file_path.replace("\\", "/")

if not normalized.endswith(TARGET_SUFFIX):
    sys.exit(0)

repo_root = Path(__file__).resolve().parents[2]
build_script = repo_root / "scripts" / "build-css.sh"

result = subprocess.run(["bash", str(build_script)], cwd=repo_root)
sys.exit(result.returncode)
