// T12 overlay tool: draws review annotations onto a copy of a T1 capture.
// usage: swift annotate.swift <spec.json> [more specs...]
// Coordinates in every spec are pixels of the SOURCE capture (2x Retina).
// Optional "crop":[x0,y0,x1,y1] + "scale":s renders a magnified crop; ops keep source coordinates.
// ops: shade{r,c} box{r,c,w,dash} path{p,c,w,dash} x{at,s,c,w} label{at,t,c,bg,size}
import Foundation; import CoreGraphics; import ImageIO; import CoreText; import UniformTypeIdentifiers

func color(_ s: String) -> CGColor {
  var h = s.hasPrefix("#") ? String(s.dropFirst()) : s
  if h.count == 6 { h += "ff" }
  let v = UInt64(h, radix: 16)!
  return CGColor(srgbRed: CGFloat((v >> 24) & 255)/255, green: CGFloat((v >> 16) & 255)/255,
                 blue: CGFloat((v >> 8) & 255)/255, alpha: CGFloat(v & 255)/255)
}
func nums(_ a: Any?) -> [CGFloat] { ((a as? [Any]) ?? []).map { CGFloat(($0 as! NSNumber).doubleValue) } }

let base = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
for specPath in CommandLine.arguments.dropFirst() {
  let specURL = URL(fileURLWithPath: specPath)
  let spec = try! JSONSerialization.jsonObject(with: Data(contentsOf: specURL)) as! [String: Any]
  let dir = specURL.deletingLastPathComponent()
  let srcURL = dir.appendingPathComponent(spec["src"] as! String)
  let img = CGImageSourceCreateImageAtIndex(CGImageSourceCreateWithURL(srcURL as CFURL, nil)!, 0, nil)!
  let crop = nums(spec["crop"]); let s = CGFloat((spec["scale"] as? NSNumber)?.doubleValue ?? 1)
  let (ox, oy, cw, ch): (CGFloat, CGFloat, CGFloat, CGFloat) = crop.count == 4
    ? (crop[0], crop[1], crop[2]-crop[0], crop[3]-crop[1]) : (0, 0, CGFloat(img.width), CGFloat(img.height))
  let W = Int(cw*s), H = Int(ch*s)
  let ctx = CGContext(data: nil, width: W, height: H, bitsPerComponent: 8, bytesPerRow: 0,
                      space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
  // flip to top-left origin, map source px -> canvas
  ctx.translateBy(x: 0, y: CGFloat(H)); ctx.scaleBy(x: 1, y: -1)
  ctx.interpolationQuality = s > 1 ? .none : .high
  ctx.saveGState()
  ctx.translateBy(x: 0, y: CGFloat(H)); ctx.scaleBy(x: 1, y: -1)   // CGImage draws bottom-up
  ctx.draw(img, in: CGRect(x: -ox*s, y: CGFloat(H) - (CGFloat(img.height) - oy)*s, width: CGFloat(img.width)*s, height: CGFloat(img.height)*s))
  ctx.restoreGState()
  ctx.scaleBy(x: s, y: s); ctx.translateBy(x: -ox, y: -oy)
  for case let op as [String: Any] in (spec["ops"] as! [Any]) {
    let c = color(op["c"] as? String ?? "#ff0000")
    let w = CGFloat((op["w"] as? NSNumber)?.doubleValue ?? 6) / s * (s > 1 ? 2 : 1)
    ctx.setLineDash(phase: 0, lengths: nums(op["dash"]).map { $0 / s * (s > 1 ? 2 : 1) })
    ctx.setLineCap(.round); ctx.setLineJoin(.round)
    switch op["op"] as! String {
    case "shade":
      let r = nums(op["r"]); ctx.setFillColor(c); ctx.fill(CGRect(x: r[0], y: r[1], width: r[2]-r[0], height: r[3]-r[1]))
    case "box":
      let r = nums(op["r"]); ctx.setStrokeColor(c); ctx.setLineWidth(w)
      ctx.stroke(CGRect(x: r[0], y: r[1], width: r[2]-r[0], height: r[3]-r[1]))
    case "path":
      let p = (op["p"] as! [Any]).map { nums($0) }
      ctx.setStrokeColor(c); ctx.setLineWidth(w); ctx.beginPath()
      ctx.move(to: CGPoint(x: p[0][0], y: p[0][1])); for q in p.dropFirst() { ctx.addLine(to: CGPoint(x: q[0], y: q[1])) }
      ctx.strokePath()
      if op["arrow"] as? Bool == true, p.count >= 2 {
        let a = p[p.count-2], b = p[p.count-1]; let ang = atan2(b[1]-a[1], b[0]-a[0]); let L = 26 / s
        ctx.setLineDash(phase: 0, lengths: []); ctx.beginPath()
        ctx.move(to: CGPoint(x: b[0] - L*cos(ang-0.45), y: b[1] - L*sin(ang-0.45)))
        ctx.addLine(to: CGPoint(x: b[0], y: b[1]))
        ctx.addLine(to: CGPoint(x: b[0] - L*cos(ang+0.45), y: b[1] - L*sin(ang+0.45))); ctx.strokePath()
      }
    case "x":
      let a = nums(op["at"]); let r = CGFloat((op["s"] as? NSNumber)?.doubleValue ?? 22) / s * (s > 1 ? 2 : 1)
      ctx.setStrokeColor(c); ctx.setLineWidth(w); ctx.setLineDash(phase: 0, lengths: [])
      ctx.beginPath(); ctx.move(to: CGPoint(x: a[0]-r, y: a[1]-r)); ctx.addLine(to: CGPoint(x: a[0]+r, y: a[1]+r))
      ctx.move(to: CGPoint(x: a[0]+r, y: a[1]-r)); ctx.addLine(to: CGPoint(x: a[0]-r, y: a[1]+r)); ctx.strokePath()
    case "label":
      let a = nums(op["at"]); let size = CGFloat((op["size"] as? NSNumber)?.doubleValue ?? 26) / s * (s > 1 ? 2 : 1)
      let font = CTFontCreateWithName("Helvetica-Bold" as CFString, size, nil)
      let lines = (op["t"] as! String).components(separatedBy: "\n")
      let attrs: [NSAttributedString.Key: Any] = [.init(kCTFontAttributeName as String): font, .init(kCTForegroundColorAttributeName as String): c]
      let cts = lines.map { CTLineCreateWithAttributedString(NSAttributedString(string: $0, attributes: attrs)) }
      let lh = size * 1.25; let pad = size * 0.35
      let tw = cts.map { CGFloat(CTLineGetTypographicBounds($0, nil, nil, nil)) }.max()!
      ctx.setFillColor(color(op["bg"] as? String ?? "#000000d9"))
      ctx.fill(CGRect(x: a[0], y: a[1], width: tw + 2*pad, height: lh*CGFloat(lines.count) + 2*pad))
      ctx.saveGState(); ctx.textMatrix = CGAffineTransform(scaleX: 1, y: -1)
      for (i, l) in cts.enumerated() {
        ctx.textPosition = CGPoint(x: a[0] + pad, y: a[1] + pad + lh*CGFloat(i) + size*0.95)
        CTLineDraw(l, ctx)
      }
      ctx.restoreGState()
    default: break
    }
  }
  let out = dir.appendingPathComponent(spec["out"] as! String)
  let dest = CGImageDestinationCreateWithURL(out as CFURL, UTType.png.identifier as CFString, 1, nil)!
  CGImageDestinationAddImage(dest, ctx.makeImage()!, nil); CGImageDestinationFinalize(dest)
  print("wrote \(out.lastPathComponent) \(W)x\(H)")
  _ = base
}
