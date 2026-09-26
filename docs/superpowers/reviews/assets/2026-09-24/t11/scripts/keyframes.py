#!/usr/bin/env python3
"""T11: keep a few full-window key frames (PNG, 880 px wide) per scenario before the raw dumps are deleted."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402  (re-running build is idempotent; reuses its helpers)
OUT = os.path.join(build.OUT, "keyframes")
os.makedirs(OUT, exist_ok=True)
wanted = {
    "supersede": [("after-click2", "click", 272 + 100), ("after-click3", "click", 602 + 100), ("peak", "commit", 600), ("final", "commit", 1190)],
    "reduced": [("pending", "click", 100), ("commit+40", "commit", 40), ("before-relay-removal", "commit", 1100), ("final", "commit", 1390)],
    "toggle": [("peak-before-toggle", "commit", 380), ("after-toggle-on", "commit", 420), ("after-toggle-off", "commit", 720), ("final", "commit", 2800)],
    "reveal": [("commit", "commit", 0)],
}
for scn, items in wanted.items():
    seq, commit, click = build.shots(scn)
    for label, ref, t in items:
        base = commit if ref == "commit" else click
        at, path = min(seq, key=lambda s: abs(s[0] - base - t))
        name = f"{scn}-{label}-{ref}{at - base:+.0f}ms.png"
        build.png(path, os.path.join(OUT, name), scale=880)
        print(name)
