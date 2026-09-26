import sys, glob, os, json
from PIL import Image
import numpy as np
out=[]
for p in sorted(glob.glob('*.png')):
    a=np.asarray(Image.open(p).convert('RGB')).astype(int)
    H,W,_=a.shape
    dark=p.startswith('dark')
    panel=np.array([75,75,75] if dark else [255,255,255]); bg=np.array([45,45,45] if dark else [248,248,248])
    x0,x1=16,W-16
    seg=a[:,x0:x1]
    fp=(np.abs(seg-panel).sum(2)<=3).mean(1)
    fb=(np.abs(seg-bg).sum(2)<=3).mean(1)
    TB=56
    # first panel row after title bar
    P0=next(y for y in range(TB,H) if fp[y]>0.9)
    # bottom: last panel-ish row before bottom bg margin
    Pb=max(y for y in range(P0,H) if fb[y]<0.5)+1
    # uniform non-panel lines in P0..P0+100
    lines=[]
    for y in range(P0,P0+100):
        row=seg[y]; c=row[len(row)//2]
        u=(np.abs(row-c).sum(1)<=6).mean()
        if u>0.97 and np.abs(c-panel).sum()>3:
            if not lines or y>lines[-1][1]+1: lines.append([y,y])
            else: lines[-1][1]=y
    worktop=lines[-1][1]+1
    # separators above P0 (header seps)
    hl=[]
    for y in range(TB,P0):
        row=seg[y]; c=row[len(row)//2]
        u=(np.abs(row-c).sum(1)<=6).mean()
        if u>0.97 and np.abs(c-bg).sum()>3:
            if not hl or y>hl[-1][1]+1: hl.append([y,y])
            else: hl[-1][1]=y
    # vertical split: columns uniform over work rows
    col=a[worktop+20:Pb-4, x0:x1]
    cands=[]
    for x in range(col.shape[1]):
        c=col[:,x]; m=c[len(c)//2]
        u=(np.abs(c-m).sum(1)<=6).mean()
        if u>0.97 and np.abs(m-panel).sum()>3: cands.append(x+x0)
    out.append(dict(file=p,W=W,H=H,P0=P0,bottom=Pb,panel_lines=lines,header_lines=hl,worktop=worktop,vsplit=cands))
    print(p, W,H,'P0',P0,'bot',Pb,'lines',lines,'hdr',hl,'work',worktop,'vsplit',cands)
json.dump(out,open(sys.argv[1],'w'),indent=1)
