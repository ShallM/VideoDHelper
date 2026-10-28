import SwiftUI
import AppKit
import UniformTypeIdentifiers
import VideoCore

@MainActor
final class WorkspaceModel: ObservableObject {
    let workspace: WorkspaceKind
    @Published var controlsExpanded = true
    @Published var recordingURL: URL?
    @Published var recordingDuration: Double = 0
    @Published var recordingAction = "transcribe"
    @Published var dialogueStrategy = DialogueStrategy.contextV2.rawValue
    @Published var diarizationMode = "two"
    @Published var speakerNames: [String: String] = ["A": "说话人 A", "B": "说话人 B"]
    @Published var editsRevision = "original"
    @Published var recordingEditsValid = true
    let playback = AudioPlayback()
    @Published var link = ""
    @Published var metadata: VideoMetadata?
    @Published var selectedTrackID = ""
    @Published var segments: [TranscriptSegment] = []
    @Published var condensed: [CitedText] = []
    @Published var notes: StudyNotes?
    @Published var title = "把视频变成学习笔记"
    @Published var source = ""
    @Published var isBusy = false
    @Published var status = "准备就绪"
    @Published var progress: Double?
    @Published var stage = ""
    @Published var error: String?
    @Published var selectedTab = 0
    @Published var showsLogin = false
    @Published var hasSavedSession = FileManager.default.fileExists(atPath: BilibiliCookies.fileURL.path)
    @Published var transcriptionMode = "auto"
    @Published var mediaMode = "needed"
    @Published var keepMedia = false
    @Published var videoFormatID = ""
    @Published var audioFormatID = ""
    @Published var container = "mp4"
    @Published var whisperModel = "turbo"
    @Published var summaryStrategy = "structured"
    @Published var history: [ProcessingJob] = []
    @Published var library = TaskLibrary()
    @Published var collection: TaskCollection = .recent
    @Published var historyQuery = ""
    @Published var currentJob: ProcessingJob?
    private var activeTask: Task<Void, Never>?
    private var runID = UUID()
    private var libraryWritable = true

