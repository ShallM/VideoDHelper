import Foundation

public enum AppStorage {
    public static var root: URL {
        // Isolates packaging/CI smoke tests from a person's real library.
        if let directory = ProcessInfo.processInfo.environment["VIDEOHELPER_DATA_DIRECTORY"], directory.hasPrefix("/") {
            return URL(fileURLWithPath: directory, isDirectory: true)
        }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("VideoDHelper", isDirectory: true)
    }
    public static var jobs: URL { root.appendingPathComponent("tasks", isDirectory: true) }
    public static func jobs(for workspace: WorkspaceKind) -> URL { root.appendingPathComponent(workspace.tasksDirectoryName, isDirectory: true) }

    public static func write<T: Encodable>(_ value: T, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let data = try JSONEncoder().encode(value)
        let temporary = url.deletingLastPathComponent().appendingPathComponent(".writing-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: temporary) }
        guard FileManager.default.createFile(atPath: temporary.path, contents: data, attributes: [.posixPermissions: 0o600]) else {
            throw VideoError.toolFailed("无法保存任务数据。")
        }
        if rename(temporary.path, url.path) != 0 { throw VideoError.toolFailed("无法保存任务数据。") }
    }

    public static func read<T: Decodable>(_ type: T.Type, from url: URL) throws -> T {
        try JSONDecoder().decode(type, from: Data(contentsOf: url))
    }

    public static func history() -> [ProcessingJob] {
        return WorkspaceKind.allCases.flatMap { workspace -> [ProcessingJob] in
            let directories = (try? FileManager.default.contentsOfDirectory(at: jobs(for: workspace), includingPropertiesForKeys: nil)) ?? []
            return directories.filter { UUID(uuidString: $0.lastPathComponent) != nil }
            .compactMap { directory -> ProcessingJob? in
                guard let job = try? read(ProcessingJob.self, from: directory.appendingPathComponent("job.json")), job.id == directory.lastPathComponent, job.workspace == workspace else { return nil }
                return job
            }
        }
            .sorted { $0.updatedAt > $1.updatedAt }
    }

    public static func cleanMedia(for job: ProcessingJob) throws {
        // Job identifiers come from UUIDs, never titles or links.
        guard UUID(uuidString: job.id) != nil else { throw VideoError.toolFailed("无效任务编号。") }
        for name in ["media", "temporary", "media-manifest.json"] {
            let path = job.directory.appendingPathComponent(name)
            if FileManager.default.fileExists(atPath: path.path) { try FileManager.default.removeItem(at: path) }
        }
    }
}

public struct APISettings: Codable, Sendable {
    public var baseURL = "https://api.icompify.com/v1"
    public var model = "deepseek-v4.1-flash"
    public var apiKey = ""
    public var reasoningEffort = "low"
    public init() {}

