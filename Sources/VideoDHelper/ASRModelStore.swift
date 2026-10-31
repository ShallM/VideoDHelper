import SwiftUI
import VideoCore

/// Application-owned downloads survive settings window navigation and closure.
@MainActor
final class ASRModelStore: ObservableObject {
    static let shared = ASRModelStore()
    @Published private(set) var models = ASRModelCatalog.packaged.filter { !$0.legacy }
    @Published private(set) var inventoryLoaded = false
    @Published private(set) var isRefreshing = false
    @Published private(set) var queuedIDs: [String] = []
    @Published private(set) var activeID: String?
    @Published private(set) var mutationIDs = Set<String>()
    @Published private(set) var feedback: String?
    @Published private(set) var feedbackIsError = false
    private var downloadTask: Task<Void, Never>?
    private var refreshTask: Task<Void, Never>?
    private var isQuitting = false
    private var shouldRefreshAgain = false
    private var clearAfterPause = Set<String>()
    private var activeOperationID: UUID?
    private var inventoryEpoch = 0

    var readyIDs: [String] { models.filter { $0.isReady }.map(\.id) }
    var primaryModels: [LocalASRModel] { models.filter { !$0.legacy } }
    var legacyModels: [LocalASRModel] { models.filter(\.legacy) }
    var installedCount: Int { models.filter(\.isReady).count }
    var occupiedBytes: Int64 { models.reduce(0) { $0 + $1.reclaimableBytes } }
    func record(_ id: String) -> LocalASRModel? { models.first { $0.id == id } }
    func isReady(_ id: String) -> Bool { inventoryLoaded && record(id)?.isReady == true && !mutationIDs.contains(id) }
    func name(_ id: String) -> String { record(id)?.name ?? ASRModelCatalog.name(for: id) }
    func isQueued(_ id: String) -> Bool { queuedIDs.contains(id) }
    func isMutating(_ id: String) -> Bool { mutationIDs.contains(id) }

    init() { refresh() }

    func refresh() {
        guard !isQuitting else { return }
        guard !isRefreshing else { shouldRefreshAgain = true; return }
        isRefreshing = true
        let epoch = inventoryEpoch
        refreshTask = Task {
            defer {
                isRefreshing = false; refreshTask = nil
                if shouldRefreshAgain { shouldRefreshAgain = false; refresh() }
            }
            do {
                let values = try await ASRModelWorker.run(action: "list")
                try Task.checkCancellation()
                if epoch == inventoryEpoch { replaceInventory(values, preservingActiveProgress: true) }
                else { shouldRefreshAgain = true }
            } catch is CancellationError { }
            catch { show("无法读取本地模型：\(error.localizedDescription)", error: true) }
        }
    }

    func download(_ id: String) {
        guard !isQuitting, inventoryLoaded, let model = record(id), !model.isReady,
              !model.isInUse, !mutationIDs.contains(id), activeID != id, !queuedIDs.contains(id) else { return }
        queuedIDs.append(id)
        feedback = nil
        startNextDownload()
    }

    func pause(_ id: String) {
        if let index = queuedIDs.firstIndex(of: id) {
            queuedIDs.remove(at: index)
            show("已取消排队。")
        } else if activeID == id {
            update(id) { $0.message = "正在保存下载进度并暂停…" }
            downloadTask?.cancel()
        }
    }

    func remove(_ id: String) {
        guard let model = record(id), !model.isInUse, activeID != id, !queuedIDs.contains(id) else { return }
        mutate(id, action: "remove", success: "模型已删除。已有录音、原稿、笔记和默认选择均已保留。")
    }

    func clear(_ id: String) {
        queuedIDs.removeAll { $0 == id }
        if activeID == id {
            clearAfterPause.insert(id)
            pause(id)
        } else {
            mutate(id, action: "clear", success: "已清理未完成的下载文件。")
        }
    }

    func prepareForQuit() {
        isQuitting = true
        queuedIDs.removeAll(); clearAfterPause.removeAll()
        refreshTask?.cancel(); downloadTask?.cancel()
    }

    private func startNextDownload() {
        guard !isQuitting, downloadTask == nil, !queuedIDs.isEmpty else { return }
        let id = queuedIDs.removeFirst()
        guard let model = record(id), !model.isReady, !model.isInUse, !mutationIDs.contains(id) else {
            startNextDownload(); return
        }
        activeID = id
        let operationID = UUID()
        activeOperationID = operationID
        inventoryEpoch += 1
        update(id) { $0.state = .downloading; $0.message = "准备下载…"; $0.progress = nil }
        downloadTask = Task {
            defer {
                activeID = nil; activeOperationID = nil; downloadTask = nil; inventoryEpoch += 1
                if !isQuitting {
                    if clearAfterPause.remove(id) != nil { clear(id) }
                    refresh(); startNextDownload()
                }
            }
            do {
                let values = try await ASRModelWorker.run(action: "download", modelID: id) { [weak self] event in
                    Task { @MainActor in self?.receive(event, for: id, operationID: operationID) }
                }
                try Task.checkCancellation()
                replaceInventory(values, preservingActiveProgress: false)
                show("\(name(id)) 已准备好，可设为默认或在任务中选择。")
            } catch is CancellationError {
                update(id) { $0.state = .paused; $0.message = "下载已暂停，已完成部分会在继续时复用。" }
                if !isQuitting { show("下载已暂停。") }
            } catch {
                update(id) { $0.state = .failed; $0.message = error.localizedDescription }
                if !isQuitting { show(error.localizedDescription, error: true) }
            }
        }
    }

    private func mutate(_ id: String, action: String, success: String) {
        guard !isQuitting, !mutationIDs.contains(id), record(id)?.isInUse != true else { return }
        mutationIDs.insert(id)
        inventoryEpoch += 1
        Task {
            defer { mutationIDs.remove(id); inventoryEpoch += 1; refresh() }
            do {
                let values = try await ASRModelWorker.run(action: action, modelID: id)
                replaceInventory(values, preservingActiveProgress: true)
                show(success)
            } catch is CancellationError { }
            catch { show(error.localizedDescription, error: true) }
        }
    }

    private func receive(_ event: ASRModelEvent, for id: String, operationID: UUID) {
        guard activeID == id, activeOperationID == operationID, event.modelID == id, !isQuitting else { return }
        update(id) { model in
            if let state = event.state { model.state = state }
            if let message = event.message { model.message = message }
            model.progress = event.progress.map { min(1, max(0, $0)) }
            if let bytes = event.downloadedBytes { model.downloadedBytes = max(0, bytes) }
            if let bytes = event.totalBytes { model.totalBytes = max(0, bytes) }
        }
    }

    private func replaceInventory(_ values: [LocalASRModel], preservingActiveProgress: Bool) {
        var updated = values
        if preservingActiveProgress, let id = activeID, let current = record(id),
           let index = updated.firstIndex(where: { $0.id == id }) {
            updated[index].state = current.state
            updated[index].message = current.message
            updated[index].progress = current.progress
            updated[index].downloadedBytes = current.downloadedBytes
            updated[index].totalBytes = current.totalBytes
        }
        models = updated; inventoryLoaded = true
    }
    private func update(_ id: String, action: (inout LocalASRModel) -> Void) {
        if let index = models.firstIndex(where: { $0.id == id }) { action(&models[index]) }
    }
    private func show(_ text: String, error: Bool = false) { feedback = text; feedbackIsError = error }
}