    init(workspace: WorkspaceKind = .bilibili) {
        self.workspace = workspace
        self.library = TaskLibrary(workspace: workspace)
        if workspace == .recording { title = "整理一段对话" }
        applyDefaults()
        do { library = try TaskLibrary.load(workspace: workspace) }
        catch { self.error = "无法读取项目目录。为保留现有归属，暂时停止修改目录；任务结果仍可打开。"; libraryWritable = false }
        history = AppStorage.history().filter { $0.workspace == self.workspace }
        if let saved = try? AppStorage.read(WorkspacePresentation.self, from: presentationURL) {
            switch saved.collection {
            case "unassigned": collection = .unassigned
            case "archived": collection = .archived
            case "project": if let id = saved.projectID, library.projects.contains(where: { $0.id == id }) { collection = .project(id) }
            default: break
            }
            if let id = saved.taskID, let job = history.first(where: { $0.id == id }) { openJob(job) }
            historyQuery = saved.query; link = saved.link; controlsExpanded = saved.controlsExpanded
            selectedTab = min(2, max(0, saved.tab))
        } else if let latest = visibleHistory.first { openJob(latest) }
    }
    private var presentationURL: URL { AppStorage.root.appendingPathComponent("workspace-\(workspace.rawValue).json") }
    func savePresentation() {
        let key: String
        var projectID: String?
        switch collection {
        case .recent: key = "recent"
        case .unassigned: key = "unassigned"
        case .archived: key = "archived"
        case .project(let id): key = "project"; projectID = id
        }
        try? AppStorage.write(WorkspacePresentation(taskID: currentJob?.id, collection: key, projectID: projectID,
            query: historyQuery, link: link, tab: selectedTab, controlsExpanded: controlsExpanded), to: presentationURL)
    }
    var visibleHistory: [ProcessingJob] { library.tasks(history, in: collection, matching: historyQuery) }
    var collectionTitle: String {
        switch collection {
        case .recent: return "最近任务"
        case .unassigned: return "未归属"
        case .archived: return "已归档"
        case .project(let id): return library.projects.first(where: { $0.id == id })?.name ?? "项目"
        }
    }
    var newTaskProjectID: String? { if case .project(let id) = collection { return id }; return nil }
    func taskCount(in collection: TaskCollection) -> Int { library.tasks(history, in: collection).count }
    func refreshHistory() {
        history = AppStorage.history().filter { $0.workspace == self.workspace }
        do { library = try TaskLibrary.load(workspace: workspace); libraryWritable = true }
        catch { libraryWritable = false; self.error = "无法读取任务目录，暂时停止修改归属和归档状态；原文件已保留。" }
        if let job = currentJob, !isBusy, !history.contains(where: { $0.id == job.id }) { resetReader() }
    }
    func newTask() {
        guard !isBusy else { return }
        if collection == .archived { collection = .recent }
        historyQuery = ""
        resetReader()
    }
    func resetReader() {
        recordingEditsValid = true
        dialogueStrategy = DialogueStrategy.contextV2.rawValue
        playback.stop(); recordingURL = nil; recordingDuration = 0; editsRevision = "original"
        speakerNames = ["A": "说话人 A", "B": "说话人 B"]
        metadata = nil; currentJob = nil; selectedTrackID = ""; link = ""
        segments = []; condensed = []; notes = nil; source = ""; title = workspace == .bilibili ? "把视频变成学习笔记" : "整理一段对话"
        controlsExpanded = true
        selectedTab = 0; stage = ""; progress = nil; error = nil
        applyDefaults()
        status = newTaskProjectID == nil ? (workspace == .bilibili ? "粘贴链接，开始一个新任务。" : "选择手机录音，在本机提取对话。") : "新任务将保存到「\(collectionTitle)」。"
    }
    @discardableResult
    func saveProject(id: String?, name: String) -> Bool {
        updateLibrary { library in
            if let id { try library.renameProject(id, name: name); return .project(id) }
            return .project(try library.createProject(name: name).id)
        }
    }
    func removeProject(_ project: TaskProject) {
        if updateLibrary({ library in
            library.removeProject(project.id)
            return collection == .project(project.id) ? .unassigned : collection
        }) { status = "已移除「\(project.name)」，任务归属已解除；已归档的任务保持归档，所有结果文件保留。" }
    }
    func moveJob(_ job: ProcessingJob, to projectID: String?) {
        guard job.workspace == workspace else { error = "不能跨功能页移动任务。"; return }
        if updateLibrary({ library in try library.moveTask(job, to: projectID); return collection }) {
            if !isBusy { selectAfterRemoving(job.id) }
            status = "任务已移至\(library.project(for: job.id).map { "「\($0.name)」" } ?? "未归属")。" + (library.isArchived(job.id) ? "仍保留在已归档中。" : "")
        }
    }
    var canManageTasks: Bool { !isBusy && libraryWritable }
    func archiveJob(_ job: ProcessingJob) {
        guard canManageTasks else { return }
        do {
            library = try TaskLifecycle.setArchived(true, taskID: job.id, workspace: workspace)
            selectAfterRemoving(job.id)
            status = "已归档「\(job.title)」。原稿、笔记和媒体保留，可在已归档中恢复。"
        } catch { self.error = error.localizedDescription }
    }
    func restoreJob(_ job: ProcessingJob) {
        guard canManageTasks else { return }
        do {
            library = try TaskLifecycle.setArchived(false, taskID: job.id, workspace: workspace)
            collection = library.project(for: job.id).map { .project($0.id) } ?? .unassigned
            historyQuery = ""
            openJob(job)
            status = "已恢复「\(job.title)」到\(collectionTitle)。"
        } catch { self.error = error.localizedDescription }
    }
    func trashJob(_ job: ProcessingJob) {
        guard canManageTasks else { return }
        do {
            if currentJob?.id == job.id { playback.stop() }
            library = try TaskLifecycle.trash(taskID: job.id, workspace: workspace)
            history = AppStorage.history().filter { $0.workspace == self.workspace }
            selectAfterRemoving(job.id)
            status = "「\(job.title)」已移到 macOS 废纸篓。历史用量与费用保留。"
        } catch {
            // Also reconcile the rare case where Trash succeeded but its rollback failed.
            refreshHistory()
            self.error = error.localizedDescription
        }
    }
    private func selectAfterRemoving(_ taskID: String) {
        guard currentJob?.id == taskID, !visibleHistory.contains(where: { $0.id == taskID }) else { return }
        if let next = visibleHistory.first { openJob(next) }
        else { resetReader() }
    }
    func updateLibrary(_ change: (inout TaskLibrary) throws -> TaskCollection) -> Bool {
        guard libraryWritable else { error = "项目目录未能读取，已保留原文件，请先修复 library.json 后重新打开应用。"; return false }
        do {
            var updated = try TaskLibrary.load(workspace: workspace)
            let selected = try change(&updated)
            try updated.save(to: TaskLibrary.fileURL(for: workspace))
            library = updated; collection = selected
            return true
        } catch { self.error = error.localizedDescription; return false }
    }
    var selectedTrack: SubtitleTrack? { metadata?.tracks.first { $0.id == selectedTrackID } }
    var videoFormats: [MediaFormat] {
        (metadata?.formats ?? []).filter(\.hasVideo).sorted {
            if $0.height != $1.height { return ($0.height ?? 0) > ($1.height ?? 0) }
            if $0.codecLabel != $1.codecLabel { return $0.codecLabel < $1.codecLabel }
            return ($0.tbr ?? 0) > ($1.tbr ?? 0)
        }
    }
    var audioFormats: [MediaFormat] { (metadata?.formats ?? []).filter { $0.hasAudio && !$0.hasVideo }.reversed() }
    var canStart: Bool { !isBusy && (metadata != nil || (!segments.isEmpty && currentJob == nil)) }
    var currentJobIsArchived: Bool { currentJob.map { library.isArchived($0.id) } ?? false }
    var canResume: Bool { !isBusy && currentJob != nil && currentJob?.state != "completed" && currentJob?.state != "transcribed" && !currentJobIsArchived }
    var canChooseASRModel: Bool {
        !isBusy && !currentJobIsArchived && segments.isEmpty
            && (currentJob.map { !ASRTaskSelection.isFrozen(at: $0.directory) } ?? true)
    }
    var asrModelName: String { ASRModelStore.shared.name(whisperModel) }
    var requiresInstalledASR: Bool {
        if let job = currentJob, metadata == nil,
           let request = try? AppStorage.read(ProcessingRequest.self, from: job.directory.appendingPathComponent("request.json")) {
            return ASRTaskSelection.needsModel(for: request, directory: job.directory)
        }
        return !isRecording && metadata != nil && (transcriptionMode == "whisper" || selectedTrack == nil)
    }
    func requireASRIfNeeded() throws {
        if requiresInstalledASR && !ASRModelStore.shared.isReady(whisperModel) {
            throw VideoError.toolFailed("「\(asrModelName)」尚不可用，请在设置的“语音转文字”页面下载或修复。任务与已有进度已保留。")
        }
    }
    var stageLabel: String {
        switch stage {
        case "prepare": return "准备任务"
        case "download": return "获取媒体"
        case "model": return "准备转录模型"
        case "transcribe": return "本地转录"
        case "align": return "对齐文字时间"
        case "diarize": return "区分说话人"
        case "transcribed": return "原稿已就绪"
        case "transcript": return "保存原稿"
        case "summary": return isRecording ? "理解对话与主题" : "整理台词与章节"
        case "condense": return "独立整理原话"
        case "review": return "对照原文复核"
        case "plan": return "理解全文结构"
        case "chapter": return "整理主题笔记"
        case "verify": return "检查遗漏与依据"
        case "reread": return "回查原文"
        case "overview": return isRecording ? "汇总对话总结" : "汇总学习笔记"
        case "cleanup": return "清理媒体"
        case "completed": return "处理完成"
        default: return ""
        }
    }
    func inspectLink() {
        guard !isBusy else { return }
        if currentJob != nil || metadata == nil { applyDefaults() }
        let reference: VideoReference
        do { reference = try VideoReference.parse(link) }
        catch { self.error = error.localizedDescription; return }
        start(status: "正在读取视频信息、字幕和可用下载规格…") {
            let info = try await BilibiliProbe.inspect(reference, cookieFile: BilibiliCookies.fileURL)
            try Task.checkCancellation()
            self.metadata = info
            self.selectedTrackID = info.tracks.first?.id ?? ""
            self.videoFormatID = self.videoFormats.first(where: { $0.codecLabel == "H.264" && ($0.height ?? 0) <= 1080 })?.id ?? self.videoFormats.first?.id ?? ""
            self.audioFormatID = self.audioFormats.first?.id ?? ""
            self.segments = []; self.notes = nil; self.condensed = []; self.currentJob = nil
            self.title = info.title; self.source = ""
            self.status = info.tracks.isEmpty ? "未取得字幕；使用所选本地模型转录前，请确认模型已下载。登录可能解锁平台字幕和更多规格。" : "找到 \(info.tracks.count) 条字幕轨，可直接开始生成学习笔记。"
            self.selectedTab = 0
        }
    }
    func loadTrack() {
        guard let track = selectedTrack, let info = metadata, !isBusy else { return }
        start(status: "正在读取字幕…") {
            let segments = try await BilibiliProbe.load(track, referer: info.url)
            try Task.checkCancellation()
            self.show(segments: segments, title: info.title, source: track.label)
        }
    }
    func beginProcessing() {
        guard canStart else { return }
        if collection == .archived { collection = .recent }
        if let info = metadata {
            guard (try? VideoReference.parse(link).url) == info.url else {
                error = "链接已修改，请先重新读取视频信息，避免处理上一个视频。"; return
            }
        }
        start(status: "正在准备学习笔记任务…") {
            try self.requireASRIfNeeded()
            let info = self.metadata
            let useWhisper = info != nil && (self.transcriptionMode == "whisper" || self.selectedTrack == nil)
            if let info, !useWhisper, let track = self.selectedTrack {
                let values = try await BilibiliProbe.load(track, referer: info.url)
                self.show(segments: values, title: info.title, source: track.label)
            }
            let source = useWhisper ? "本地转录 · \(self.asrModelName) · 中文" : self.source
            let job = ProcessingJob(title: self.title, url: info?.canonicalURL.absoluteString ?? "", source: source, summaryStrategy: self.summaryStrategy)
            var request = ProcessingRequest(title: job.title, url: job.url, source: source,
                segments: useWhisper ? [] : self.segments, useWhisper: useWhisper,
                mediaMode: info == nil ? "needed" : self.mediaMode, keepMedia: self.keepMedia,
                videoFormatID: self.videoFormatID, audioFormatID: self.audioFormatID,
                videoIncludesAudio: self.videoFormats.first(where: { $0.id == self.videoFormatID })?.hasAudio ?? false,
                container: self.container, whisperModel: self.whisperModel, summaryStrategy: self.summaryStrategy)
            request.asrModelID = self.whisperModel
            try AppStorage.write(request, to: job.directory.appendingPathComponent("request.json"))
            try AppStorage.write(job, to: job.directory.appendingPathComponent("job.json"))
            self.currentJob = job; self.source = source; self.notes = nil; self.condensed = []; self.historyQuery = ""
            if let projectID = self.newTaskProjectID {
                guard self.updateLibrary({ library in try library.moveTask(job, to: projectID); return self.collection }) else {
                    self.history = AppStorage.history().filter { $0.workspace == self.workspace }
                    throw VideoError.toolFailed("任务已保存在未归属，但项目归属保存失败。可以从最近任务继续。")
                }
            }
            if useWhisper { self.segments = [] }
            self.history = AppStorage.history().filter { $0.workspace == self.workspace }
            try await self.run(job)
        }
    }
    func resumeJob() {
        guard let job = currentJob, canResume else { return }
        if isRecording,
           let request = try? AppStorage.read(ProcessingRequest.self, from: job.directory.appendingPathComponent("request.json")),
           request.recordingAction != "summarize" { transcribeRecording(); return }
        start(status: "继续任务，复用已保存的原稿与完成的分段…") {
            try self.requireASRIfNeeded()
            try await self.run(job)
        }
    }
    func compareSummary(strategy: String) {
        guard let original = currentJob, !isBusy, !segments.isEmpty, !currentJobIsArchived else { return }
        let values = segments
        let projectID = library.project(for: original.id)?.id
        start(status: "使用同一份原稿创建对照任务…") {
            let job = ProcessingJob(title: original.title, url: original.url, source: original.source, summaryStrategy: strategy)
            let request = ProcessingRequest(title: job.title, url: job.url, source: job.source,
                segments: values, useWhisper: false, mediaMode: "needed", keepMedia: false,
                videoFormatID: "", audioFormatID: "", videoIncludesAudio: false,
                container: "mp4", whisperModel: "turbo", summaryStrategy: strategy)
            try AppStorage.write(request, to: job.directory.appendingPathComponent("request.json"))
            try AppStorage.write(job, to: job.directory.appendingPathComponent("job.json"))
            self.currentJob = job; self.notes = nil; self.condensed = []; self.historyQuery = ""
            self.summaryStrategy = strategy
            if let projectID { self.moveJob(job, to: projectID) }
            self.history = AppStorage.history().filter { $0.workspace == self.workspace }
            try await self.run(job)
        }
    }
    func run(_ job: ProcessingJob) async throws {
        let thisRun = runID
        defer { reloadJob(job); history = AppStorage.history().filter { $0.workspace == self.workspace } }
        do {
            try await ProcessingWorker.run(job: job) { [weak self] event in
                Task { @MainActor in
                    guard let self, self.isBusy, self.runID == thisRun else { return }
                    self.status = event.message; self.stage = event.stage; self.progress = event.progress
                    if self.segments.isEmpty, ["transcript", "summary", "condense", "review", "overview", "plan", "chapter", "verify", "reread"].contains(event.stage) {
                        self.segments = (try? AppStorage.read([TranscriptSegment].self, from: job.directory.appendingPathComponent("transcript.json"))) ?? []
                    }
                }
            }
        } catch {
            if Task.isCancelled { throw CancellationError() }
            let saved = try? AppStorage.read(ProcessingJob.self, from: job.directory.appendingPathComponent("job.json"))
            throw VideoError.toolFailed(saved?.state == "failed" ? saved!.message : "处理引擎中断。已保存完成的阶段，可继续处理。")
        }
    }
    func openJob(_ job: ProcessingJob) {
        guard !isBusy, job.workspace == workspace else { return }
        playback.stop()
        metadata = nil; selectedTrackID = ""; link = job.url
        segments = []; notes = nil; condensed = []
        reloadJob(job)
    }
    func reloadJob(_ original: ProcessingJob) {
        guard let job = try? AppStorage.read(ProcessingJob.self, from: original.directory.appendingPathComponent("job.json")) else {
            resetReader()
            status = "任务已移走或无法读取，请刷新任务列表。"
            return
        }
        guard job.workspace == workspace else { return }
        currentJob = job; title = job.title; source = job.source
        status = job.message; stage = job.stage; progress = job.progress
        if let values = try? AppStorage.read([TranscriptSegment].self, from: job.directory.appendingPathComponent("transcript.json")) { segments = values }
        else if let request = try? AppStorage.read(ProcessingRequest.self, from: job.directory.appendingPathComponent("request.json")) { segments = request.segments }
        if workspace == .recording {
            do {
                let results = try RecordingResults.load(from: job.directory)
                notes = results.notes; condensed = results.condensed
            } catch {
                notes = nil; condensed = []
                self.error = (error as? VideoError)?.localizedDescription
                    ?? "录音整理成果无法完整读取，已保留原稿与成果文件。请重新整理或恢复成果文件；未混用旧版结果。"
            }
        } else {
            condensed = (try? AppStorage.read([CitedText].self, from: job.directory.appendingPathComponent("condensed.json"))) ?? []
            notes = try? AppStorage.read(StudyNotes.self, from: job.directory.appendingPathComponent("notes.json"))
        }
        if let request = try? AppStorage.read(ProcessingRequest.self, from: job.directory.appendingPathComponent("request.json")) {
            summaryStrategy = request.summaryStrategy ?? "legacy"
            whisperModel = request.effectiveASRModelID
        }
        if workspace == .recording { loadRecording(job) }
        let preferred = (try? AppPreferences.load().general.readingTab) ?? 2
        selectedTab = preferred == 2 && notes != nil ? 2 : preferred == 1 && !condensed.isEmpty ? 1 : 0
    }
    func importSubtitle() {
        guard !isBusy else { return }
        let panel = NSOpenPanel()
        panel.title = "导入字幕"
        panel.allowedContentTypes = ["srt", "vtt", "json"].compactMap { UTType(filenameExtension: $0) }
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            guard let format = SubtitleFormat(rawValue: url.pathExtension.lowercased()) else { return }
            let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            guard size <= 10 * 1024 * 1024 else { throw VideoError.invalidSubtitle("文件超过 10 MB。") }
            let values = try TranscriptParser.parse(Data(contentsOf: url), format: format)
            applyDefaults()
            metadata = nil; currentJob = nil; link = ""
            show(segments: values, title: url.deletingPathExtension().lastPathComponent, source: "本地导入 · \(format.rawValue.uppercased())")
        } catch { self.error = error.localizedDescription }
    }
    private func show(segments: [TranscriptSegment], title: String, source: String) {
        self.segments = segments; self.title = title; self.source = source
        self.notes = nil; self.condensed = []; self.currentJob = nil
        self.status = "已读取 \(segments.count) 段字幕。"; self.error = nil; self.selectedTab = 0
    }
    func export(_ format: String) {
        guard !segments.isEmpty else { return }
        let panel = NSSavePanel()
        let notesExport = format == "notes", condensedExport = format == "condensed"
        let ext = notesExport || condensedExport ? "md" : format
        let suffix = notesExport ? (isRecording ? "对话总结" : "学习笔记") : condensedExport ? (isRecording ? "整理对话" : "精简台词") : "原稿"
        panel.title = "导出\(suffix)"
        panel.nameFieldStringValue = "\(title.replacingOccurrences(of: "/", with: "-")) · \(suffix).\(ext)"
        panel.allowedContentTypes = [UTType(filenameExtension: ext) ?? .plainText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            let content: String
            if isRecording && (notesExport || condensedExport) { content = recordingMarkdown(notesExport: notesExport) }
            else if notesExport || condensedExport, let job = currentJob {
                let file = job.directory.appendingPathComponent(notesExport ? "study-notes.md" : "condensed.md")
                if condensedExport, !FileManager.default.fileExists(atPath: file.path) {
                    content = "# \(title) · 精简台词\n\n来源：\(source)\n\n" + condensed.map { "[\(sourceLabel($0.source_ids))] \($0.text)" }.joined(separator: "\n\n")
                } else { content = displayText(try String(contentsOf: file, encoding: .utf8)) }
            } else if isRecording { content = (format == "md" ? "# \(title)\n\n" : "") + exportRecordingText(format: format) }
            else if format == "srt" { content = TranscriptExport.srt(segments) }
            else { content = (format == "md" ? "# \(title)\n\n来源：\(source)\n\n" : "") + TranscriptExport.text(segments) }
            try content.write(to: url, atomically: true, encoding: .utf8)
            status = "已导出：\(url.lastPathComponent)"
        } catch { self.error = error.localizedDescription }
    }
    func revealJob() { if let job = currentJob { NSWorkspace.shared.open(job.directory) } }
    func cleanMedia() {
        guard workspace == .bilibili else { return }
        guard var job = currentJob, !isBusy else { return }
        do {
            try AppStorage.cleanMedia(for: job)
            job.mediaFiles = []; job.message = "本任务媒体已清理，文字和已完成的转录片段保留。"
            try AppStorage.write(job, to: job.directory.appendingPathComponent("job.json"))
            reloadJob(job); history = AppStorage.history().filter { $0.workspace == self.workspace }
        } catch { self.error = error.localizedDescription }
    }
    func openSource(_ ids: [Int]) {
        if workspace == .recording {
            if let id = ids.first, let segment = segments.first(where: { $0.id == id }) { playback.seek(to: segment.start); playback.play() }
            return
        }
        let sourceURL = currentJob?.url ?? metadata?.canonicalURL.absoluteString ?? ""
        guard let first = ids.first, let segment = segments.first(where: { $0.id == first }),
              var components = URLComponents(string: sourceURL), components.host != nil else { return }
        var query = components.queryItems ?? []; query.removeAll { $0.name == "t" }
        query.append(URLQueryItem(name: "t", value: String(Int(segment.start))))
        components.queryItems = query
        if let url = components.url { NSWorkspace.shared.open(url) }
    }
    func sourceLabel(_ ids: [Int]) -> String {
        let values = ids.compactMap { id in segments.first(where: { $0.id == id })?.start }
        guard let first = values.min(), let last = values.max() else { return "回看来源" }
        return TranscriptExport.timestamp(first) + (last > first ? " – \(TranscriptExport.timestamp(last))" : "")
    }
    func sourceRanges(_ ids: [Int]) -> [[Int]] {
        var result: [[Int]] = []
        for id in Set(ids).sorted() {
            if let last = result.last?.last, id == last + 1 { result[result.count - 1].append(id) }
            else { result.append([id]) }
        }
        return result
    }
    private func applyDefaults() {
        do {
            let value = try AppPreferences.load().general
            transcriptionMode = value.transcriptionMode; whisperModel = value.effectiveASRModelID
            mediaMode = value.mediaMode; keepMedia = value.keepMedia; container = value.container
            summaryStrategy = value.effectiveSummaryStrategy
        } catch { self.error = "无法读取默认设置，已保留原文件。请在设置页检查配置。" }
    }
    func preferencesChanged() {
        hasSavedSession = FileManager.default.fileExists(atPath: BilibiliCookies.fileURL.path)
        if !isBusy && currentJob == nil && metadata == nil { applyDefaults() }
    }
    func cancel() { status = "正在取消并保存已完成的阶段…"; activeTask?.cancel() }
    func sessionSaved() {
        hasSavedSession = true; status = "已保存本机登录会话。"
        if !link.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { inspectLink() }
    }
    func start(status: String, operation: @escaping () async throws -> Void) {
        guard !isBusy else { return }
        guard WorkspaceProcessing.owner == nil else { error = "另一个功能页仍在处理，请等待完成或先取消。"; return }
        WorkspaceProcessing.owner = workspace
        playback.pause()
        isBusy = true; self.status = status; progress = nil; stage = ""; error = nil; runID = UUID()
        activeTask = Task {
            defer { isBusy = false; activeTask = nil; progress = nil; WorkspaceProcessing.owner = nil }
            do { try await operation() }
            catch is CancellationError {
                self.status = "已取消。已完成的数据保留，可继续处理。"
                if var job = self.currentJob, job.state == "running" || job.state == "pending",
                   FileManager.default.fileExists(atPath: job.directory.appendingPathComponent("job.json").path) {
                    job.state = "cancelled"; job.message = self.status
                    try? AppStorage.write(job, to: job.directory.appendingPathComponent("job.json"))
                    self.currentJob = job; self.history = AppStorage.history().filter { $0.workspace == self.workspace }
                }
            }
            catch { self.error = error.localizedDescription; self.status = "处理未完成，已保留完成的数据。" }
        }
    }
}

private struct WorkspacePresentation: Codable {
    let taskID: String?
    let collection: String
    let projectID: String?
    let query: String
    let link: String
    let tab: Int
    let controlsExpanded: Bool
}
