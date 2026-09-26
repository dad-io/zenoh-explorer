#!/usr/bin/env python3
"""T11: build the evidence folder from the scratch runs.
Crops (560x480 px = 280x240 pt at 2x, same size as the T9 strip frames), strips, videos, logs."""
import os, re, shutil, subprocess
import numpy as np

SCR = os.path.dirname(os.path.abspath(__file__))
T11 = os.path.join(SCR, "t11")
MAIN = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True).stdout.strip()  # run from inside the repo
OUT = os.path.join(MAIN, "docs/superpowers/reviews/assets/2026-09-24/t11")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
T9_TIMES = [-21, 82, 174, 265, 351, 439, 523, 608, 695, 780, 867, 954, 1041, 1126, 1207, 1282, 1358]
CROP_PT = (70, 44, 350, 284)  # x0, y0, x1, y1 in points: header bottom, key, gutter, panel B's left edge
os.makedirs(os.path.join(OUT, "frames"), exist_ok=True)
os.makedirs(os.path.join(OUT, "video"), exist_ok=True)
os.makedirs(os.path.join(OUT, "logs"), exist_ok=True)

def read_ppm(p):
    b = open(p, "rb").read(); head, data = b.split(b"\n", 1); _, w, h, _ = head.split()
    return np.frombuffer(data, np.uint8).reshape(int(h), int(w), 3)

def frame_times(log):
    ft, commit, click = {}, None, None
    for line in open(log):
        m = re.match(r"frame=(\d+) t=([\d.]+)", line)
        if m: ft[int(m.group(1))] = float(m.group(2))
        m = re.match(r"# event t=([\d.]+)ms (click|commit) action=\d+", line)
        if m and m.group(2) == "commit": commit = float(m.group(1))
        if m and m.group(2) == "click" and click is None: click = float(m.group(1))
    return ft, commit, click

def shots(scn):
    d = os.path.join(T11, f"shots-shots-{scn}")
    ft, commit, click = frame_times(os.path.join(T11, f"shots-{scn}.log"))
    fs = sorted(f for f in os.listdir(d) if f.endswith(".ppm"))
    return [(ft[int(re.search(r"_n(\d+)", f).group(1))], os.path.join(d, f)) for f in fs], commit, click

def png(src_ppm, dst, crop=None, scale=None):
    vf = []
    if crop:
        x0, y0, x1, y1 = [int(v * 2) for v in crop]
        vf.append(f"crop={x1-x0}:{y1-y0}:{x0}:{y0}")
    if scale: vf.append(f"scale={scale}:-1:flags=lanczos")
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", src_ppm] + (["-vf", ",".join(vf)] if vf else []) + [dst]
    subprocess.run(cmd, check=True)

# ---- reveal strip: nearest captured frame to each T9 time (ms after commit) ----
seq, commit, click = shots("reveal")
cells, picked = [], []
for i, t in enumerate(T9_TIMES):
    at, path = min(seq, key=lambda s: abs(s[0] - commit - t))
    rel = at - commit
    name = f"s{i:02d}_{rel:+.0f}ms.png"
    png(path, os.path.join(OUT, "frames", name), CROP_PT)
    picked.append((t, rel, name))
# full-window frames at pending, peak and settled, for context
for label, t in [("pending", -300), ("peak", 600), ("settled", 1300)]:
    at, path = min(seq, key=lambda s: abs(s[0] - commit - t))
    png(path, os.path.join(OUT, f"window-{label}-{at - commit:+.0f}ms.png"))

hdr = ("T11 frame strip — egui 0.29.1 spike (branch spike/causal-motion-egui), Subscribe → simulated 600 ms commit; "
       "crop x70–350 y44–284 pt at 2× (same 560×480 px as T9); t = ms after the commit, taken from egui's i.time of the "
       "painted frame (GL framebuffer read back after painting that frame); real-time playback, not slowed")
