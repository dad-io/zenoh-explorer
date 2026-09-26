"""Regenerate `tui/testdata/cockpit.json` from the constructed inputs tests/test_cockpit.py uses.

Run: `PYTHONPATH=src .venv/bin/python -m bearhug.replay.cockpit_fixture`
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from bearhug.paths import REPO_ROOT


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from tests.test_cockpit import _fixture_cockpit

    with tempfile.TemporaryDirectory() as scratch:
        cockpit = _fixture_cockpit(Path(scratch))
    target = REPO_ROOT / "tui" / "testdata" / "cockpit.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cockpit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
