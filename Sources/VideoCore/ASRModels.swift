import Foundation

public enum ASRInstallState: String, Codable, Sendable {
    case uninstalled, downloading, paused, verifying, ready, repair, failed, unknown
    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Self(rawValue: raw) ?? .unknown
    }
    public var title: String {
        switch self {
        case .uninstalled: return "未下载"
        case .downloading: return "下载中"
        case .paused: return "已暂停"
        case .verifying: return "校验中"
        case .ready: return "已安装可用"
        case .repair: return "需要修复"
        case .failed: return "下载失败"
        case .unknown: return "状态无法识别"
        }
    }
}

public struct LocalASRComponent: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let repository: String
    public let revision: String
    public let bytes: Int64
    public init(id: String, name: String, repository: String, revision: String, bytes: Int64) {
        self.id = id; self.name = name; self.repository = repository; self.revision = revision; self.bytes = bytes
    }
}

public struct LocalASRModel: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let subtitle: String
    public let engine: String
    public let legacy: Bool
    public let license: String
    public let downloadBytes: Int64
    public var installedBytes: Int64
    public var partialBytes: Int64
    public var state: ASRInstallState
    public var message: String
    public let path: String
    public let components: [LocalASRComponent]
    public var inUseTask: String?
    public var progress: Double?
    public var downloadedBytes: Int64?
    public var totalBytes: Int64?
    public var isReady: Bool { state == .ready }
    public var isInUse: Bool { !(inUseTask ?? "").isEmpty }
    public var reclaimableBytes: Int64 { max(0, installedBytes) + max(0, partialBytes) }

    public init(id: String, name: String, subtitle: String, engine: String, legacy: Bool, license: String,
                downloadBytes: Int64, installedBytes: Int64 = 0, partialBytes: Int64 = 0,
                state: ASRInstallState = .uninstalled, message: String = "", path: String = "",
                components: [LocalASRComponent] = [], inUseTask: String? = nil) {
        self.id = id; self.name = name; self.subtitle = subtitle; self.engine = engine; self.legacy = legacy
        self.license = license; self.downloadBytes = downloadBytes; self.installedBytes = installedBytes
        self.partialBytes = partialBytes; self.state = state; self.message = message; self.path = path
        self.components = components; self.inUseTask = inUseTask
    }

    private enum CodingKeys: String, CodingKey {
        case id, name, subtitle, engine, legacy, license, downloadBytes, installedBytes, partialBytes, state, message, path
        case components, inUseTask, progress, downloadedBytes, totalBytes
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id); name = try c.decode(String.self, forKey: .name)
        subtitle = try c.decodeIfPresent(String.self, forKey: .subtitle) ?? ""
        engine = try c.decodeIfPresent(String.self, forKey: .engine) ?? ""
        legacy = try c.decodeIfPresent(Bool.self, forKey: .legacy) ?? false
        license = try c.decodeIfPresent(String.self, forKey: .license) ?? ""
        downloadBytes = max(0, try c.decodeIfPresent(Int64.self, forKey: .downloadBytes) ?? 0)
        installedBytes = max(0, try c.decodeIfPresent(Int64.self, forKey: .installedBytes) ?? 0)
        partialBytes = max(0, try c.decodeIfPresent(Int64.self, forKey: .partialBytes) ?? 0)
        state = try c.decodeIfPresent(ASRInstallState.self, forKey: .state) ?? .unknown
        message = try c.decodeIfPresent(String.self, forKey: .message) ?? ""
        path = try c.decodeIfPresent(String.self, forKey: .path) ?? ""
        components = try c.decodeIfPresent([LocalASRComponent].self, forKey: .components) ?? []
        inUseTask = try c.decodeIfPresent(String.self, forKey: .inUseTask)
        progress = try c.decodeIfPresent(Double.self, forKey: .progress).map { min(1, max(0, $0)) }
        downloadedBytes = try c.decodeIfPresent(Int64.self, forKey: .downloadedBytes)
        totalBytes = try c.decodeIfPresent(Int64.self, forKey: .totalBytes)
    }
}

