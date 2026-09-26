"""T7 evidence: crop state regions from the T1 captures and pair each crop
with a greyscale copy (ITU-R BT.601 luma via PIL 'L'), so "distinct without
colour" can be judged from the lower half of each image.

Run from the repo root:  python3 docs/superpowers/reviews/assets/2026-09-24/t7/crops.py
Needs Pillow. Boxes are in capture pixels (2x Retina, window incl. 28 pt title bar).
"""
from pathlib import Path
from PIL import Image

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app"

CROPS = {
    # name: [(capture, box), ...]  -- stacked vertically, then greyscale copy below
    "header-status-memory": [
        ("light-1400-01-disconnected-panel", (2140, 78, 2795, 122)),
        ("light-1400-05-transfer-details", (2140, 78, 2795, 122)),
        ("dark-1400-01-disconnected-panel", (2140, 78, 2795, 122)),
        ("dark-1400-05-transfer-details", (2140, 78, 2795, 122)),
    ],
    "banner-success": [
        ("light-1400-04-alert-banner", (16, 216, 830, 250)),
        ("dark-1400-04-alert-banner", (16, 216, 830, 250)),
    ],
    "save-disabled-vs-enabled": [
        ("light-1400-05-transfer-details", (826, 316, 1215, 356)),  # Save disabled, Pause enabled
        ("light-1400-04-alert-banner", (826, 360, 1215, 400)),      # Save enabled, Pause enabled
        ("dark-1400-05-transfer-details", (826, 316, 1215, 356)),
        ("dark-1400-04-alert-banner", (826, 360, 1215, 400)),
    ],
    "tabs-selected": [
        ("light-1400-05-transfer-details", (20, 216, 720, 248)),
        ("dark-1400-05-transfer-details", (20, 216, 720, 248)),
    ],
    "tree-selected-row-progress": [
        ("light-1400-05-transfer-details", (60, 746, 800, 790)),
        ("dark-1400-05-transfer-details", (60, 746, 800, 790)),
        ("light-1400-04-alert-banner", (60, 1000, 800, 1044)),  # temp1 selected, pointer elsewhere
    ],
    "checkbox-autoscroll": [
        ("light-1400-01-disconnected-panel", (1490, 604, 1760, 642)),
        ("dark-1400-01-disconnected-panel", (1490, 604, 1760, 642)),
    ],
    "checkbox-queryable": [
        ("light-1400-06-publish", (840, 788, 1200, 826)),
        ("dark-1400-06-publish", (840, 788, 1200, 826)),
    ],
    "chunk-labels": [
        ("light-1400-05-transfer-details", (820, 420, 1520, 496)),
        ("dark-1400-05-transfer-details", (820, 420, 1520, 496)),
    ],
}


def stack(images):
    w = max(i.width for i in images)
    h = sum(i.height for i in images) + 4 * (len(images) - 1)
    out = Image.new("RGB", (w, h), (128, 128, 128))
    y = 0
    for i in images:
        out.paste(i, (0, y))
        y += i.height + 4
    return out


for name, parts in CROPS.items():
    colour = [Image.open(APP / f"{cap}.png").convert("RGB").crop(box) for cap, box in parts]
    grey = [c.convert("L").convert("RGB") for c in colour]
    stack(colour + grey).save(HERE / f"crop-{name}.png")
    print(f"crop-{name}.png  <- " + "; ".join(f"{c} {b}" for c, b in parts))
