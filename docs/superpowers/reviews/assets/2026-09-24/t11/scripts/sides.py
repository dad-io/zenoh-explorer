#!/usr/bin/env python3
"""T11: pixel-based per-side onset and colour. usage: sides.py <shots_dir> <log> <out_txt>
For each receiver, each side's 8 pt band (middle third of the side, away from corners) is
compared with the first frame at or after the commit (all receivers still at depth 0); 'onset' = first frame (ms after commit) in
which any pixel of that side band differs. Also prints the mean RGB of the outermost 2 pt of
each side band per frame (edge colour as rendered)."""
import os, re, sys
import numpy as np

shots, logp, outp = sys.argv[1], sys.argv[2], sys.argv[3]
rects, ppp, ftime, commit = {}, 2.0, {}, None
for line in open(logp):
    m = re.match(r"# rect (\w+) ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)", line)
    if m: rects[m.group(1)] = tuple(float(x) for x in m.groups()[1:])
    m = re.match(r"frame=(\d+) t=([\d.]+)", line)
    if m: ftime[int(m.group(1))] = float(m.group(2))
    m = re.match(r"# event t=([\d.]+)ms commit action=\d+", line.strip())
    if m: commit = float(m.group(1))
def read_ppm(p):
    b = open(p, "rb").read(); head, data = b.split(b"\n", 1); _, w, h, _ = head.split()
    return np.frombuffer(data, np.uint8).reshape(int(h), int(w), 3)
files = sorted(f for f in os.listdir(shots) if f.endswith(".ppm"))
fno = lambda f: int(re.search(r"_n(\d+)", f).group(1))
t_of = lambda f: ftime[fno(f)] - commit
pre = [f for f in files if t_of(f) >= 0][0]
base = read_ppm(os.path.join(shots, pre)).astype(np.int16)
SIDES = ["top", "right", "bottom", "left"]
def side_box(r, s, depth=8.0):
    x0, y0, x1, y1 = r; w, h = x1 - x0, y1 - y0
    return [(x0 + w / 3, y0, x1 - w / 3, y0 + depth), (x1 - depth, y0 + h / 3, x1, y1 - h / 3),
            (x0 + w / 3, y1 - depth, x1 - w / 3, y1), (x0, y0 + h / 3, x0 + depth, y1 - h / 3)][s]
def px(b): return tuple(int(round(v * ppp)) for v in b)
out = open(outp, "w")
out.write(f"# per-side onset vs first post-commit frame ({pre}); ms after commit; side band = middle third, 8 pt deep\n")
for name in ["source_key", "panel_a", "header", "panel_b"]:
    r = rects[name]; onset = {}
    for f in files:
        if t_of(f) <= t_of(pre): continue
        img = read_ppm(os.path.join(shots, f)).astype(np.int16)
        for s in range(4):
            if s in onset: continue
            x0, y0, x1, y1 = px(side_box(r, s))
            if (img[y0:y1, x0:x1] != base[y0:y1, x0:x1]).any(): onset[s] = t_of(f)
    order = sorted(onset, key=lambda s: onset[s])
    out.write(f"{name}: onset " + ", ".join(f"{SIDES[s]}={onset[s]:.0f}" for s in order) + f"  -> pixel order {[SIDES[s] for s in order]}\n")
out.write("\n# mean RGB of the outer 2 pt of each side band (middle third), per frame\n")
out.write("t_ms\t" + "\t".join(f"{n}.{s}" for n in ["source_key", "panel_b"] for s in SIDES) + "\n")
for f in files:
    img = read_ppm(os.path.join(shots, f))
    cells = []
    for n in ["source_key", "panel_b"]:
        for s in range(4):
            x0, y0, x1, y1 = px(side_box(rects[n], s, 2.0))
            m = img[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
            cells.append("#%02x%02x%02x" % tuple(int(round(v)) for v in m))
    out.write(f"{t_of(f):.0f}\t" + "\t".join(cells) + "\n")
out.close()
print(open(outp).read())
