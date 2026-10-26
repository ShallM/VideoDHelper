import Foundation
import Darwin

public struct SubtitleTrack: Identifiable, Equatable, Sendable {
    public let language: String
    public let format: SubtitleFormat
    public let url: URL?
    public let inlineText: String?
    public let isAutomatic: Bool
    public var id: String { "\(language)-\(format.rawValue)-\(isAutomatic)-\(url?.absoluteString ?? "inline")" }
    public var label: String { "\(language) · \(isAutomatic ? "平台自动字幕" : "平台字幕") · \(format.rawValue.uppercased())" }
}

public struct VideoMetadata: Sendable {
    public let title: String
    public let author: String
    public let duration: Double?
    public let url: URL
    public let canonicalURL: URL
    public let tracks: [SubtitleTrack]
    public let formats: [MediaFormat]

    public static func decode(_ data: Data, reference: VideoReference) throws -> VideoMetadata {
        struct Payload: Decodable {
            struct Track: Decodable { let ext: String?; let url: String?; let data: String? }
            let _type: String?
            let title: String?
            let uploader: String?
            let duration: Double?
            let webpage_url: String?
            let subtitles: [String: [Track]]?
            let automatic_captions: [String: [Track]]?
            let formats: [MediaFormat]?
        }
        let payload: Payload
        do { payload = try JSONDecoder().decode(Payload.self, from: data) }
        catch { throw VideoError.toolFailed("下载工具返回了无法识别的视频信息。") }
        guard payload._type != "playlist", payload._type != "multi_video" else { throw VideoError.unsupportedPlaylist }
        guard let title = payload.title, !title.isEmpty else { throw VideoError.toolFailed("未获得视频标题。") }
        var tracks: [SubtitleTrack] = []
        for (dictionary, automatic) in [(payload.subtitles, false), (payload.automatic_captions, true)] {
            for (language, entries) in dictionary ?? [:] {
                for entry in entries {
                    guard let ext = entry.ext, let format = SubtitleFormat(rawValue: ext) else { continue }
                    if let text = entry.data, !text.isEmpty {
                        tracks.append(SubtitleTrack(language: language, format: format, url: nil, inlineText: text, isAutomatic: automatic))
                    } else if let string = entry.url, let url = URL(string: string), isAllowedSubtitleURL(url) {
                        tracks.append(SubtitleTrack(language: language, format: format, url: url, inlineText: nil, isAutomatic: automatic))
                    }
                }
            }
        }
        func rank(_ track: SubtitleTrack) -> Int {
            let chinese = track.language.lowercased().contains("zh") || track.language.lowercased() == "cn"
            return (chinese ? 0 : 10) + (track.isAutomatic ? 2 : 0) + (track.format == .bilibiliJSON ? 0 : 1)
        }
        tracks.sort { rank($0) == rank($1) ? $0.id < $1.id : rank($0) < rank($1) }
        let canonical = payload.webpage_url.flatMap { try? VideoReference.parse($0).url } ?? reference.url
        return VideoMetadata(title: title, author: payload.uploader ?? "未知作者", duration: payload.duration,
                             url: reference.url, canonicalURL: canonical, tracks: tracks, formats: (payload.formats ?? []).filter { $0.isUsable })
    }
}

private func isAllowedSubtitleURL(_ url: URL) -> Bool {
    guard ["http", "https"].contains(url.scheme?.lowercased() ?? ""),
          url.user == nil, url.password == nil, url.port == nil,
          let host = url.host?.lowercased() else { return false }
    return ["bilibili.com", "hdslb.com"].contains { host == $0 || host.hasSuffix("." + $0) }
}

private final class SubtitleRedirectPolicy: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(request.url.map(isAllowedSubtitleURL) == true ? request : nil)
    }
}

