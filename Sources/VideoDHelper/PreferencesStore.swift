import SwiftUI
import VideoCore

extension Notification.Name { static let videoPreferencesChanged = Notification.Name("VideoDHelper.preferencesChanged") }

enum SettingsPage: String, CaseIterable, Identifiable {
    case general, speech, models, usage
    var id: String { rawValue }
    var title: String { switch self { case .general: return "通用"; case .speech: return "语音转文字"; case .models: return "大语言模型"; case .usage: return "用量与费用" } }
    var icon: String { switch self { case .general: return "slider.horizontal.3"; case .speech: return "waveform"; case .models: return "cpu"; case .usage: return "chart.bar.xaxis" } }
}

@MainActor
final class PreferencesStore: ObservableObject {
    @Published private(set) var value: AppPreferences
    @Published var page: SettingsPage = .general
    @Published private(set) var loadError: String?
    var writable: Bool { loadError == nil }
    var colorScheme: ColorScheme? {
        switch value.general.appearance { case "light": return .light; case "dark": return .dark; default: return nil }
    }
    init() {
        do {
            var loaded = try AppPreferences.load()
            let migrated = loaded.migrateASRPreferences()
            if migrated || !FileManager.default.fileExists(atPath: AppPreferences.fileURL.path) { try loaded.save() }
            value = loaded
        } catch {
            value = AppPreferences()
            loadError = "设置文件无法读取，已保留原文件并暂停修改。任务与用量记录仍可查看。"
        }
    }
    func save(_ preferences: AppPreferences) throws {
        guard writable else { throw VideoError.toolFailed(loadError ?? "设置不可写入。") }
        try preferences.save()
        value = try AppPreferences.load()
        NotificationCenter.default.post(name: .videoPreferencesChanged, object: nil)
    }
}

