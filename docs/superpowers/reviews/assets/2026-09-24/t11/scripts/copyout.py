#!/usr/bin/env python3
"""Copy final logs, analysis outputs and scripts into the T11 evidence folder."""
import os, shutil, subprocess
SCR = os.path.dirname(os.path.abspath(__file__))
T11 = os.path.join(SCR, "t11")
MAIN = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True).stdout.strip()  # run from inside the repo
OUT = os.path.join(MAIN, "docs/superpowers/reviews/assets/2026-09-24/t11")
for f in os.listdir(T11):
    if f.endswith(".log"):
        shutil.copy(os.path.join(T11, f), os.path.join(OUT, "logs", f))
    elif f.startswith(("diff-", "sides-", "analysis", "final")) and f.endswith(".txt"):
        shutil.copy(os.path.join(T11, f), os.path.join(OUT, f))
for f in ["run.sh", "runall.sh", "analyze.py", "diff.py", "sides.py", "final.py", "alldiff.py", "build.py", "keyframes.py", "cargorun.sh", "copyout.py"]:
    shutil.copy(os.path.join(SCR, f), os.path.join(OUT, "scripts", f))
for root, _, files in os.walk(OUT):
    for f in sorted(files):
        p = os.path.join(root, f)
        print(f"{os.path.getsize(p):>9}  {os.path.relpath(p, OUT)}")
