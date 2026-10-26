import Foundation

public struct VideoReference: Equatable, Sendable {
    public let url: URL

    public static func parse(_ input: String) throws -> VideoReference {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.lowercased().hasPrefix("https://") && !text.lowercased().hasPrefix("http://") {
            let detector = try NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)
            let matches = detector.matches(in: text, range: NSRange(text.startIndex..., in: text))
            guard matches.count == 1, let url = matches.first?.url else { throw VideoError.invalidLink }
            return try parseURL(url.absoluteString)
        }
        return try parseURL(text)
    }

    private static func parseURL(_ text: String) throws -> VideoReference {
        guard var components = URLComponents(string: text),
              components.scheme?.lowercased() == "https" || components.scheme?.lowercased() == "http",
              components.user == nil, components.password == nil,
              components.port == nil else {
            throw VideoError.invalidLink
        }
        let host = components.host?.lowercased() ?? ""
        if host == "b23.tv" {
            guard components.path.range(of: #"^/[A-Za-z0-9]+/?$"#, options: .regularExpression) != nil else {
                throw VideoError.invalidLink
            }
            components.host = "b23.tv"
            components.query = nil
        } else {
            guard ["www.bilibili.com", "bilibili.com", "m.bilibili.com"].contains(host),
                  components.path.range(of: #"^/video/(BV[A-Za-z0-9]{10}|av[0-9]+)/?$"#, options: .regularExpression) != nil else {
                throw VideoError.invalidLink
            }
            let part = components.queryItems?.first(where: { $0.name == "p" })?.value
            if let part {
                guard let number = Int(part), number > 0 else { throw VideoError.invalidLink }
                components.queryItems = [URLQueryItem(name: "p", value: String(number))]
            } else {
                components.query = nil
            }
            components.host = "www.bilibili.com"
        }
        components.scheme = "https"
        components.fragment = nil
        guard let url = components.url else { throw VideoError.invalidLink }
        return VideoReference(url: url)
    }
}

public enum VideoError: LocalizedError {
    case invalidLink
    case invalidSubtitle(String)
    case emptySubtitle
    case invalidBudget
    case toolUnavailable
    case toolFailed(String)
    case unsupportedPlaylist

    public var errorDescription: String? {
        switch self {
        case .invalidLink:
            return "请输入 B 站视频页链接或 b23.tv 短链接；第一步暂不接受合集或个人空间链接。"
        case .invalidSubtitle(let reason): return "字幕读取失败：\(reason)"
        case .emptySubtitle: return "文件中没有可用的字幕文本。"
        case .invalidBudget: return "分块预算过小，无法完整保存一个文字字符。"
        case .toolUnavailable: return "没有找到 yt-dlp。当前开发预览需要本机已安装 yt-dlp。"
        case .toolFailed(let reason): return reason
        case .unsupportedPlaylist: return "这个链接返回了多个视频，请粘贴指定分 P 的视频链接（包含 p=编号）。"
        }
    }
}
