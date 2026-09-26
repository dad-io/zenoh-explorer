from PIL import Image
from collections import Counter
def s(f,box,label,n=5):
    im=Image.open(f).convert('RGB')
    x0,y0,x1,y1=[int(v*1.4) for v in box]
    c=Counter(im.crop((x0,y0,x1,y1)).getdata())
    print(f"{f} {label} pts{box}: "+", ".join(f"{k}x{v}" for k,v in c.most_common(n)))
for t in ['light','dark']:
    f=f'{t}-1400-03-topic-details-leaf.png'
    s(f,(12,110,110,132),'Disconnect button')
    s(f,(56,686,144,712),'selected temp1 row')
    s(f,(155,538,255,558),'progress fill')
    s(f,(783,232,862,252),'Pause button')
    s(f,(594,446,766,514),'history group box')
    s(f,(300,80,900,95),'header bg')
    s(f,(700,600,1900,1000),'detail bg')
    s(f,(100,1000,500,1200),'tree bg')
    s(f,(1540,62,1740,80),'memory text')
    s(f,(720,458,757,478),'SUB badge')
    s(f,(594,340,634,360),'code bg')
    s(f,(100,292,500,316),'text edit')
    s(f,(596,255,1970,268),'separator band')
