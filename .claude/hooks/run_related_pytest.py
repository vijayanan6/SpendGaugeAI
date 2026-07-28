#!/usr/bin/env python3
"""PostToolUse hook: run the matching test file after editing a source module.

Maps src/spendgaugeai/<name>.py -> tests/test_<name>.py and runs it if that
test file exists. Informational only — never blocks, just surfaces pass/fail
immediately instead of waiting for a manual pytest run.
"""
import json
import subprocess
import sys
from pathlib import Path

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

file_path = payload.get("tool_input", {}).get("file_path", "")
normalized = file_path.replace("\\", "/")

prefix = "src/spendgaugeai/"
if prefix not in normalized or not normalized.endswith(".py"):
    sys.exit(0)

module_name = Path(normalized).stem
if module_name == "__init__":
    sys.exit(0)

repo_root = Path(__file__).resolve().parents[2]
test_file = repo_root / "tests" / f"test_{module_name}.py"
if not test_file.exists():
    sys.exit(0)

venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
if not venv_python.exists():
    venv_python = repo_root / ".venv" / "bin" / "python"
python_exe = str(venv_python) if venv_python.exists() else sys.executable

result = subprocess.run(
    [python_exe, "-m", "pytest", str(test_file), "-q"],
    cwd=repo_root,
)
sys.exit(0)
