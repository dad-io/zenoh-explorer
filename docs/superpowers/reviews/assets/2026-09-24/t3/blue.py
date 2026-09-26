import sys
from PIL import Image
import numpy as np
from scipy import ndimage
p=sys.argv[1]; c=np.array(list(map(int,sys.argv[2].split(','))))
y0,y1=int(sys.argv[3]),int(sys.argv[4])
a=np.asarray(Image.open(p).convert('RGB')).astype(int)[y0:y1,:600]
m=np.abs(a-c).sum(2)<=12
lab,n=ndimage.label(m)
for sl in ndimage.find_objects(lab):
    h=sl[0].stop-sl[0].start; w=sl[1].stop-sl[1].start
    if h>20 and w>60: print(p,'x',sl[1].start,sl[1].stop,'y',sl[0].start+y0,sl[0].stop+y0)
