// Pixel probe: prints colour runs along a row (r y) or column (c x) of a PNG (pixel coords).
// usage: swift probe.swift <png> r|c <coord> [minRun]
import Foundation; import CoreGraphics; import ImageIO
let a = CommandLine.arguments
let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: a[1]) as CFURL, nil)!
let img = CGImageSourceCreateImageAtIndex(src, 0, nil)!
let w = img.width, h = img.height
var buf = [UInt8](repeating: 0, count: w*h*4)
let ctx = CGContext(data: &buf, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w*4, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
func px(_ x: Int, _ y: Int) -> (Int,Int,Int) { let i = (y*w + x)*4; return (Int(buf[i]),Int(buf[i+1]),Int(buf[i+2])) }
let row = a[2] == "r"; let k = Int(a[3])!; let minRun = a.count > 4 ? Int(a[4])! : 1
let n = row ? w : h
var start = 0; var cur = row ? px(0,k) : px(k,0)
for i in 1...n {
  let c = i < n ? (row ? px(i,k) : px(k,i)) : (-1,-1,-1)
  if c != cur { if i - start >= minRun { print("\(start)-\(i-1) (\(i-start)) rgb\(cur)") }; start = i; cur = c }
}
