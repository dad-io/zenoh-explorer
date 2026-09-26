#!/usr/bin/env python3
"""forbidden-command-gate.py — project-supplied PreToolUse forbidden-command gate.

Bear Hug's generalization of Barracuda's hard-safety.py: a PreToolUse hook that blocks a tool call
whose full command matches a rule the TARGET PROJECT wrote to ``.bearhug/forbidden-commands.json``,
rather than five rules hand-maintained in this file. Absent that config, this hook is inert -- it
allows everything, and records that verdict rather than inventing a rule.

Thin by design, same split as hard-safety.py/hardsafety.py: this file is the I/O shell only (read
the PreToolUse payload from stdin, write the block decision to stdout). All parsing and matching
logic lives in scripts/hooks/forbidden_command_rules.py, where it is tested directly.

Any internal failure here exits 0 and stamps the crash rather than raising: a broken hook must
never wedge the session (the same decision 0277 pattern hard-safety.py follows).
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _stamp(verdict: str) -> None:
    try:
        root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        directory = os.path.join(root, ".automation-stamps")
        os.makedirs(directory, exist_ok=True)
        with open(
            os.path.join(directory, "forbidden-command-gate"), "w", encoding="utf-8"
        ) as handle:
            handle.write(f"{int(time.time())} {verdict}\n")
    except Exception:  # noqa: BLE001 -- a stamp failure must not surface as a gate failure
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 -- unreadable input is not a rule violation
        _stamp("SKIP-unreadable-payload")
        return

    try:
        import forbidden_command_rules as rules_module

        root = None
        if isinstance(payload, dict):
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                root = cwd
        root = root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

        rules = rules_module.load_config(root)
        if not rules:
            _stamp("inert")
            return

        tool_name = payload.get("tool_name") if isinstance(payload, dict) else None
        tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
        found = rules_module.violations(tool_name or "", tool_input or {}, rules, root)
    except Exception as exc:  # noqa: BLE001 -- any internal fault fails open, never wedges Stop
        _stamp(f"CRASH-{type(exc).__name__}")
        return

    if not found:
        _stamp("pass")
        return

    reason = "\n\n".join(f"[{rule_id}] {message}" for rule_id, message in found)
    _stamp("BLOCKED-" + ",".join(rule_id for rule_id, _ in found))
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
