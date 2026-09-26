#!/usr/bin/env python3
"""T11: pixel diff of spike screenshots (PPM, read back from the GL framebuffer).
usage: diff.py <shots_dir> <log> <out_txt>
Regions are in points from the log's '# rect' lines; ppp from '# pixels_per_point'.
Baseline = the last frame captured before the first click (t < 0).
Classes (exclusive, first match wins): face regions; gutter (between the panels, outside
their bands); surface bands (8 pt edge band + 1.5 pt catchlight below); elsewhere.
The footer (dynamic status text) is excluded."""
import os, re, sys
import numpy as np

shots, logp, outp = sys.argv[1], sys.argv[2], sys.argv[3]
rects, ppp = {}, 2.0
for line in open(logp):
    m = re.match(r"# pixels_per_point=([\d.]+)", line)
    if m: ppp = float(m.group(1))
    m = re.match(r"# rect (\w+) ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)", line)
    if m: rects[m.group(1)] = tuple(float(x) for x in m.groups()[1:])

def read_ppm(p):
    b = open(p, "rb").read()
    head, data = b.split(b"\n", 1)
    _, w, h, _ = head.split()
    return np.frombuffer(data, np.uint8).reshape(int(h), int(w), 3)

BAND, CATCH = 8.0, 1.5
def shrink(r, d): return (r[0] + d, r[1] + d, r[2] - d, r[3] - d)
ftime, clicks, commit = {}, [], None
for line in open(logp):
    m = re.match(r"frame=(\d+) t=([\d.]+)", line)
    if m: ftime[int(m.group(1))] = float(m.group(2))
    m = re.match(r"# event t=([\d.]+)ms (click|commit) action=\d+", line.strip())
    if m and m.group(2) == "click": clicks.append(float(m.group(1)))
    if m and m.group(2) == "commit": commit = float(m.group(1))
first_click = clicks[0]
REF = os.environ.get("REF", "commit")  # label frames relative to the commit (default) or the first click
ref = commit if (REF == "commit" and commit is not None) else first_click
files = sorted(f for f in os.listdir(shots) if f.endswith(".ppm"))
def fno(f): return int(re.search(r"_n(\d+)", f).group(1))
def t_of(f): return ftime[fno(f)] - ref
base_f = [f for f in files if ftime[fno(f)] < first_click][-1]
base = read_ppm(os.path.join(shots, base_f)).astype(np.int16)
H, W, _ = base.shape
ys, xs = np.mgrid[0:H, 0:W]
X, Y = (xs + 0.5) / ppp, (ys + 0.5) / ppp
def box(r): return (X >= r[0]) & (X < r[2]) & (Y >= r[1]) & (Y < r[3])
def band(r): return (box(r) & ~box(shrink(r, BAND))) | ((X >= r[0]) & (X < r[2]) & (Y >= r[3]) & (Y < r[3] + CATCH))

src, hdr, pa, pb = rects["source_key"], rects["header"], rects["panel_a"], rects["panel_b"]
faces = {
    "src_face": shrink(src, 9),
    "hdr_face": shrink(hdr, 9),
    "A_face_above_key": (pa[0] + 9, pa[1] + 9, pa[2] - 9, src[1] - 1),
    "A_face_below_key": (pa[0] + 9, src[3] + 2, pa[2] - 9, pa[3] - 9),
    "B_face": shrink(pb, 9),
}
gutter = (pa[2], pa[1], pb[0], pb[3])
footer = Y >= pa[3] + 12.0
masks, taken = {}, footer.copy()
for k, r in faces.items():
    masks[k] = box(r) & ~taken; taken |= masks[k]
bands = band(src) | band(hdr) | band(pa) | band(pb)
masks["gutter"] = box(gutter) & ~bands & ~taken; taken |= masks["gutter"]
masks["bands"] = bands & ~taken; taken |= masks["bands"]
masks["elsewhere"] = ~taken
with open(outp, "w") as out:
    out.write(f"# times: ms relative to {REF} (click at {first_click:.1f}, commit at {commit} ms app time); baseline = last frame before the first click\n# baseline {base_f}; image {W}x{H} px, ppp {ppp}; band {BAND} pt; regions in points (x0,y0,x1,y1)\n")
    for k, r in faces.items(): out.write(f"# {k}: {r}\n")
    out.write(f"# gutter: {gutter}; footer y >= {pa[3] + 12.0} excluded (dynamic status text)\n")
    out.write("# columns: changed pixels (any channel differs from baseline) per class; gutter_y = y-range (pt) of changed gutter pixels\n")
    out.write("t_ms\t" + "\t".join(masks) + "\tgutter_y\n")
    for f in files:
        ch = (read_ppm(os.path.join(shots, f)).astype(np.int16) != base).any(axis=2)
        row = [int((ch & m).sum()) for m in masks.values()]
        gy = Y[ch & masks["gutter"]]
        out.write(f"{t_of(f):.0f}\t" + "\t".join(map(str, row)) + (f"\t{gy.min():.1f}-{gy.max():.1f}" if gy.size else "\t-") + "\n")
print(open(outp).read())
# optional: DUMP=<t_ms> writes elsewhere.ppm marking 'elsewhere' changed pixels in magenta
if os.environ.get("DUMP"):
    f = min(files, key=lambda f: abs(t_of(f) - float(os.environ["DUMP"])))
    img = read_ppm(os.path.join(shots, f)).copy()
    ch = (img.astype(np.int16) != base).any(axis=2) & masks["elsewhere"]
    img[ch] = [255, 0, 255]
    ys_, xs_ = np.nonzero(ch)
    if ys_.size: print("elsewhere bbox px", xs_.min(), ys_.min(), xs_.max(), ys_.max(), "n", ys_.size)
    open("elsewhere.ppm", "wb").write(f"P6 {W} {H} 255\n".encode() + img.tobytes())
