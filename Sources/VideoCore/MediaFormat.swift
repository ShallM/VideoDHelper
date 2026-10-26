import Foundation

public struct MediaFormat: Decodable, Identifiable, Sendable {
    public let id: String
    public let ext: String?
    public let width: Double?
    public let height: Double?
    public let fps: Double?
    public let vcodec: String?
    public let acodec: String?
    public let tbr: Double?
    public let filesize: Double?
    public let estimatedSize: Double?
    public let dynamicRange: String?
    enum CodingKeys: String, CodingKey {
        case id = "format_id", ext, width, height, fps, vcodec, acodec, tbr, filesize
        case estimatedSize = "filesize_approx", dynamicRange = "dynamic_range"
    }
    public var hasVideo: Bool { vcodec != nil && vcodec != "none" }
    public var hasAudio: Bool { acodec != nil && acodec != "none" }
    public var isUsable: Bool {
        id.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil && (hasVideo || hasAudio)
    }
    public var codecLabel: String {
        let code = vcodec ?? ""
        if code.hasPrefix("avc") { return "H.264" }
        if code.hasPrefix("hvc") || code.hasPrefix("hev") { return "HEVC" }
        if code.hasPrefix("av01") { return "AV1" }
        return hasVideo ? code : ((acodec?.hasPrefix("mp4a") == true) ? "AAC" : acodec ?? "音频")
    }
    public var sizeLabel: String {
        guard let size = filesize ?? estimatedSize, size.isFinite, size >= 0, size < Double(Int64.max) else { return "大小待定" }
        return (filesize == nil ? "约 " : "") + ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file)
    }
    public var label: String {
        func integer(_ value: Double?) -> Int {
            guard let value, value.isFinite, value >= 0, value < 1_000_000 else { return 0 }
            return Int(value.rounded())
        }
        if hasVideo {
            return "\(integer(height))p · \(integer(fps)) fps · \(codecLabel) · \(sizeLabel) · #\(id)"
        }
        return "\(codecLabel) · \(integer(tbr)) kbps · \(sizeLabel) · #\(id)"
    }
}
