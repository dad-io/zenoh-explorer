import Foundation
import CoreGraphics
import ImageIO
// usage: pdiff a.png b.png  -> prints "x y w h npix" of differing pixels (pixel coords), or "none"
func load(_ p: String) -> (Int, Int, [UInt8]) {
  let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: p) as CFURL, nil)!
  let img = CGImageSourceCreateImageAtIndex(src, 0, nil)!
  let w = img.width, h = img.height
  var buf = [UInt8](repeating: 0, count: w*h*4)
  let ctx = CGContext(data: &buf, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w*4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
  ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
  return (w, h, buf)
}
let a = load(CommandLine.arguments[1]), b = load(CommandLine.arguments[2])
guard a.0 == b.0 && a.1 == b.1 else { print("size-mismatch"); exit(0) }
var x0 = Int.max, y0 = Int.max, x1 = -1, y1 = -1, n = 0
for y in 0..<a.1 { for x in 0..<a.0 { let i = (y*a.0+x)*4
  if abs(Int(a.2[i])-Int(b.2[i])) + abs(Int(a.2[i+1])-Int(b.2[i+1])) + abs(Int(a.2[i+2])-Int(b.2[i+2])) > 24 { n += 1; x0 = min(x0,x); y0 = min(y0,y); x1 = max(x1,x); y1 = max(y1,y) } } }
if n == 0 { print("none") } else { print(x0, y0, x1-x0+1, y1-y0+1, n) }
