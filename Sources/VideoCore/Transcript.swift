import Foundation

public struct TranscriptSegment: Codable, Identifiable, Equatable, Sendable {
    public let id: Int
    public let start: Double
    public let end: Double
    public let text: String

    public init(id: Int, start: Double, end: Double, text: String) {
        self.id = id
        self.start = start
        self.end = end
        self.text = text
    }
}

public enum SubtitleFormat: String, CaseIterable, Sendable {
    case bilibiliJSON = "json"
    case srt
    case vtt
}

public enum TranscriptParser {
    public static func parse(_ data: Data, format: SubtitleFormat) throws -> [TranscriptSegment] {
        let segments: [TranscriptSegment]
        if format == .bilibiliJSON {
            struct Payload: Decodable {
                struct Cue: Decodable { let from: Double; let to: Double; let content: String }
                let body: [Cue]
            }
            guard let payload = try? JSONDecoder().decode(Payload.self, from: data) else {
                throw VideoError.invalidSubtitle("JSON 必须包含 B 站字幕格式的 body / from / to / content 字段。")
            }
            segments = payload.body.enumerated().map {
                TranscriptSegment(id: $0.offset, start: $0.element.from, end: $0.element.to,
                                  text: $0.element.content.trimmingCharacters(in: .whitespacesAndNewlines))
            }
        } else {
            guard let source = String(data: data, encoding: .utf8) else {
                throw VideoError.invalidSubtitle("目前支持 UTF-8 编码的字幕。")
            }
            segments = try parseTimedText(source, format: format)
        }
        let usable = segments.filter { !$0.text.isEmpty }
        guard !usable.isEmpty else { throw VideoError.emptySubtitle }
        guard usable.allSatisfy({ $0.start.isFinite && $0.end.isFinite && $0.start >= 0 && $0.end >= $0.start }) else {
            throw VideoError.invalidSubtitle("发现无效的起止时间。")
        }
        return usable.sorted { $0.start == $1.start ? $0.id < $1.id : $0.start < $1.start }
            .enumerated().map { TranscriptSegment(id: $0.offset, start: $0.element.start, end: $0.element.end, text: $0.element.text) }
    }

    private static func parseTimedText(_ source: String, format: SubtitleFormat) throws -> [TranscriptSegment] {
        let normalized = source.replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n").replacingOccurrences(of: "\u{FEFF}", with: "")
        var blocks: [[String]] = []
        var current: [String] = []
        for line in normalized.components(separatedBy: "\n") {
            if line.trimmingCharacters(in: .whitespaces).isEmpty {
                if !current.isEmpty { blocks.append(current); current = [] }
            } else { current.append(line) }
        }
        if !current.isEmpty { blocks.append(current) }
        var segments: [TranscriptSegment] = []
        for block in blocks {
            let first = block[0].trimmingCharacters(in: .whitespaces)
            if format == .vtt && (first == "WEBVTT" || first.hasPrefix("WEBVTT ") || first == "STYLE" || first == "REGION" || first == "NOTE" || first.hasPrefix("NOTE ")) { continue }
            guard let timeIndex = block.firstIndex(where: { $0.contains("-->") }) else {
                throw VideoError.invalidSubtitle("有字幕段缺少时间戳。")
            }
            let times = block[timeIndex].components(separatedBy: "-->")
            guard times.count == 2,
                  let start = parseTime(times[0].trimmingCharacters(in: .whitespaces)),
                  let endText = times[1].split(whereSeparator: { $0.isWhitespace }).first,
                  let end = parseTime(String(endText)) else {
                throw VideoError.invalidSubtitle("无法识别时间戳。")
            }
            let text = cleanMarkup(block.dropFirst(timeIndex + 1).joined(separator: "\n"))
            segments.append(TranscriptSegment(id: segments.count, start: start, end: end, text: text))
        }
        return segments
    }

    private static func parseTime(_ text: String) -> Double? {
        guard text.range(of: #"^(?:[0-9]+:)?[0-9]{2}:[0-9]{2}(?:[.,][0-9]{1,3})?$"#, options: .regularExpression) != nil else { return nil }
        let parts = text.replacingOccurrences(of: ",", with: ".").split(separator: ":")
        guard (2...3).contains(parts.count), let seconds = Double(parts.last!), seconds < 60,
              let minutes = Double(parts[parts.count - 2]), minutes < 60 else { return nil }
        let hours = parts.count == 3 ? Double(parts[0]) ?? 0 : 0
        return hours * 3600 + minutes * 60 + seconds
    }

    private static func cleanMarkup(_ text: String) -> String {
        // Remove subtitle presentation tags, preserving literal code such as x < y.
        var value = text.replacingOccurrences(of: #"</?(?:b|i|u|c|v|lang|ruby|rt)(?:[. ][^>]*)?>|<[0-9:.]+>"#, with: "", options: .regularExpression)
        for (entity, literal) in [("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "), ("&quot;", "\""), ("&apos;", "'"), ("&amp;", "&")] {
            value = value.replacingOccurrences(of: entity, with: literal)
        }
        return value.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

public enum TranscriptExport {
    public static func timestamp(_ seconds: Double, milliseconds: Bool = false) -> String {
        guard seconds.isFinite, seconds >= 0, seconds < Double(Int.max / 2000) else { return "--:--:--" }
        let total = Int((max(0, seconds) * 1000).rounded())
        let h = total / 3_600_000
        let m = total / 60_000 % 60
        let s = total / 1000 % 60
        let base = String(format: "%02d:%02d:%02d", h, m, s)
        return milliseconds ? base + String(format: ",%03d", total % 1000) : base
    }

    public static func text(_ segments: [TranscriptSegment]) -> String {
        segments.map { "[\(timestamp($0.start))] \($0.text)" }.joined(separator: "\n\n") + "\n"
    }

    public static func srt(_ segments: [TranscriptSegment]) -> String {
        segments.enumerated().map { index, segment in
            "\(index + 1)\n\(timestamp(segment.start, milliseconds: true)) --> \(timestamp(segment.end, milliseconds: true))\n\(segment.text)"
        }.joined(separator: "\n\n") + "\n"
    }
}
