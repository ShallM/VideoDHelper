import Foundation

public struct TokenPricing: Codable, Equatable, Sendable {
    public var currency = "CNY"
    public var inputPerMillion: Double?
    public var outputPerMillion: Double?
    /// Nil explicitly means cached input uses the normal input rate.
    public var cachedInputPerMillion: Double?
    public init(currency: String = "CNY", input: Double? = nil, output: Double? = nil, cached: Double? = nil) {
        self.currency = currency; inputPerMillion = input; outputPerMillion = output; cachedInputPerMillion = cached
    }
    public var isConfigured: Bool { inputPerMillion != nil && outputPerMillion != nil }
    public static func fromInput(currency: String, input: String, output: String, cached: String, tokensPerUnit: Int = 1_000_000) throws -> TokenPricing {
        guard [1_000, 1_000_000].contains(tokensPerUnit) else { throw VideoError.toolFailed("请选择每千或每百万 Tokens 的报价单位。") }
        func rate(_ text: String) throws -> Double? {
            let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if text.isEmpty { return nil }
            guard let value = Double(text), value.isFinite, value >= 0, value <= 1_000_000_000 else {
                throw VideoError.toolFailed("单价必须为非负数字；暂时不知道可留空。")
            }
            return value * (1_000_000 / Double(tokensPerUnit))
        }
        let price = TokenPricing(currency: currency, input: try rate(input), output: try rate(output), cached: try rate(cached))
        try price.validate(); return price
    }
    public func validate() throws {
        guard ["CNY", "USD", "EUR", "HKD", "JPY"].contains(currency),
              [inputPerMillion, outputPerMillion, cachedInputPerMillion].compactMap({ $0 }).allSatisfy({ $0.isFinite && $0 >= 0 && $0 <= 1_000_000_000 }) else {
            throw VideoError.toolFailed("价格需为非负数，并选择支持的币种；留空表示暂未定价。")
        }
    }
}

public struct LanguageModel: Codable, Identifiable, Equatable, Sendable {
    public var id = UUID().uuidString
    public var modelID: String
    public var name: String
    public var reasoningEffort = "low"
    public var pricing = TokenPricing()
    public init(modelID: String, name: String? = nil) {
        self.modelID = modelID; self.name = name ?? modelID
    }
    public var reasoningEffortLabel: String { ReasoningEffort(rawValue: reasoningEffort)?.label ?? "无效强度" }
}

public struct ModelProvider: Codable, Identifiable, Equatable, Sendable {
    public var id = UUID().uuidString
    public var name: String
    public var baseURL: String
    public var apiKey = ""
    public var enabled = true
    public var models: [LanguageModel] = []
    public init(name: String, baseURL: String) { self.name = name; self.baseURL = baseURL }
}

public struct GeneralPreferences: Codable, Equatable, Sendable {
    public var transcriptionMode = "auto"
    public var whisperModel = "turbo"
    // Optional fields preserve decoding and rollback of pre-manager preferences.
    public var defaultASRModelID: String? = "turbo"
    public var asrLegacyNoticePending: Bool?
    public var effectiveASRModelID: String { defaultASRModelID ?? ASRModelCatalog.defaultModelID }
    public var mediaMode = "needed"
    public var keepMedia = false
    public var container = "mp4"
    public var appearance = "system"
    public var readingTab = 2
    public var textSize = 15
    public var summaryConcurrency = 2
    // Optional so older settings keep decoding without migration or data loss.
    public var summaryStrategy: String?
    public var effectiveSummaryStrategy: String {
        get { summaryStrategy ?? "structured" }
        set { summaryStrategy = newValue }
    }
    public init() {}
}

public struct AppPreferences: Codable, Equatable, Sendable {
    public var schemaVersion = 1
    public var general = GeneralPreferences()
    public var providers: [ModelProvider]
    public var defaultProviderID: String
    public var defaultModelID: String
    public static var fileURL: URL { AppStorage.root.appendingPathComponent("preferences.json") }

    public init(legacy: APISettings = APISettings()) {
        var provider = ModelProvider(name: "积算平台", baseURL: legacy.baseURL)
        provider.apiKey = legacy.apiKey
        if URL(string: legacy.baseURL)?.host != "api.icompify.com" { provider.name = "已配置的服务商" }
        var model = LanguageModel(modelID: legacy.model)
        model.reasoningEffort = legacy.reasoningEffort
        provider.models = [model]; providers = [provider]
        defaultProviderID = provider.id; defaultModelID = model.id
    }

    public static func load(from url: URL = fileURL, legacy: APISettings? = nil) throws -> AppPreferences {
        if FileManager.default.fileExists(atPath: url.path) {
            let value = try AppStorage.read(Self.self, from: url)
            try value.validate(); return value
        }
        return AppPreferences(legacy: legacy ?? APISettings.load())
    }
    public var defaultProvider: ModelProvider? { providers.first { $0.id == defaultProviderID } }
    public var defaultModel: LanguageModel? { defaultProvider?.models.first { $0.id == defaultModelID } }

