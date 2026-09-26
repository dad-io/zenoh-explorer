import sys
W,H=2800,1856
d=open(sys.argv[1],'rb').read()
def px(x,y): i=(y*W+x)*3; return d[i],d[i+1],d[i+2]
# region of tree crop: x 20..820, y 600..1020
def bbox(pred,x0,y0,x1,y1):
    xs=[];ys=[]
    for y in range(y0,y1):
        for x in range(x0,x1):
            if pred(*px(x,y)): xs.append(x);ys.append(y)
    return (min(xs),min(ys),max(xs),max(ys)) if xs else None
blue=lambda r,g,b: b>200 and r<60 and g<150
print('save btn humidity', bbox(blue,270,910,340,970))
print('save btn temp1', bbox(blue,270,960,340,1000))
# selection outline around report: bluish stroke
out=lambda r,g,b: b>180 and r<120 and g>100
print('report outline', bbox(out,60,730,220,800))
# selection fill color sample inside report box
print('report fill', px(110,752), 'bg', px(400,700))
print('bar fill', px(260,768), 'bar track', px(420,768))
