"""Build before/after landmark strips from T1 captures with measured guide lines (2x px)."""
from PIL import Image, ImageDraw
import os
A = os.path.join(os.path.dirname(__file__), '..', 'app')
pairs = [
 # out, before, after, crop_w, crop_h, [(y_before, y_after, label)]
 ("pair-1400-connect-disconnect.png", "dark-1400-02-topics-all-messages.png", "dark-1400-01-disconnected-panel.png", 1600, 820,
  [(152,408,"Connect/Disconnect button"),(212,480,"toolbar top"),(256,524,"work top / tree filter"),(277,545,"detail title")]),
 ("pair-1000-connect-disconnect.png", "light-1000-02-topics-all-messages.png", "light-1000-01-disconnected-panel.png", 1200, 820,
  [(152,408,"Connect/Disconnect button"),(212,480,"toolbar top"),(256,524,"work top / tree filter"),(277,545,"detail title")]),
 ("pair-1400-banner.png", "dark-1400-03-topic-details-leaf.png", "dark-1400-04-alert-banner.png", 1600, 760,
  [(212,256,"toolbar top"),(256,300,"work top / tree filter"),(277,321,"detail title"),(630,674,"tree row 'demo'")]),
 ("pair-1000-banner.png", "light-1000-03-topic-details-leaf.png", "light-1000-04-alert-banner.png", 1200, 760,
  [(212,256,"toolbar top"),(256,300,"work top / tree filter"),(277,321,"detail title"),(630,674,"tree row 'demo'")]),
]
cols = [(255,80,80),(255,170,0),(60,200,90),(200,90,255)]
for out, b, a, cw, ch, marks in pairs:
    ib = Image.open(os.path.join(A,b)).convert('RGB').crop((0,0,cw,ch))
    ia = Image.open(os.path.join(A,a)).convert('RGB').crop((0,0,cw,ch))
    canvas = Image.new('RGB',(cw*2+20,ch+40),(128,128,128))
    canvas.paste(ib,(0,40)); canvas.paste(ia,(cw+20,40))
    d = ImageDraw.Draw(canvas)
    d.text((8,10),"BEFORE: "+b,fill=(0,0,0)); d.text((cw+28,10),"AFTER: "+a,fill=(0,0,0))
    for (yb,ya,lab),c in zip(marks,cols):
        d.line([(0,yb+40),(cw,yb+40)],fill=c,width=3)
        d.line([(cw+20,ya+40),(2*cw+20,ya+40)],fill=c,width=3)
        d.line([(cw,yb+40),(cw+20,ya+40)],fill=c,width=3)
        d.text((cw-300,yb+44),f"{lab} y={yb}",fill=c); d.text((2*cw-280,ya+44),f"{lab} y={ya} (+{(ya-yb)//2} pt)",fill=c)
    canvas.save(os.path.join(os.path.dirname(__file__),out))
    print(out)