@MainActor
final class PreferencesEditor: ObservableObject {
    @Published var draft: AppPreferences
    @Published var selectedProviderID: String
    @Published var feedback: String?
    @Published var feedbackIsError = false
    @Published var isConnecting = false
    @Published var operation = ""
    @Published var catalog: [String] = []
    @Published var catalogProviderID: String?
    @Published var catalogSelection = Set<String>()
    @Published var catalogSearch = ""
    @Published var showsCatalog = false
    @Published var snapshot = UsageSnapshot()
    @Published var isLoadingUsage = false
    private var connectionTask: Task<Void, Never>?
    private let store: PreferencesStore
    init(store: PreferencesStore) {
        self.store = store; draft = store.value; selectedProviderID = store.value.defaultProviderID
    }
    var isDirty: Bool { draft != store.value }
    var selectedProvider: ModelProvider? { draft.providers.first { $0.id == selectedProviderID } }
    func save() {
        do { try store.save(draft); draft = store.value; show("设置已保存。处理默认值和模型选择在下一次运行生效。") }
        catch { show(error.localizedDescription, error: true) }
    }
    func resetDraft() { draft = store.value; selectedProviderID = draft.defaultProviderID; feedback = nil }
    func makeDefaultASR(_ id: String) {
        do {
            var saved = store.value
            try saved.setDefaultASRModel(id)
            try store.save(saved)
            draft.mergeASRPreferences(from: store.value)
            show("已设为新任务默认转录模型。已开始的任务保持原模型。")
        } catch { show(error.localizedDescription, error: true) }
    }
    func dismissASRMigrationNotice() {
        do {
            var saved = store.value
            saved.general.asrLegacyNoticePending = false
            try store.save(saved)
            draft.mergeASRPreferences(from: store.value)
        } catch { show(error.localizedDescription, error: true) }
    }
    func syncImmediateASRSettings() { draft.mergeASRPreferences(from: store.value) }
    func restoreGeneralDefaults() {
        var general = GeneralPreferences()
        general.defaultASRModelID = store.value.general.defaultASRModelID
        general.asrLegacyNoticePending = store.value.general.asrLegacyNoticePending
        draft.general = general
    }
    func savePrices(_ changes: [ModelPriceChange]) throws {
        var saved = store.value
        for change in changes {
            guard let p = saved.providers.firstIndex(where: { $0.id == change.providerID }),
                  let m = saved.providers[p].models.firstIndex(where: { $0.id == change.modelConfigID && $0.modelID == change.modelID }) else {
                throw VideoError.toolFailed("模型配置已改变，请重新打开价格表。")
            }
            saved.providers[p].models[m].pricing = change.pricing
        }
        try store.save(saved)
        // Price-only saves must not commit or discard unrelated settings drafts.
        for change in changes {
            if let p = draft.providers.firstIndex(where: { $0.id == change.providerID }),
               let m = draft.providers[p].models.firstIndex(where: { $0.id == change.modelConfigID && $0.modelID == change.modelID }) {
                draft.providers[p].models[m].pricing = change.pricing
            }
        }
        show("价格表已保存，已按当前价格重新估算；历史价格快照保持不变。")
    }
    func show(_ text: String, error: Bool = false) { feedback = text; feedbackIsError = error }
    func addProvider(_ provider: ModelProvider) { draft.providers.append(provider); selectedProviderID = provider.id; feedback = nil }
    func removeProvider(_ id: String) {
        do { try draft.removeProvider(id); selectedProviderID = draft.defaultProviderID }
        catch { show(error.localizedDescription, error: true) }
    }
    func upsertModel(_ model: LanguageModel, providerID: String) {
        guard let index = draft.providers.firstIndex(where: { $0.id == providerID }) else { return }
        if let m = draft.providers[index].models.firstIndex(where: { $0.id == model.id }) { draft.providers[index].models[m] = model }
        else { draft.providers[index].models.append(model) }
        feedback = nil
    }
    func removeModel(_ modelID: String, providerID: String) {
        do { try draft.removeModel(modelID, from: providerID) }
        catch { show(error.localizedDescription, error: true) }
    }
    func makeDefault(providerID: String, modelID: String) {
        do { try draft.setDefault(providerID: providerID, modelID: modelID) }
        catch { show(error.localizedDescription, error: true) }
    }
    func syncModels() {
        guard let provider = selectedProvider, !isConnecting else { return }
        isConnecting = true; operation = "读取模型列表"; feedback = nil
        connectionTask = Task {
            defer { isConnecting = false; connectionTask = nil }
            do {
                let models = try await ProviderAPI.models(provider: provider)
                try Task.checkCancellation()
                catalogProviderID = provider.id; catalog = models; catalogSelection = []; catalogSearch = ""; showsCatalog = true
            } catch is CancellationError { show("已取消查询。") }
            catch { show(error.localizedDescription, error: true) }
        }
    }
    func addCatalogSelection() {
        guard let provider = draft.providers.first(where: { $0.id == catalogProviderID }) else { showsCatalog = false; return }
        for id in catalogSelection.sorted() where !provider.models.contains(where: { $0.modelID == id }) {
            upsertModel(LanguageModel(modelID: id), providerID: provider.id)
        }
        showsCatalog = false
    }
    func test(_ model: LanguageModel, provider: ModelProvider) {
        guard !isConnecting else { return }
        isConnecting = true; operation = "测试 \(model.name)"; feedback = nil
        connectionTask = Task {
            defer { isConnecting = false; connectionTask = nil; refreshUsage() }
            do { show(try await ProviderAPI.test(provider: provider, model: model)) }
            catch is CancellationError { show("测试已取消；未返回的用量会标为未知。") }
            catch { show(error.localizedDescription, error: true) }
        }
    }
    func cancelConnection() { connectionTask?.cancel() }
    func refreshUsage() {
        guard !isLoadingUsage else { return }
        isLoadingUsage = true
        Task {
            snapshot = await Task.detached(priority: .utility) { UsageRepository.load() }.value
            isLoadingUsage = false
        }
    }
}
