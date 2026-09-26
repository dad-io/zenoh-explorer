"""Share of chromatic pixels (HSV saturation >= 0.35, value >= 0.25) that are the iOS-blue
family (hue 200-225 deg) in each T1 capture, excluding the 28 pt macOS title bar (56 px at 2x)."""
import colorsys, glob, os
from PIL import Image
for f in sorted(glob.glob('app/*-1400-0[2-6]*.png')):
    im = Image.open(f).convert('RGB'); w, h = im.size
    px = im.crop((0, 56, w, h)).resize((w // 2, (h - 56) // 2), Image.NEAREST).getdata()
    chrom = blue = 0
    for r, g, b in px:
        hh, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s >= 0.35 and v >= 0.25:
            chrom += 1
            if 200 <= hh * 360 <= 225: blue += 1
    tot = len(px)
    print(f'{os.path.basename(f)}: chromatic {100*chrom/tot:.2f}% of window, of which blue family {100*blue/max(chrom,1):.1f}%')
