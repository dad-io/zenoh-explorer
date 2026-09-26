#!/usr/bin/env python3
"""T11: figures in the review doc's T11 section that analyze.py does not print.

usage: extra.py <logs dir>   (run from scripts/ so that analyze.py imports; output -> extra.txt)

Prints, per log: the interval histogram and share under 5 ms between consecutive
animating frames; cpu_prev shifted to the frame it measures; the not-animating costs
before the click and at rest; the effect and whole-frame medians over the same
0-392.3 ms after the commit (the window in which rel-toggle animates); and the
effect cost of animating pending frames that carry exactly one surface.
"""
import os, sys, statistics as st

ARGS, sys.argv = sys.argv[1:], sys.argv[:1]  # analyze.py summarises sys.argv[1:] on import
from analyze import parse, pct, num  # noqa: E402

TIMING = ["rel-reveal-committed", "rel-reveal", "rel-reveal-rows2000", "dev-reveal",
          "rel-supersede", "rel-toggle", "rel-reduced"]
SHOTS = ["shots-reveal", "shots-supersede", "shots-reduced", "shots-toggle"]
WINDOW_MS = 392.3  # last animating frame of rel-toggle (analysis.txt:207)


def anim(f):
    return f["animating"] == "true"


def summary(v, fmt="{:.3f}"):
    if not v:
        return "n=0"
    return f"median {fmt.format(st.median(v))} p95 {fmt.format(pct(v, .95))} max {fmt.format(max(v))} (n={len(v)})"


def main(d):
    for name in TIMING:
        frames, _ = parse(os.path.join(d, name + ".log"))
        print(f"== {name}")
        iv = [num(b["interval"]) for a, b in zip(frames, frames[1:]) if anim(a) and anim(b)]
        if iv:
            bins = [("<2", 0, 2), ("2-4", 2, 4), ("4-5", 4, 5), ("5-12", 5, 12), ("12-16", 12, 16), (">=16", 16, 1e9)]
            hist = {k: sum(lo <= x < hi for x in iv) for k, lo, hi in bins}
            odd = sorted(round(x, 2) for x in iv if 4 <= x < 5)
            under5 = sum(x < 5 for x in iv)
            print(f"  intervals between consecutive animating frames (n={len(iv)}): {hist}; 4-5 ms values {odd}")
            print(f"  share under 5 ms: {under5}/{len(iv)} = {100 * under5 / len(iv):.0f} %")
            # cpu_prev describes the previous frame: attribute frame i+1's value to frame i
            shifted = [num(b["cpu_prev"]) for a, b in zip(frames, frames[1:]) if anim(a) and num(b["cpu_prev"]) is not None]
            logged = [num(f["cpu_prev"]) for f in frames if anim(f) and num(f["cpu_prev"]) is not None]
            print(f"  cpu while animating, as logged:  {summary(logged)} ms")
            print(f"  cpu while animating, shifted:    {summary(shifted)} ms")
        click = next((i for i, f in enumerate(frames) if f["since_click"] not in (None, "-")), None)
        pre = [f for f in frames[:click] if num(f["effect_us"]) is not None and num(f["effect_us"]) <= 1
               and num(f["cpu_prev"]) is not None and int(f["frame"]) > 3]
        if pre:
            c = [num(f["cpu_prev"]) for f in pre]
            print(f"  pre-click frames {pre[0]['frame']}-{pre[-1]['frame']} (effect_us <= 1): cpu median {st.median(c):.3f} "
                  f"range {min(c):.3f}-{max(c):.3f} ms (n={len(c)})")
        last = max((i for i, f in enumerate(frames) if anim(f)), default=None)
        if last is not None:
            rest = [f for f in frames[last + 1:] if num(f["effect_us"]) is not None]
            print("  after the last animating frame, frame:effect_us (* = relay still present): "
                  + " ".join(f"{f['frame']}:{f['effect_us']}{'*' if 'relay' in f['surf'] else ''}" for f in rest))
        win = [f for f in frames if anim(f) and num(f["since_commit"]) is not None and 0 <= num(f["since_commit"]) <= WINDOW_MS]
        if win:
            print(f"  animating, 0-{WINDOW_MS} ms after the commit: effect median {st.median([num(f['effect_us']) for f in win]):.0f} us, "
                  f"cpu_prev median {st.median([num(f['cpu_prev']) for f in win]):.3f} ms (n={len(win)})")
        pend = [num(f["effect_us"]) for f in frames if anim(f) and f["since_commit"] == "-" and len(f["surf"]) == 1]
        if pend:
            print(f"  animating pending frames with one surface: effect median {st.median(pend):.0f} us (n={len(pend)})")
    for name in SHOTS:
        frames, _ = parse(os.path.join(d, name + ".log"))
        c = [num(f["cpu_prev"]) for f in frames if num(f["cpu_prev"]) is not None]
        print(f"== {name}: cpu_prev over all frames {summary(c)} ms")


if __name__ == "__main__":
    main(ARGS[0] if ARGS else "../logs")
