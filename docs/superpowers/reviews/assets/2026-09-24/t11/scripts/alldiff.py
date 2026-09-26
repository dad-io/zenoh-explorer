#!/usr/bin/env python3
"""Run diff.py for each scenario and print per-class maxima."""
import os, subprocess
os.chdir(os.path.dirname(os.path.abspath(__file__)))
for s, ref in [("reveal", "commit"), ("supersede", "commit"), ("reduced", "commit"), ("toggle", "commit")]:
    out = f"t11/diff-{s}.txt"
    subprocess.run(["python3", "diff.py", f"t11/shots-shots-{s}", f"t11/shots-{s}.log", out],
                   env={**os.environ, "REF": ref}, stdout=subprocess.DEVNULL, check=True)
    lines = open(out).read().splitlines()
    hdr_i = next(i for i, l in enumerate(lines) if l.startswith("t_ms"))
    cols = lines[hdr_i].split("\t")
    rows = [l.split("\t") for l in lines[hdr_i + 1:]]
    print(f"== {s}: {lines[0]}")
    print("   frames:", len(rows), " t range:", rows[0][0], "..", rows[-1][0])
    print("   max changed px per class:", {c: max(int(r[i]) for r in rows) for i, c in enumerate(cols[1:-1], 1)})
    nz = [r[0] for r in rows if int(r[2]) > 0]
    if nz: print("   hdr_face changed at t:", nz[0], "..", nz[-1])
