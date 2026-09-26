"""T3: chrome-vs-work area from the T1 captures' measured band edges.
Band edges (2x px) come from measure.py output (measurements.txt). Units below are points (px/2).
Content area = window minus the 28 pt macOS title bar (56 px), i.e. the egui viewport."""
TB = 28  # pt, OS title bar, excluded
def pt(px): return px / 2 - TB  # window px row -> content pt y
cases = {
 # name: (W, H, work_top_px, bottom_px, split_px(l,r), tree_data_top_px, detail_data_top_px)
 "1400x900 connected (02)":     (1400, 900, 256, 1840, (813, 816), 551, 421),
 "1400x900 connected+banner (04)": (1400, 900, 300, 1840, (813, 816), None, None),
 "1400x900 disconnected (01)":  (1400, 900, 524, 1840, (813, 816), 655, 689),
 "1000x600 connected (02)":     (1000, 600, 256, 1240, (813, 816), 551, 421),
 "1000x600 connected+banner (04)": (1000, 600, 300, 1240, (813, 816), None, None),
 "1000x600 disconnected (01)":  (1000, 600, 524, 1240, (813, 816), 655, 689),
}
print("| State | Content W×H pt | Work rect (x 8..W−8, y top..bottom) pt | Work area pt² | Chrome area pt² | Work % | Chrome % | Chrome:work | Tree+list data surface % |")
print("|---|---|---|---|---|---|---|---|---|")
for k, (W, H, wt, wb, (sl, sr), td, dd) in cases.items():
    top, bot = pt(wt), pt(wb)
    ww = W - 16
    work = ww * (bot - top)
    total = W * H
    chrome = total - work
    data = ""
    if td:
        tree_w = (sl - 16) / 2          # 8pt margin -> splitter
        det_w = (W * 2 - 16 - sr) / 2   # splitter -> right margin
        ds = tree_w * (bot - pt(td)) + det_w * (bot - pt(dd))
        data = f"{ds/total*100:.1f} % ({tree_w:.1f}×{bot-pt(td):.1f} + {det_w:.1f}×{bot-pt(dd):.1f} = {ds:,.0f})"
    print(f"| {k} | {W}×{H} = {total:,} | {ww} × ({bot:g}−{top:g}) = {ww}×{bot-top:g} | {work:,.0f} | {chrome:,.0f} | {work/total*100:.1f} % | {chrome/total*100:.1f} % | {chrome/work:.3f} | {data} |")
