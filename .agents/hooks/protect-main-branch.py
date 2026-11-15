#!/usr/bin/env python3
"""PreToolUse hook: stop Claude from getting around the main-branch push guard.

`.githooks/pre-push` refuses every push to main. That only holds while the
hook runs, so a Bash command that pushes is denied when it:

  - passes `--no-verify`, which skips pre-push hooks
  - sets `core.hooksPath` or other git config through the environment
  - sets ALLOW_MAIN_PUSH or touches CLAUDECODE (earlier versions of the hook's override and Claude check)
  - clears the environment with `env -i`
  - runs while core.hooksPath isn't `.githooks`, so the guard isn't installed

GitHub's "Protect main" ruleset (.github/rulesets/protect-main.json) rejects
direct pushes to main on the server; this catches them before they leave.
"""

import json
import os
import re
import subprocess
import sys

HOOKS_PATH = ".githooks"

PUSH = re.compile(r"\bgit\b.*\bpush\b", re.S)
BYPASSES = [
    (re.compile(r"--no-verify\b"), "`--no-verify` skips the pre-push hook"),
    (re.compile(r"core\.hooksPath", re.I), "it changes `core.hooksPath`, which decides whether the hook runs"),
    (re.compile(r"\bGIT_CONFIG_"), "it sets git config through the environment"),
    (re.compile(r"\bALLOW_MAIN_PUSH\b"), "ALLOW_MAIN_PUSH is the user's override, not Claude's"),
    (re.compile(r"\bCLAUDECODE\b"), "it touches CLAUDECODE, which the hook uses to recognize Claude"),
    (re.compile(r"\benv\b(\s+-\S+)*\s+(-i\b|--ignore-environment\b)"), "`env -i` clears CLAUDECODE"),
]


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not PUSH.search(command):
        return

    for pattern, why in BYPASSES:
        if pattern.search(command):
            deny(f"Push blocked: {why}. Main changes only through a pull request.")

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or "."
    try:
        result = subprocess.run(["git", "-C", project_dir, "config", "--get", "core.hooksPath"],
                                capture_output=True, text=True, timeout=10)
        hooks_path = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as err:
        deny(f"Push blocked: couldn't check that the main-branch pre-push guard is installed ({err}).")
    if hooks_path != HOOKS_PATH:
        deny(f"Push blocked: the main-branch pre-push guard isn't installed (core.hooksPath is "
             f"{hooks_path or 'unset'!r}, expected {HOOKS_PATH!r}). "
             f"Ask the user to run `git config core.hooksPath {HOOKS_PATH}` in this repo.")


if __name__ == "__main__":
    main()
