import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: r, green: g, blue: b, alpha: a)
}
func render(_ width: Int, _ height: Int, to url: URL, draw: () -> Void) throws {
    let image = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: image)
    draw()
    NSGraphicsContext.restoreGraphicsState()
    try image.representation(using: .png, properties: [:])!.write(to: url)
}
func icon(_ size: Int, to url: URL) throws {
    try render(size, size, to: url) {
        NSGraphicsContext.current!.cgContext.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
        let tile = NSBezierPath(roundedRect: NSRect(x: 82, y: 82, width: 860, height: 860), xRadius: 192, yRadius: 192)
        NSGradient(starting: color(0.06, 0.76, 0.68), ending: color(0.03, 0.34, 0.43))!.draw(in: tile, angle: -60)
        color(1, 1, 1, 0.3).setFill()
        NSBezierPath(roundedRect: NSRect(x: 250, y: 330, width: 566, height: 440), xRadius: 60, yRadius: 60).fill()
        color(0.97, 1, 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: 208, y: 260, width: 566, height: 440), xRadius: 60, yRadius: 60).fill()
        color(0.05, 0.55, 0.55).setFill()
        let play = NSBezierPath()
        play.move(to: NSPoint(x: 416, y: 444)); play.line(to: NSPoint(x: 416, y: 616)); play.line(to: NSPoint(x: 574, y: 530)); play.close(); play.fill()
        for (y, width) in [(370.0, 330.0), (322.0, 218.0)] {
            color(0.12, 0.49, 0.53, 0.6).setFill()
            NSBezierPath(roundedRect: NSRect(x: 326, y: y, width: width, height: 17), xRadius: 8, yRadius: 8).fill()
        }
    }
}
let iconset = output.appendingPathComponent("AppIcon.iconset", isDirectory: true)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    try icon(size, to: iconset.appendingPathComponent("icon_\(size)x\(size).png"))
    try icon(size * 2, to: iconset.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
}
func text(_ value: String, x: CGFloat, y: CGFloat, size: CGFloat, weight: NSFont.Weight = .regular, tint: NSColor = color(0.12, 0.21, 0.26)) {
    (value as NSString).draw(at: NSPoint(x: x, y: y), withAttributes: [.font: NSFont.systemFont(ofSize: size, weight: weight), .foregroundColor: tint])
}
try render(760, 470, to: output.appendingPathComponent("dmg-background.png")) {
    color(0.965, 0.985, 0.981).setFill(); NSRect(x: 0, y: 0, width: 760, height: 470).fill()
    text("VideoDHelper", x: 48, y: 402, size: 29, weight: .semibold)
    text("将应用拖入右侧「应用程序」，即可完成安装", x: 48, y: 368, size: 15, tint: color(0.35, 0.43, 0.46))
    for x in [CGFloat(190), CGFloat(570)] {
        color(0.85, 0.94, 0.92).setFill(); NSBezierPath(ovalIn: NSRect(x: x - 82, y: 170, width: 164, height: 164)).fill()
    }
    color(0.15, 0.57, 0.54).setStroke()
    let arrow = NSBezierPath(); arrow.lineWidth = 3; arrow.lineCapStyle = .round
    arrow.move(to: NSPoint(x: 329, y: 251)); arrow.line(to: NSPoint(x: 427, y: 251))
    arrow.move(to: NSPoint(x: 414, y: 264)); arrow.line(to: NSPoint(x: 427, y: 251)); arrow.line(to: NSPoint(x: 414, y: 238)); arrow.stroke()
    // Finder may keep the user's path/status bars visible despite disk settings.
    // Keep both instructions inside the smaller available content area.
    text("① 拖入应用程序      ② 推出此磁盘      ③ 从应用程序打开", x: 151, y: 124, size: 13)
    text("Apple Silicon · macOS 14+  |  本版未公证，首次打开方法请见发布说明", x: 130, y: 96, size: 11, tint: color(0.40, 0.46, 0.48))
}