    public static var developmentEnvironment: URL? {
        if Bundle.main.object(forInfoDictionaryKey: "VideoDHelperDistribution") as? Bool == true { return nil }
        let resource = Bundle.main.resourceURL?.appendingPathComponent("development-env-path.txt")
        if let resource, let path = try? String(contentsOf: resource, encoding: .utf8) {
            return URL(fileURLWithPath: path.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent(".env")
    }

    public static func load() -> APISettings {
        if let value = try? AppStorage.read(APISettings.self, from: AppStorage.root.appendingPathComponent("settings.json")) { return value }
        var value = APISettings()
        if let url = developmentEnvironment, let text = try? String(contentsOf: url, encoding: .utf8) {
            for line in text.split(separator: "\n") {
                let pair = line.split(separator: "=", maxSplits: 1).map(String.init)
                guard pair.count == 2 else { continue }
                let content = pair[1].trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                switch pair[0] {
                case "VIDEOHELPER_BASE_URL": value.baseURL = content
                case "VIDEOHELPER_MODEL": value.model = content
                case "VIDEOHELPER_API_KEY": value.apiKey = content
                default: break
                }
            }
        }
        return value
    }

    public func save() throws {
        guard let url = URL(string: baseURL), url.scheme == "https", url.host != nil, url.user == nil,
              url.password == nil, url.query == nil, url.fragment == nil else {
            throw VideoError.toolFailed("请输入有效的 HTTPS Base URL。")
        }
        try AppStorage.write(self, to: AppStorage.root.appendingPathComponent("settings.json"))
    }
}

public struct ProcessingJob: Codable, Identifiable, Sendable {
    public var id: String
    public var title: String
    public var url: String
    public var source: String
    public var createdAt: Double
    public var updatedAt: Double
    public var state: String
    public var stage: String
    public var message: String
    public var progress: Double?
    public var mediaFiles: [String]
    public var segmentCount: Int?
    public var summaryStrategy: String?
    public var dialogueStrategy: String?
    public var sourceKind: WorkspaceKind?
    public var workspace: WorkspaceKind { sourceKind ?? .bilibili }
    public var directory: URL { AppStorage.jobs(for: workspace).appendingPathComponent(id, isDirectory: true) }
    public init(title: String, url: String, source: String, summaryStrategy: String? = nil, workspace: WorkspaceKind = .bilibili) {
        id = UUID().uuidString; self.title = title; self.url = url; self.source = source
        self.summaryStrategy = summaryStrategy
        self.sourceKind = workspace
        createdAt = Date().timeIntervalSince1970; updatedAt = createdAt
        state = "pending"; stage = "prepare"; message = "等待处理"; mediaFiles = []
    }
    public var summaryStrategyLabel: String {
        if workspace == .recording { return DialogueStrategy.label(for: dialogueStrategy) }
        return summaryStrategy == "structured" ? "全文理解" : "旧版分块"
    }
    public var stateLabel: String {
        switch state {
        case "completed": return "已完成"
        case "transcribed": return "原稿已就绪"
        case "failed": return "待重试"
        case "cancelled": return "已取消"
        case "running": return "处理中 / 可恢复"
        default: return "待处理"
        }
    }
}

public struct ProcessingRequest: Codable, Sendable {
    public var title: String
    public var url: String
    public var source: String
    public var segments: [TranscriptSegment]
    public var useWhisper: Bool
    public var mediaMode: String
    public var keepMedia: Bool
    public var videoFormatID: String
    public var audioFormatID: String
    public var videoIncludesAudio: Bool
    public var container: String
    public var whisperModel: String
    /// New tasks use the generic local speech model ID. Missing values preserve legacy requests.
    public var asrModelID: String?
    public var effectiveASRModelID: String { asrModelID ?? whisperModel }
    public var summaryStrategy: String?
    public var sourceKind: WorkspaceKind?
    public var localAudioFile: String?
    public var recordingAction: String?
    public var diarizationMode: String?
    /// Missing on older recordings: their original reader and checkpoints remain in use.
    public var dialogueStrategy: String?
    public var effectiveDialogueStrategy: String { dialogueStrategy ?? DialogueStrategy.legacy.rawValue }
    public init(title: String, url: String, source: String, segments: [TranscriptSegment], useWhisper: Bool,
                mediaMode: String, keepMedia: Bool, videoFormatID: String, audioFormatID: String, videoIncludesAudio: Bool,
                container: String, whisperModel: String, summaryStrategy: String = "structured") {
        self.title = title; self.url = url; self.source = source; self.segments = segments; self.useWhisper = useWhisper
        self.mediaMode = mediaMode; self.keepMedia = keepMedia; self.videoFormatID = videoFormatID; self.audioFormatID = audioFormatID
        self.videoIncludesAudio = videoIncludesAudio; self.container = container; self.whisperModel = whisperModel
        self.summaryStrategy = summaryStrategy
    }
}

public enum DialogueStrategy: String, CaseIterable, Codable, Sendable {
    case legacy
    case contextV2 = "context-v2"
    case meetingV3 = "meeting-v3"
    case meetingBaseline = "meeting-baseline"

    // The new reader stays opt-in until semantic regression gates are met.
    public static let defaultForNewTasks: DialogueStrategy = .legacy

    public static func label(for value: String?) -> String {
        switch value {
        case nil, Self.legacy.rawValue: return "旧版对话流程"
        case Self.contextV2.rawValue: return "全文对话理解 · 预览"
        case Self.meetingV3.rawValue: return "会议纪要 · 预览"
        case Self.meetingBaseline.rawValue: return "全文纪要 · 对照基线"
        default: return "其他对话流程"
        }
    }
}

public struct DialoguePart: Codable, Sendable {
    public let id: String
    public let speaker: String
    public let text: String
    public let source_ids: [Int]

    public func speakerLabel(names: [String: String]) -> String {
        if speaker == "A" || speaker == "B" { return names[speaker] ?? "说话人 \(speaker)" }
        return "归属未确定"
    }
}

public struct CitedText: Codable, Sendable {
    public let id: String?
    public let source_ids: [Int]
    public let text: String
    public var speaker: String?
    public let parts: [DialoguePart]?
    public let status: String?
    public var continuousReading: Bool? = nil
    public var metadata: MeetingStatementMetadata? = nil

    public func dialogueText(names: [String: String]) -> String {
        if let parts, !parts.isEmpty {
            return parts.enumerated().map { index, part in
                if index > 0, parts[index - 1].speaker == part.speaker { return part.text }
                return "\(index == 0 ? "" : " ")[\(part.speakerLabel(names: names))] \(part.text)"
            }.joined()
        }
        let name: String
        if let speaker, speaker == "A" || speaker == "B" { name = names[speaker] ?? "说话人 \(speaker)" }
        else { name = "归属未确定" }
        return "\(name)：\(text)"
    }
}
public struct MeetingStatementMetadata: Codable, Sendable {
    public let text: String
    public let kind: String
    public let state: String
    public let conditions: [String]
    public let scope: String?
    public let speaker: String
    public let attribution_parts: [Int]?
    public let owner: String?
    public let due: String?
}
public struct StudyChapter: Codable, Sendable {
    public let source_ids: [Int]
    public let title: String
    public let detail: String
    public let claims: [CitedText]?
}
public struct StudyNotes: Codable, Sendable {
    public let overview: String
    public let overviewSourceIDs: [Int]?
    public let chapters: [StudyChapter]
    public let key_points: [CitedText]
    public let uncertainties: [CitedText]
    public let model: String
    public let tokensThisRun: Int
    public let summaryStrategy: String?
    public let summaryRoute: String?
    public let reviewedParagraphs: Int?
    public let paragraphCount: Int?
    public let rereadCount: Int?
    public let recordingRevision: String?
    public var action_items: [CitedText]? = nil
    public var evidenceItemCount: Int? = nil
    public var assessmentCount: Int? = nil
    public var isMeeting: Bool { ["meeting-v3", "meeting-baseline"].contains(summaryStrategy ?? "") }
}

/// One publication generation: notes and cleaned dialogue are always read together.
public struct DialogueResultsBundle: Codable, Sendable {
    public let schemaVersion: Int
    public let generationID: String
    public let notes: StudyNotes
    public let condensed: [CitedText]
    public var strategy: String? = nil
}

public struct RecordingResults: Sendable {
    public let notes: StudyNotes?
    public let condensed: [CitedText]

    public static func load(from directory: URL) throws -> RecordingResults {
        let bundleURL = directory.appendingPathComponent("dialogue-results.json")
        if FileManager.default.fileExists(atPath: bundleURL.path) {
            let bundle = try AppStorage.read(DialogueResultsBundle.self, from: bundleURL)
            guard [1, 2].contains(bundle.schemaVersion), !bundle.generationID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw VideoError.toolFailed("录音成果版本无法读取，已保留原文件。")
            }
            let expected = bundle.schemaVersion == 1 ? DialogueStrategy.contextV2.rawValue : bundle.strategy
            guard bundle.schemaVersion == 1 || (["meeting-v3", "meeting-baseline"].contains(expected ?? "") && bundle.notes.summaryStrategy == expected) else {
                throw VideoError.toolFailed("录音成果中的流程标识不一致，已保留原文件。")
            }
            let requestURL = directory.appendingPathComponent("request.json")
            if FileManager.default.fileExists(atPath: requestURL.path) {
                let request = try AppStorage.read(ProcessingRequest.self, from: requestURL)
                guard request.effectiveDialogueStrategy == expected else {
                    throw VideoError.toolFailed("录音任务流程与成套成果不一致，可能曾被旧版应用更新。已保留原稿和所有成果文件；请从原稿新建对照任务重新整理，不会自动选择或混用两代结果。")
                }
            }
            return RecordingResults(notes: bundle.notes, condensed: bundle.condensed)
        }
        // Older tasks have no bundle. Once a bundle exists, a failed decode must
        // never fall back to individual files that may belong to other generations.
        return RecordingResults(notes: try? AppStorage.read(StudyNotes.self, from: directory.appendingPathComponent("notes.json")),
                                condensed: (try? AppStorage.read([CitedText].self, from: directory.appendingPathComponent("condensed.json"))) ?? [])
    }
}
public struct WorkerEvent: Decodable, Sendable {
    public let stage: String
    public let message: String
    public let progress: Double?
}

private final class EventBuffer: @unchecked Sendable {
    private var data = Data()
    private let lock = NSLock()
    func append(_ chunk: Data, callback: (WorkerEvent) -> Void) {
        lock.lock(); defer { lock.unlock() }
        data.append(chunk)
        while let index = data.firstIndex(of: 10) {
            let line = data.prefix(upTo: index)
            data.removeSubrange(...index)
            if let event = try? JSONDecoder().decode(WorkerEvent.self, from: line) { callback(event) }
        }
    }
}

public enum ProcessingWorker {
    public static func run(job: ProcessingJob, onEvent: @escaping @Sendable (WorkerEvent) -> Void) async throws {
        guard UUID(uuidString: job.id) != nil else { throw VideoError.toolFailed("无效任务编号。") }
        let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let bundled = Bundle.main.resourceURL?.appendingPathComponent("video-worker").path
        let development = root.appendingPathComponent("scripts/video-worker.sh").path
        guard let path = [bundled, development].compactMap({ $0 }).first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            throw VideoError.toolFailed("未安装处理引擎，请重新构建应用。")
        }
        var arguments = ["--job", job.directory.path, "--support", AppStorage.root.path]
        if let url = APISettings.developmentEnvironment { arguments += ["--env-file", url.path] }
        let buffer = EventBuffer()
        _ = try await ProcessRunner.run(path: path, arguments: arguments, timeoutSeconds: 14_400, processGroup: true) { data in
            buffer.append(data, callback: onEvent)
        }
    }
}
