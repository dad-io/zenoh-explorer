#!/usr/bin/env python3
"""T11: summarise a causal_motion_spike per-frame log. usage: analyze.py <log> [...]"""
import re, sys, statistics as st

SURF = re.compile(r" (\w+)/(\w+)((?:\(\w+\))*):d=\[([^\]]*)\] c=\[([^\]]*)\] rank=\[([^\]]*)\]")
SIDES = ["top", "right", "bottom", "left"]

def pct(v, p):
    v = sorted(v); k = (len(v) - 1) * p; f = int(k); c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)

def parse(path):
    frames, events = [], []
    for line in open(path):
        if line.startswith("# event"):
            events.append(line.strip()); continue
        if not line.startswith("frame="):
            continue
        kv = dict(re.findall(r"(\w+)=(\S+)", line.split(" source/")[0].split(" relay/")[0].split(" header/")[0].split(" result/")[0].split(" link:")[0]))
        f = {k: kv.get(k) for k in ["frame", "t", "interval", "cpu_prev", "effect_us", "since_click", "since_commit", "animating", "repaint", "action"]}
        f["surf"] = {}
        for m in SURF.finditer(line):
            name, role, flags, d, c, r = m.groups()
            f["surf"][name] = dict(flags=flags, d=[float(x) for x in d.split(",")], c=c.split(","), rank=[int(x) for x in r.split(",")])
        lm = re.search(r"link:p=([\d.]+) o=([\d.]+)(\(frozen\))?", line)
        f["link"] = (float(lm.group(1)), float(lm.group(2)), bool(lm.group(3))) if lm else None
        frames.append(f)
    return frames, events

def num(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def main(path):
    frames, events = parse(path)
    print(f"== {path}")
    for e in events: print("  ", e)
    anim = [f for f in frames if f["animating"] == "true"]
    # intervals between two consecutive animating frames (excludes the wake-up after an idle gap)
    iv = [num(b["interval"]) for a, b in zip(frames, frames[1:]) if a["animating"] == "true" and b["animating"] == "true" and num(b["interval"]) is not None]
    gaps = [num(b["interval"]) for a, b in zip(frames, frames[1:]) if b["animating"] == "true" and a["animating"] != "true" and num(b["interval"]) is not None]
    cpu = [num(f["cpu_prev"]) for f in anim if num(f["cpu_prev"]) is not None]
    eff = [num(f["effect_us"]) for f in anim if num(f["effect_us"]) is not None]
    idle = [f for f in frames if f["animating"] == "false" and num(f["cpu_prev"]) is not None and int(f["frame"]) > 3]
    icpu = [num(f["cpu_prev"]) for f in idle]
    if iv:
        print(f"  frames logged: {len(frames)} total, {len(anim)} animating; interval between consecutive animating frames ms (n={len(iv)}): mean {st.mean(iv):.2f} median {st.median(iv):.2f} p95 {pct(iv,.95):.2f} max {max(iv):.2f}")
        print(f"  wake-up intervals into an animating frame after an idle gap (ms): {[round(g,1) for g in gaps]}")
        print(f"  cpu per frame (update+tessellate+paint, excl. vsync) while animating: median {st.median(cpu):.3f} p95 {pct(cpu,.95):.3f} max {max(cpu):.3f} ms")
        print(f"  effect build (rects->meshes, set) while animating: median {st.median(eff):.0f} p95 {pct(eff,.95):.0f} max {max(eff):.0f} us")
    if icpu:
        print(f"  cpu per frame when not animating: median {st.median(icpu):.3f} ms (n={len(icpu)})")
    rp = {}
    for f in frames:
        k = (f["repaint"] or "?").split("(")[0]; rp[k] = rp.get(k, 0) + 1
    print(f"  repaint policy decisions: {rp}")
    # fast-frame window after the final action's commit
    com = [f for f in frames if num(f["since_commit"]) is not None]
    if com:
        a_last = max(num(f["since_commit"]) for f in com if f["animating"] == "true") if any(f["animating"] == "true" for f in com) else None
        print(f"  last animating frame at since_commit = {a_last} ms")
    # per-surface: side start (first depth>0 after commit), peak hold window, settle colour
    for name in ["source", "relay", "header", "result"]:
        rows = [(num(f["since_commit"]), f["surf"][name]) for f in com if name in f["surf"] and "pending" not in f["surf"][name]["flags"]]
        if not rows: continue
        rank = rows[0][1]["rank"]
        starts, peaks = [], []
        for s in range(4):
            st_ = next((t for t, x in rows if x["d"][s] > 0.0), None)
            pk = [t for t, x in rows if abs(x["d"][s] - 1.15) < 1e-3]
            starts.append(st_); peaks.append((min(pk), max(pk)) if pk else None)
        order = sorted(range(4), key=lambda s: rank[s])
        print(f"  {name}: rank(top,right,bottom,left)={rank} order={[SIDES[s] for s in order]}")
        print(f"     first frame with depth>0 per side (ms after commit): " + ", ".join(f"{SIDES[s]}={starts[s]}" for s in order))
        print(f"     frames at peak 1.15: " + ", ".join(f"{SIDES[s]}={peaks[s]}" for s in order))
        peakc = [x["c"] for t, x in rows if all(abs(d - 1.15) < 1e-3 for d in x["d"])]
        if peakc: print(f"     peak colours (t,r,b,l) = {peakc[0]}")
        last_t, last = rows[-1]
        print(f"     last logged ({last_t} ms): d={last['d']} c={last['c']} flags={last['flags']}")
    gone = [num(f["since_commit"]) for f in com if "relay" not in f["surf"]]
    if gone: print(f"  relay absent from since_commit = {min(gone)} ms")
    lk = [(num(f["since_commit"]), f["link"]) for f in com if f["link"]]
    if lk:
        full = [t for t, l in lk if l[0] >= 0.999]
        print(f"  link: first frame {lk[0]}, pattern=1 from {min(full) if full else None} to {max(full) if full else None}, last {lk[-1]}")
    # pending segment
    pend = [(num(f["since_click"]), f["surf"]["source"]) for f in frames if "source" in f["surf"] and "pending" in f["surf"]["source"]["flags"]]
    if pend:
        print(f"  pending frames: {len(pend)}, from {pend[0][0]} to {pend[-1][0]} ms after click; max depth {max(max(x['d']) for _, x in pend):.3f}; colours {set(c for _, x in pend for c in x['c'])}")

for p in sys.argv[1:]:
    if p.startswith("-"): continue
    main(p)
