#!/usr/bin/env python3
"""T11: compare the last captured frame of several runs (footer excluded).
usage: final.py <ref_dir> <other_dir> [...]"""
import os, sys
import numpy as np
def last(d):
    f = sorted(x for x in os.listdir(d) if x.endswith(".ppm"))[-1]
    b = open(os.path.join(d, f), "rb").read(); head, data = b.split(b"\n", 1); _, w, h, _ = head.split()
    return f, np.frombuffer(data, np.uint8).reshape(int(h), int(w), 3).astype(np.int16)
rf, ref = last(sys.argv[1])
H = int(494 * 2)  # footer (dynamic status text) starts at y = 494 pt
print(f"reference: {sys.argv[1].rstrip('/').split('/')[-1]}/{rf}")
for d in sys.argv[2:]:
    f, img = last(d)
    ch = (img[:H] != ref[:H]).any(axis=2)
    ys, xs = np.nonzero(ch)
    bbox = f" bbox px x{xs.min()}-{xs.max()} y{ys.min()}-{ys.max()}" if ys.size else ""
    print(f"{d.rstrip('/').split('/')[-1]}/{f}: {int(ch.sum())} changed pixels above the footer{bbox}")
