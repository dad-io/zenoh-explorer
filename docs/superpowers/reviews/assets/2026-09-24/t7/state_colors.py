"""T7 state-colour calculations (no dependencies for the maths; Pillow only for sampling).

Run from the repo root:
  python3 docs/superpowers/reviews/assets/2026-09-24/t7/state_colors.py > docs/superpowers/reviews/assets/2026-09-24/t7/state_colors.txt
WCAG 2.x relative luminance / contrast, as in t5/contrast.py.
"""
from collections import Counter
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"


def lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def L(rgb):
    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def cr(a, b):
    la, lb = sorted((L(a), L(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def luma601(rgb):  # what PIL 'L' greyscale shows
    r, g, b = rgb
    return round(0.299 * r + 0.587 * g + 0.114 * b)


def tint_towards(c, t):  # ecolor-0.29.1 lib.rs:140-161, opaque branch (a >= 170)
    return tuple(ch // 2 + tc // 2 for ch, tc in zip(c, t))


PRIMARY, PRIMARY_HOVER = (0, 122, 255), (0, 102, 217)
DARK_PRIMARY, DARK_PRIMARY_HOVER = (10, 132, 255), (64, 156, 255)
SUCCESS, WARNING, ERROR = (52, 199, 89), (255, 149, 0), (255, 59, 48)
DARK_SUCCESS = (48, 209, 88)
TEXT_PRIMARY, TEXT_SECONDARY = (28, 28, 30), (60, 60, 67)
WHITE = (255, 255, 255)
PANEL_L, PANEL_D, SURFACE_L = (255, 255, 255), (75, 75, 75), (250, 250, 250)
HEADER_L, HEADER_D = (248, 248, 248), (45, 45, 45)
SEL_L, SEL_D = (230, 255, 255), (73, 195, 255)  # composited selection, measured in T5
FADE_DARK_STYLE, FADE_LIGHT_STYLE = (27, 27, 27), (248, 248, 248)  # noninteractive.weak_bg_fill, style.rs:1412 / 1457

print("## 1. Disabled buttons (Ui::add_enabled -> Ui::disable -> fade_to_color = noninteractive.weak_bg_fill)")
print("Captured configuration: egui dark style active in both app themes (T5), so the fade target is gray(27).")
for name, face, label in [
    ("light Save File (white label)", PRIMARY, WHITE),
    ("dark  Save File (white label)", DARK_PRIMARY, WHITE),
    ("light Publish/Query/Subscribe (TEXT_PRIMARY label)", PRIMARY, TEXT_PRIMARY),
    ("dark  Publish/Query/Subscribe (white label)", DARK_PRIMARY, WHITE),
]:
    df, dl = tint_towards(face, FADE_DARK_STYLE), tint_towards(label, FADE_DARK_STYLE)
    print(f"- {name}: enabled face {face} label {label} ({cr(face, label):.2f}:1) -> disabled face {df} label {dl} ({cr(df, dl):.2f}:1); "
          f"enabled vs disabled face {cr(face, df):.2f}:1; greyscale luma {luma601(face)} -> {luma601(df)}")
df = tint_towards(PRIMARY, FADE_LIGHT_STYLE)
print(f"- (if the OS supplied egui's light style: light disabled face {df}, {cr(PRIMARY, df):.2f}:1 vs enabled)")

print("\n## 2. Enabled Pause label vs disabled Save label (light theme)")
print(f"- Pause label TEXT_SECONDARY on PRIMARY: {cr(TEXT_SECONDARY, PRIMARY):.2f}:1, greyscale luma label {luma601(TEXT_SECONDARY)} face {luma601(PRIMARY)}")
dl, df = tint_towards(WHITE, FADE_DARK_STYLE), tint_towards(PRIMARY, FADE_DARK_STYLE)
print(f"- disabled Save label {dl} on {df}: {cr(dl, df):.2f}:1, greyscale luma label {luma601(dl)} face {luma601(df)}")

print("\n## 3. Status hues in greyscale (memory readout, connection word, banner)")
for bg_name, bg in [("light header", HEADER_L), ("dark header", HEADER_D), ("light panel", PANEL_L)]:
    for n, c in [("SUCCESS", SUCCESS), ("WARNING", WARNING), ("ERROR", ERROR)]:
        print(f"- {n} on {bg_name}: {cr(c, bg):.2f}:1, luma {luma601(c)}")
print(f"- SUCCESS vs WARNING (text colours against each other): {cr(SUCCESS, WARNING):.2f}:1; luma {luma601(SUCCESS)} vs {luma601(WARNING)}")
print(f"- SUCCESS vs ERROR: {cr(SUCCESS, ERROR):.2f}:1; WARNING vs ERROR: {cr(WARNING, ERROR):.2f}:1")

print("\n## 4. Focus on selectable labels (tabs, tree rows): focused-unselected pill vs selected fill")
print("Widgets::style -> active when has_focus (style.rs:1077); selected_label.rs:69 paints weak_bg_fill when focused.")
print(f"- light: focused pill PRIMARY_HOVER {PRIMARY_HOVER} vs panel: {cr(PRIMARY_HOVER, PANEL_L):.2f}:1; selected fill {SEL_L} vs panel: {cr(SEL_L, PANEL_L):.2f}:1")
print(f"- dark: focused pill DARK_PRIMARY_HOVER {DARK_PRIMARY_HOVER} vs panel: {cr(DARK_PRIMARY_HOVER, PANEL_D):.2f}:1; selected fill {SEL_D} vs panel: {cr(SEL_D, PANEL_D):.2f}:1; focused pill vs selected fill: {cr(DARK_PRIMARY_HOVER, SEL_D):.2f}:1")

print("\n## 5. Text caret (TextCursorStyle; the app never sets it)")
DARK_CARET, LIGHT_CARET = (192, 222, 255), (0, 83, 125)  # style.rs:851 default (dark), style.rs:1371 (light)
print(f"- dark-style caret {DARK_CARET} on light-theme field SURFACE {SURFACE_L}: {cr(DARK_CARET, SURFACE_L):.2f}:1  <- captured configuration")
print(f"- dark-style caret on dark-theme field (60,60,60): {cr(DARK_CARET, (60, 60, 60)):.2f}:1")
print(f"- light-style caret {LIGHT_CARET} on {SURFACE_L}: {cr(LIGHT_CARET, SURFACE_L):.2f}:1  <- only if the OS reports light")

print("\n## 6. Focused button face vs resting face (active vs inactive weak_bg_fill), and hover")
print(f"- light: {cr(PRIMARY_HOVER, PRIMARY):.2f}:1 ; dark: {cr(DARK_PRIMARY_HOVER, DARK_PRIMARY):.2f}:1 ; hover uses the same fill and rim (app.rs:190-191, 203-204, 222-223, 235-236)")

print("\n## 7. Sampled from captures (most common colours in a box, capture pixels)")
try:
    from PIL import Image

    def sample(cap, box, label, n=4):
        im = Image.open(APP / f"{cap}.png").convert("RGB")
        c = Counter(im.crop(box).getdata())
        print(f"- {cap} {label} {box}: " + ", ".join(f"{k}x{v}" for k, v in c.most_common(n)))

    sample("light-1400-05-transfer-details", (834, 322, 970, 350), "Save File DISABLED")
    sample("light-1400-04-alert-banner", (834, 366, 1076, 394), "Save File enabled")
    sample("light-1400-05-transfer-details", (994, 322, 1100, 350), "Pause enabled")
    sample("dark-1400-05-transfer-details", (834, 322, 970, 350), "Save File DISABLED")
    sample("dark-1400-04-alert-banner", (834, 366, 1076, 394), "Save File enabled")
except ImportError:
    print("- Pillow not available; skipped")