public enum BilibiliProbe {
    public static func inspect(_ reference: VideoReference, cookieFile: URL? = nil) async throws -> VideoMetadata {
        let bundled = Bundle.main.resourceURL?.appendingPathComponent("yt-dlp").path
        let projectTool = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent(".venv/bin/yt-dlp").path
        let candidates = [bundled, projectTool, "/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"].compactMap { $0 }
        guard let path = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            throw VideoError.toolUnavailable
        }
        var arguments = [
            "--ignore-config", "--no-warnings", "--no-playlist", "--simulate", "--write-subs", "--write-auto-subs", "--ignore-no-formats-error",
            "--socket-timeout", "15", "--retries", "1", "--dump-single-json"
        ]
        if let cookieFile, FileManager.default.fileExists(atPath: cookieFile.path) {
            arguments += ["--cookies", cookieFile.path]
        }
        arguments += ["--", reference.url.absoluteString]
        let output = try await ProcessRunner.run(path: path, arguments: arguments, processGroup: true)
        return try VideoMetadata.decode(output, reference: reference)
    }

    public static func load(_ track: SubtitleTrack, referer: URL) async throws -> [TranscriptSegment] {
        if let text = track.inlineText {
            guard text.utf8.count <= 10 * 1024 * 1024 else { throw VideoError.invalidSubtitle("字幕文件超过 10 MB。") }
            return try TranscriptParser.parse(Data(text.utf8), format: track.format)
        }
        guard let url = track.url, isAllowedSubtitleURL(url) else { throw VideoError.invalidSubtitle("字幕地址无效。") }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 45
        configuration.httpCookieStorage = nil
        let session = URLSession(configuration: configuration, delegate: SubtitleRedirectPolicy(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        var request = URLRequest(url: url)
        request.setValue(referer.absoluteString, forHTTPHeaderField: "Referer")
        let (bytes, response) = try await session.bytes(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw VideoError.invalidSubtitle("字幕服务器未返回成功响应，请重新解析视频链接后重试。")
        }
        var data = Data()
        for try await byte in bytes {
            guard data.count < 10 * 1024 * 1024 else { throw VideoError.invalidSubtitle("字幕文件超过 10 MB。") }
            data.append(byte)
        }
        return try TranscriptParser.parse(data, format: track.format)
    }
}

enum ProcessRunner {
    static func run(path: String, arguments: [String], timeoutSeconds: Double = 45,
                    processGroup: Bool = false, registry: ProcessRegistry = .shared,
                    onOutput: (@Sendable (Data) -> Void)? = nil) async throws -> Data {
        try Task.checkCancellation()
        let manager = FileManager.default
        let directory = manager.temporaryDirectory.appendingPathComponent("VideoDHelper-\(UUID().uuidString)", isDirectory: true)
        try manager.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        defer { try? manager.removeItem(at: directory) }
        let stdoutURL = directory.appendingPathComponent("stdout")
        let stderrURL = directory.appendingPathComponent("stderr")
        manager.createFile(atPath: stdoutURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
        manager.createFile(atPath: stderrURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
        let stdout = try FileHandle(forWritingTo: stdoutURL)
        let stderr = try FileHandle(forWritingTo: stderrURL)
        defer { try? stdout.close(); try? stderr.close() }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        process.standardOutput = stdout
        process.standardError = stderr
        process.standardInput = FileHandle.nullDevice
        defer { registry.remove(process); process.terminationHandler = nil }
        @Sendable func signalProcess(_ signal: Int32) {
            guard process.isRunning else { return }
            if processGroup { kill(-process.processIdentifier, signal) }
            kill(process.processIdentifier, signal)
        }
        let reader = try FileHandle(forReadingFrom: stdoutURL)
        defer { try? reader.close() }
        let observer = Task {
            while !Task.isCancelled {
                if let data = try? reader.readToEnd(), !data.isEmpty { onOutput?(data) }
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
        }
        defer { observer.cancel() }
        // A cancelled DispatchWorkItem still retains its captures until its
        // deadline (up to four hours). A cancellable sleep releases them promptly.
        let timeout = Task {
            do {
                try await Task.sleep(nanoseconds: UInt64(timeoutSeconds * 1_000_000_000))
                signalProcess(SIGTERM)
                try await Task.sleep(nanoseconds: 3_000_000_000)
                signalProcess(SIGKILL)
            } catch { }
        }
        defer { timeout.cancel() }
        let status: Int32 = try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { continuation in
                process.terminationHandler = { continuation.resume(returning: $0.terminationStatus) }
                do {
                    try Task.checkCancellation()
                    try registry.launch(process, group: processGroup)
                    if Task.isCancelled { signalProcess(SIGTERM) }
                } catch { continuation.resume(throwing: error) }
            }
        }, onCancel: {
            if process.isRunning {
                signalProcess(SIGTERM)
                DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
                    signalProcess(SIGKILL)
                }
            }
        })
        observer.cancel()
        await observer.value
        try Task.checkCancellation()
        guard status == 0 else {
            let diagnostic = (try? String(contentsOf: stderrURL, encoding: .utf8)) ?? ""
            // URLs can contain session parameters. Display only redacted, bounded diagnostics.
            let redacted = diagnostic.replacingOccurrences(of: #"https?://\S+"#, with: "[地址]", options: .regularExpression)
            let reason = redacted.trimmingCharacters(in: .whitespacesAndNewlines)
            throw VideoError.toolFailed(reason.isEmpty ? "请求中断或超时，请重试。" : String(reason.suffix(1200)))
        }
        return try Data(contentsOf: stdoutURL)
    }
}