/// One packaged manifest supplies both pre-download presentation and worker resources.
public enum ASRModelCatalog {
    public static let defaultModelID = "turbo"
    public static let primaryIDs = ["turbo", "large-v3", "qwen3-asr-1.7b"]
    private struct Manifest: Decodable {
        struct Model: Decodable {
            struct Component: Decodable {
                struct File: Decodable { let bytes: Int64 }
                let id: String, name: String, repository: String, revision: String
                let files: [File]
            }
            let id: String, name: String, subtitle: String, engine: String, license: String
            let legacy: Bool
            let components: [Component]
        }
        let schemaVersion: Int
        let models: [Model]
    }
    public static func load(from url: URL) throws -> [LocalASRModel] {
        let manifest = try JSONDecoder().decode(Manifest.self, from: Data(contentsOf: url))
        guard manifest.schemaVersion == 1 else { throw VideoError.toolFailed("语音模型目录版本无法读取，请更新应用。") }
        return manifest.models.map { item in
            let components = item.components.map { component in
                LocalASRComponent(id: component.id, name: component.name, repository: component.repository,
                                  revision: component.revision, bytes: component.files.reduce(0) { $0 + $1.bytes })
            }
            return LocalASRModel(id: item.id, name: item.name, subtitle: item.subtitle, engine: item.engine,
                                 legacy: item.legacy, license: item.license, downloadBytes: components.reduce(0) { $0 + $1.bytes },
                                 path: AppStorage.root.appendingPathComponent("models/\(item.id)").path, components: components)
        }
    }
    public static let packaged: [LocalASRModel] = {
        let candidates = [Bundle.main.resourceURL?.appendingPathComponent("backend/asr_catalog.json"),
                          URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("backend/asr_catalog.json")]
        return candidates.compactMap { $0 }.compactMap { try? load(from: $0) }.first ?? []
    }()
    public static func name(for id: String) -> String {
        packaged.first { $0.id == id }?.name ?? id
    }
}

public struct ASRModelEvent: Decodable, Sendable {
    public let stage: String
    public let modelID: String?
    public let state: ASRInstallState?
    public let message: String?
    public let progress: Double?
    public let downloadedBytes: Int64?
    public let totalBytes: Int64?
    public let models: [LocalASRModel]?
}

public final class ASRModelEventBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var pending = Data()
    private var inventory: [LocalASRModel]?
    private var errorMessage: String?
    public init() {}
    public func append(_ data: Data, onEvent: (ASRModelEvent) -> Void) {
        lock.lock()
        pending.append(data)
        var events: [ASRModelEvent] = []
        while let end = pending.firstIndex(of: 10) {
            let line = pending.prefix(upTo: end)
            pending.removeSubrange(...end)
            if let event = try? JSONDecoder().decode(ASRModelEvent.self, from: line) {
                if let models = event.models { inventory = models }
                if ["error", "failed"].contains(event.stage) { errorMessage = event.message }
                events.append(event)
            }
        }
        lock.unlock()
        for event in events { onEvent(event) }
    }
    public var result: [LocalASRModel]? { lock.lock(); defer { lock.unlock() }; return inventory }
    public var failure: String? { lock.lock(); defer { lock.unlock() }; return errorMessage }
}

public enum ASRModelWorker {
    public static func run(action: String, modelID: String? = nil, support: URL = AppStorage.root,
                           onEvent: @escaping @Sendable (ASRModelEvent) -> Void = { _ in }) async throws -> [LocalASRModel] {
        guard ["list", "download", "remove", "clear"].contains(action),
              modelID == nil || ASRModelCatalog.primaryIDs.contains(modelID!) || modelID == "small" else {
            throw VideoError.toolFailed("语音模型操作无效。")
        }
        let bundled = Bundle.main.resourceURL?.appendingPathComponent("video-worker").path
        let development = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("scripts/video-worker.sh").path
        guard let path = [bundled, development].compactMap({ $0 }).first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            throw VideoError.toolFailed("未安装模型管理组件，请重新安装 VideoDHelper。")
        }
        var arguments = ["--models", action, "--support", support.path]
        if let modelID { arguments += ["--model-id", modelID] }
        let buffer = ASRModelEventBuffer()
        let output: Data
        do {
            output = try await ProcessRunner.run(path: path, arguments: arguments,
                                           timeoutSeconds: action == "list" ? 180 : 86_400, processGroup: true) { data in
                buffer.append(data, onEvent: onEvent)
            }
        } catch {
            if Task.isCancelled { throw CancellationError() }
            if let message = buffer.failure { throw VideoError.toolFailed(message) }
            throw error
        }
        // Fast status commands may finish between streamed-output polls. The
        // complete stdout is authoritative for the final inventory.
        let completed = ASRModelEventBuffer()
        completed.append(output) { _ in }
        if let message = completed.failure ?? buffer.failure { throw VideoError.toolFailed(message) }
        guard let models = completed.result ?? buffer.result else { throw VideoError.toolFailed("模型管理组件没有返回有效状态，请刷新后重试。") }
        return models
    }
}
