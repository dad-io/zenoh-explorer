import Cocoa
import ApplicationServices
let args = CommandLine.arguments
func windows(_ owner: String) -> [[String: Any]] {
  let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as! [[String: Any]]
  return list.filter { ($0[kCGWindowOwnerName as String] as? String ?? "").contains(owner) && ($0[kCGWindowLayer as String] as? Int ?? 1) == 0 }
}
switch args[1] {
case "activate":
  NSRunningApplication(processIdentifier: pid_t(Int32(args[2])!))?.activate(options: [.activateIgnoringOtherApps]); usleep(250000)
case "trusted": print(AXIsProcessTrusted())
case "find":
  for w in windows(args[2]) { let b = w[kCGWindowBounds as String] as! [String: Any]; print(w[kCGWindowNumber as String]!, Int(b["X"] as! Double), Int(b["Y"] as! Double), Int(b["Width"] as! Double), Int(b["Height"] as! Double)) }
case "click":
  let p = CGPoint(x: Double(args[2])!, y: Double(args[3])!)
  for t in [CGEventType.leftMouseDown, .leftMouseUp] { CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap); usleep(40000) }
case "resize":
  // args: pid w h
  let app = AXUIElementCreateApplication(pid_t(Int32(args[2])!))
  var v: CFTypeRef?; AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &v)
  guard let wins = v as? [AXUIElement], let w = wins.first else { print("no AX window"); exit(1) }
  var size = CGSize(width: Double(args[3])!, height: Double(args[4])!)
  let sv = AXValueCreate(.cgSize, &size)!
  print(AXUIElementSetAttributeValue(w, kAXSizeAttribute as CFString, sv).rawValue)
case "type":
  for ch in args[2].utf16 { var c = ch; for down in [true,false] { let e = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down); e?.keyboardSetUnicodeString(stringLength: 1, unicodeString: &c); e?.post(tap: .cghidEventTap) }; usleep(15000) }
case "key":
  let code = CGKeyCode(Int(args[2])!); var flags: CGEventFlags = []
  if args.count > 3 && args[3].contains("cmd") { flags.insert(.maskCommand) }
  if args.count > 3 && args[3].contains("shift") { flags.insert(.maskShift) }
  for down in [true,false] { let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down); e?.flags = flags; e?.post(tap: .cghidEventTap); usleep(30000) }
default: print("?")
}