style = ("body{margin:0;background:#222;font:14px ui-monospace,Menlo,monospace;color:#eee;width:1160px}h1{font-size:15px;"
         "margin:8px 12px}.g{display:grid;grid-template-columns:repeat(4,280px);gap:8px;padding:0 12px 12px}figure{margin:0}"
         "img{width:280px;height:240px;display:block}figcaption{padding:2px 0}")
cells = "".join(f'<figure><img src="frames/{n}"><figcaption>t = {rel:+.0f} ms (T9 slot {t} ms)</figcaption></figure>' for t, rel, n in picked)
open(os.path.join(OUT, "strip.html"), "w").write(f"<!doctype html><meta charset=utf-8><style>{style}</style>\n<h1>{hdr}</h1><div class=g>{cells}</div>\n")

# ---- side-by-side comparison: T9 frame | T11 frame ----
t9 = sorted(os.listdir(os.path.join(OUT, "../t9/frames")))
style2 = ("body{margin:0;background:#222;font:13px ui-monospace,Menlo,monospace;color:#eee;width:1240px}h1{font-size:14px;margin:8px 12px}"
          ".g{display:grid;grid-template-columns:repeat(3,400px);gap:10px 12px;padding:0 12px 12px}figure{margin:0}"
          ".p{display:flex;gap:4px}img{width:198px;height:170px;display:block}figcaption{padding:2px 0}")
pairs = "".join(f'<figure><div class=p><img src="../t9/frames/{a}"><img src="frames/{n}"></div><figcaption>T9 {t} ms | T11 {rel:+.0f} ms</figcaption></figure>'
                for a, (t, rel, n) in zip(t9, picked))
open(os.path.join(OUT, "compare.html"), "w").write(
    f"<!doctype html><meta charset=utf-8><style>{style2}</style>\n<h1>Left: T9 specimen (reference-ux, Burrow→Arena, 0.1× playback, converted time). "
    f"Right: T11 egui spike (Subscribe, real time, ms after commit). Both crops 560×480 px at 2×.</h1><div class=g>{pairs}</div>\n")

def render(html, png_out, w, h):
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={w},{h}",
                    f"--screenshot={png_out}", "file://" + html], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
render(os.path.join(OUT, "strip.html"), os.path.join(OUT, "strip.png"), 1160, 1420)
render(os.path.join(OUT, "compare.html"), os.path.join(OUT, "compare.png"), 1240, 1260)

# ---- videos: every captured frame, shown for its real duration (window scaled to 880 px) ----
for scn in ["reveal", "supersede", "reduced", "toggle"]:
    seq, commit, click = shots(scn)
    lst = os.path.join(T11, f"concat-{scn}.txt")
    with open(lst, "w") as f:
        for (t, p), nxt in zip(seq, seq[1:] + [(seq[-1][0] + 33.0, None)]):
            f.write(f"file '{p}'\nduration {(nxt[0] - t) / 1000.0:.4f}\n")
        f.write(f"file '{seq[-1][1]}'\n")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-vf", "scale=880:-2:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-crf", "18",
                    "-r", "120", "-fps_mode", "cfr", os.path.join(OUT, "video", f"{scn}.mp4")], check=True)

# ---- logs and analysis outputs ----
for f in os.listdir(T11):
    if f.endswith(".log") or f.startswith(("diff-", "sides-", "analysis", "final")) and f.endswith(".txt"):
        shutil.copy(os.path.join(T11, f), os.path.join(OUT, "logs" if f.endswith(".log") else ".", f))
for f in ["run.sh", "runall.sh", "analyze.py", "diff.py", "sides.py", "final.py", "alldiff.py", "build.py", "cargorun.sh"]:
    os.makedirs(os.path.join(OUT, "scripts"), exist_ok=True)
    shutil.copy(os.path.join(SCR, f), os.path.join(OUT, "scripts", f))
print("picked:", picked)
