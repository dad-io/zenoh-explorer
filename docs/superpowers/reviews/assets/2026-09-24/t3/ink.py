import sys
from PIL import Image
import numpy as np
p,axis,a0,a1,b0,b1=sys.argv[1],sys.argv[2],*map(int,sys.argv[3:7])
a=np.asarray(Image.open(p).convert('RGB')).astype(int)
dark=p.startswith('dark')
panel=np.array([75,75,75] if dark else [255,255,255]); bg=np.array([45,45,45] if dark else [248,248,248])
# axis=rows: scan rows a0..a1 within columns b0..b1 ; axis=cols: scan cols a0..a1 within rows b0..b1
if axis=='rows': reg=a[a0:a1,b0:b1]; 
else: reg=a[b0:b1,a0:a1].transpose(1,0,2)
d=np.minimum(np.abs(reg-panel).sum(2),np.abs(reg-bg).sum(2))
ink=(d>60).any(1)
runs=[];s=None
for i,v in enumerate(ink):
    if v and s is None: s=i
    if not v and s is not None: runs.append((s+a0,i-1+a0)); s=None
if s is not None: runs.append((s+a0,len(ink)-1+a0))
print(p, runs)