    @discardableResult public mutating func migrateASRPreferences() -> Bool {
        guard general.defaultASRModelID == nil else { return false }
        general.defaultASRModelID = ASRModelCatalog.defaultModelID
        if general.whisperModel == "small" { general.asrLegacyNoticePending = true }
        return true
    }
    public mutating func setDefaultASRModel(_ id: String) throws {
        guard ASRModelCatalog.primaryIDs.contains(id) else { throw VideoError.toolFailed("请选择当前支持的语音模型。旧版模型仅用于恢复已有任务。") }
        general.defaultASRModelID = id
    }
    /// Merge only immediate ASR settings into a draft, never other unsaved edits.
    public mutating func mergeASRPreferences(from saved: AppPreferences) {
        general.defaultASRModelID = saved.general.defaultASRModelID
        general.asrLegacyNoticePending = saved.general.asrLegacyNoticePending
    }

    public mutating func setDefault(providerID: String, modelID: String) throws {
        guard let provider = providers.first(where: { $0.id == providerID }), provider.enabled,
              provider.models.contains(where: { $0.id == modelID }) else {
            throw VideoError.toolFailed("请先启用服务商，并添加需要使用的模型。")
        }
        defaultProviderID = providerID; defaultModelID = modelID
    }
    public mutating func removeProvider(_ id: String) throws {
        guard id != defaultProviderID else { throw VideoError.toolFailed("请先切换默认模型，再移除此服务商。") }
        providers.removeAll { $0.id == id }
    }
    public mutating func removeModel(_ id: String, from providerID: String) throws {
        guard id != defaultModelID else { throw VideoError.toolFailed("请先切换默认模型，再移除此模型。") }
        guard let index = providers.firstIndex(where: { $0.id == providerID }) else { return }
        providers[index].models.removeAll { $0.id == id }
    }
    public func validate() throws {
        guard schemaVersion == 1 else { throw VideoError.toolFailed("此设置文件来自更新版本，已保留原文件。") }
        guard ["auto", "whisper"].contains(general.transcriptionMode), ["turbo", "small"].contains(general.whisperModel),
              ["needed", "audio", "video"].contains(general.mediaMode), ["mp4", "mkv"].contains(general.container),
              ["system", "light", "dark"].contains(general.appearance), (0...2).contains(general.readingTab),
              (13...19).contains(general.textSize), (1...2).contains(general.summaryConcurrency),
              ["structured", "legacy"].contains(general.effectiveSummaryStrategy) else {
            throw VideoError.toolFailed("常用设置的选项无效。")
        }
        guard Set(providers.map(\.id)).count == providers.count else { throw VideoError.toolFailed("服务商编号重复。") }
        var modelIDs = Set<String>()
        for provider in providers {
            guard UUID(uuidString: provider.id) != nil, !provider.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  provider.name.count <= 60 else { throw VideoError.toolFailed("服务商名称需为 1～60 个字符。") }
            if provider.enabled || !provider.baseURL.isEmpty { _ = try APIEndpoint.base(provider.baseURL) }
            var identifiers = Set<String>()
            for model in provider.models {
                guard UUID(uuidString: model.id) != nil, modelIDs.insert(model.id).inserted,
                      !model.modelID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                      model.modelID.count <= 200, !model.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                      model.name.count <= 100, identifiers.insert(model.modelID).inserted else {
                    throw VideoError.toolFailed("模型 ID / 名称无效或重复，请检查模型列表。")
                }
                _ = try ReasoningEffort.validated(model.reasoningEffort, modelID: model.modelID)
                try model.pricing.validate()
            }
        }
        guard let provider = defaultProvider, provider.enabled, defaultModel != nil else {
            throw VideoError.toolFailed("请保留一个已启用的默认模型；停用或删除前需先切换默认模型。")
        }
    }
    public func save(to url: URL = fileURL) throws {
        try validate()
        var value = self
        for index in value.providers.indices where !value.providers[index].baseURL.isEmpty {
            value.providers[index].baseURL = try APIEndpoint.base(value.providers[index].baseURL).absoluteString
        }
        try AppStorage.write(value, to: url)
    }
}

public enum APIEndpoint {
    public static func base(_ value: String) throws -> URL {
        var cleaned = value.trimmingCharacters(in: .whitespacesAndNewlines)
        while cleaned.hasSuffix("/") { cleaned.removeLast() }
        for suffix in ["/chat/completions", "/models"] where cleaned.hasSuffix(suffix) { cleaned.removeLast(suffix.count) }
        guard let url = URL(string: cleaned), let host = url.host?.lowercased(), !host.isEmpty,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.scheme == "https" || (url.scheme == "http" && ["localhost", "127.0.0.1", "[::1]", "::1"].contains(host)) else {
            throw VideoError.toolFailed("请输入 HTTPS API 根地址；本机 localhost / 127.0.0.1 可使用 HTTP。不要包含密钥或查询参数。")
        }
        return url
    }
    public static func url(_ baseURL: String, path: String) throws -> URL { try base(baseURL).appendingPathComponent(path) }
}
