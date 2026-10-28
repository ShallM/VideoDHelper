import SwiftUI
import VideoCore

struct WorkspaceView: View {
    @StateObject private var video = WorkspaceModel(workspace: .bilibili)
    @StateObject private var recording = WorkspaceModel(workspace: .recording)
    @State private var workspace: WorkspaceKind = (try? AppStorage.read(WorkspaceKind.self, from: AppStorage.root.appendingPathComponent("selected-workspace.json"))) ?? .bilibili
    var body: some View {
        WorkspacePage(model: workspace == .bilibili ? video : recording,
                      workspace: $workspace, otherIsBusy: workspace == .bilibili ? recording.isBusy : video.isBusy)
            .id(workspace)
            .onChange(of: workspace) { _, value in
                video.playback.pause(); recording.playback.pause()
                video.savePresentation(); recording.savePresentation()
                try? AppStorage.write(value, to: AppStorage.root.appendingPathComponent("selected-workspace.json"))
            }
    }
}

struct WorkspacePage: View {
    @EnvironmentObject private var preferences: PreferencesStore
    @Environment(\.openSettings) private var openSettings
    @ObservedObject var model: WorkspaceModel
    @Binding var workspace: WorkspaceKind
    let otherIsBusy: Bool
    @State private var editingSegment: TranscriptSegment?
    @State private var optionsExpanded = true
    @State private var showsProjectEditor = false
    @State private var editingProject: TaskProject?
    @State private var projectName = ""
    @State private var projectError: String?
    @State private var removingProject: TaskProject?
    @State private var deletingJob: ProcessingJob?
    @FocusState private var projectNameFocused: Bool

    var body: some View {
        GeometryReader { geometry in
        HSplitView {
            sidebar.frame(minWidth: 280, idealWidth: 310, maxWidth: 350)
            VStack(alignment: .leading, spacing: 0) {
                header
                Divider()
                if model.metadata != nil {
                    DisclosureGroup("处理选项", isExpanded: $optionsExpanded) { options.padding(.top, 12) }
                        .font(.headline).padding(20)
                    Divider()
                }
                if model.segments.isEmpty { emptyState }
                else { readingContent }
                if model.isRecording, model.recordingURL != nil { AudioPlayerBar(player: model.playback) }
                Divider()
                footer
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(width: geometry.size.width, height: geometry.size.height)
        }
        .alert("暂时无法完成", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
            Button("知道了") { model.error = nil }
        } message: { Text(model.error ?? "") }
        .sheet(isPresented: $model.showsLogin) { BilibiliLoginView(onSaved: model.sessionSaved) }
        .sheet(isPresented: $showsProjectEditor) { projectEditor }
        .sheet(item: $editingSegment) { segment in RecordingSegmentEditor(model: model, segment: segment) }
        .confirmationDialog("移除项目", isPresented: Binding(get: { removingProject != nil }, set: { if !$0 { removingProject = nil } })) {
            Button("移除项目，保留所有任务", role: .destructive) {
                if let project = removingProject { model.removeProject(project) }
                removingProject = nil
            }
        } message: { Text("「\(removingProject?.name ?? "")」中的任务会解除项目归属，已归档任务仍保留在已归档中。原稿、笔记和媒体文件都会保留。") }
        .confirmationDialog("删除任务", isPresented: Binding(get: { deletingJob != nil }, set: { if !$0 { deletingJob = nil } }), titleVisibility: .visible) {
            Button("移到废纸篓", role: .destructive) {
                if let job = deletingJob { model.trashJob(job) }
                deletingJob = nil
            }
            Button("取消", role: .cancel) { deletingJob = nil }
        } message: {
            Text("将「\(deletingJob?.title ?? "")」的原稿、笔记、检查点和任务内媒体一起移到 macOS 废纸篓。历史用量与费用保留。\n\n可在 Finder 废纸篓中放回，恢复后位于未归属；清空废纸篓后无法恢复。")
        }
        .onChange(of: model.isBusy) { _, busy in if busy && model.currentJob != nil { optionsExpanded = false } }
        .onChange(of: model.currentJob?.id) { _, id in if id != nil { optionsExpanded = false } }
        .onChange(of: model.metadata?.title) { _, _ in optionsExpanded = true }
        .onChange(of: model.selectedTab) { _, _ in model.savePresentation() }
        .onChange(of: model.currentJob?.id) { _, _ in model.savePresentation() }
        .onChange(of: model.collection) { _, _ in model.savePresentation() }
        .onChange(of: model.historyQuery) { _, _ in model.savePresentation() }
        .onChange(of: model.link) { _, _ in model.savePresentation() }
        .onChange(of: model.controlsExpanded) { _, _ in model.savePresentation() }
        .onReceive(NotificationCenter.default.publisher(for: .videoPreferencesChanged)) { _ in model.preferencesChanged() }
        .toolbar {
            ToolbarItemGroup {
                Button { model.isRecording ? model.importRecording() : model.importSubtitle() } label: { Label(model.isRecording ? "导入录音" : "导入字幕", systemImage: "square.and.arrow.down") }.disabled(model.isBusy || otherIsBusy)
                Menu {
                    Button(model.isRecording ? "对话总结 (.md)" : "学习笔记 (.md)") { model.export("notes") }.disabled(model.notes == nil || model.summaryStale)
                    Button(model.isRecording ? "整理对话 (.md)" : "精简台词 (.md)") { model.export("condensed") }.disabled(model.condensed.isEmpty || model.summaryStale)
                    Divider()
                    Button("原稿 Markdown (.md)") { model.export("md") }
                    Button("原稿纯文本 (.txt)") { model.export("txt") }
                    Button("原始字幕 (.srt)") { model.export("srt") }
                } label: { Label("导出", systemImage: "square.and.arrow.up") }
                    .accessibilityLabel("导出结果").disabled(model.segments.isEmpty)
                Button { openSettings() } label: { Label("设置", systemImage: "gearshape") }
            }
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Menu {
                    ForEach(WorkspaceKind.allCases) { kind in
                        Button { workspace = kind } label: {
                            Label(kind.title + (otherIsBusy && kind != workspace ? " · 处理中" : ""), systemImage: kind == workspace ? "checkmark" : kind.symbol)
                        }
                    }
                } label: {
                    HStack(spacing: 9) {
                        Image(systemName: workspace.symbol).font(.title2).foregroundStyle(.teal)
                        Text(workspace.title).font(.title3.bold())
                        Image(systemName: "chevron.down").font(.caption)
                    }.padding(.vertical, 5)
                }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel("切换功能页")
                Spacer()
                Button(action: model.newTask) { Image(systemName: "square.and.pencil") }
                    .buttonStyle(.borderless).help("新任务（⌘N）").accessibilityLabel("新任务")
                    .keyboardShortcut("n", modifiers: .command).disabled(model.isBusy)
            }
            DisclosureGroup(model.isRecording ? "录音处理" : "视频处理", isExpanded: $model.controlsExpanded) {
                VStack(alignment: .leading, spacing: 10) {
                    if model.isRecording { recordingControls }
                    else { videoControls }
                }.padding(.top, 10)
            }.font(.caption.weight(.medium))
            if otherIsBusy {
                Label("另一个功能页正在处理，完成后可开始新任务。", systemImage: "hourglass")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if model.canResume {
                Button(action: model.resumeJob) { Label("继续处理", systemImage: "arrow.clockwise").frame(maxWidth: .infinity) }
                    .buttonStyle(.borderedProminent).disabled(otherIsBusy)
            }
            Divider()
            VStack(spacing: 3) {
                collectionButton(.recent, title: "最近", icon: "clock")
                collectionButton(.unassigned, title: "未归属", icon: "tray")
                collectionButton(.archived, title: "已归档", icon: "archivebox")
            }
            HStack {
                Text(model.isRecording ? "录音项目" : "视频项目").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Spacer()
                Button { editProject(nil) } label: { Image(systemName: "plus") }
                    .buttonStyle(.borderless).help("新建项目").accessibilityLabel("新建项目")
            }
            if model.library.projects.isEmpty {
                Button(model.isRecording ? "创建项目，整理录音与对话" : "创建项目，整理视频与笔记") { editProject(nil) }.buttonStyle(.link).font(.caption)
            } else {
                ScrollView {
                    VStack(spacing: 3) {
                        ForEach(model.library.projects) { project in
                            collectionButton(.project(project.id), title: project.name, icon: "folder")
                                .contextMenu {
                                    Button("重命名项目") { editProject(project) }
                                    Button("移除项目…", role: .destructive) { removingProject = project }
                                }
                        }
                    }
                }.frame(height: min(CGFloat(model.library.projects.count) * 32, 112))
            }
            Divider()
            HStack {
                Text(model.collectionTitle).font(.headline).lineLimit(1)
                Text("\(model.visibleHistory.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                Spacer()
                Button(action: model.refreshHistory) { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless).accessibilityLabel("刷新任务").help("刷新任务")
            }
            TextField("搜索当前列表", text: $model.historyQuery).textFieldStyle(.roundedBorder)
                .accessibilityLabel("搜索任务")
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 6) {
                    if model.visibleHistory.isEmpty {
                        Text(!model.historyQuery.isEmpty ? "没有匹配的任务。" : model.collection == .archived ? "还没有归档任务。\n右键任务选择归档，可保留结果并收起已完成的工作。" : model.newTaskProjectID != nil ? "这里还没有任务。\n新任务会保存到当前项目；也可将已有任务移入。" : model.isRecording ? "这里还没有录音任务。\n导入手机录音，结果会自动保存。" : "这里还没有任务。\n粘贴链接或导入字幕，结果会自动保存。")
                            .font(.callout).foregroundStyle(.secondary).padding(.vertical, 8)
                    }
                    ForEach(model.visibleHistory) { job in taskRow(job) }
                }
            }.frame(minHeight: 80, maxHeight: .infinity)
            Text("VideoDHelper · \(AppInfo.version)").font(.caption2).foregroundStyle(.tertiary)
        }.padding(20).background(.quaternary.opacity(0.3))
    }

    private var videoControls: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 10) {
                TextField("粘贴 B 站链接或整段分享文字", text: $model.link, axis: .vertical)
                    .textFieldStyle(.roundedBorder).lineLimit(2...3).onSubmit { model.inspectLink() }.disabled(model.isBusy || otherIsBusy)
                HStack {
                    Button("读取视频信息", action: model.inspectLink).disabled(model.link.isEmpty || model.isBusy)
                    Spacer()
                    Button(model.hasSavedSession ? "已登录 · 更新" : "登录 B 站") { model.showsLogin = true }
                        .buttonStyle(.borderless).font(.caption).disabled(model.isBusy || otherIsBusy)
                }
            }
            if let info = model.metadata {
                VStack(alignment: .leading, spacing: 8) {
                    Text(info.title).font(.callout.weight(.medium)).lineLimit(3)
                    HStack {
                        Text(info.author).lineLimit(1)
                        Spacer()
                        if let duration = info.duration { Label(TranscriptExport.timestamp(duration), systemImage: "clock") }
                    }.font(.caption).foregroundStyle(.secondary)
                }
            }
            Button(action: model.beginProcessing) {
                Label("生成学习笔记", systemImage: "sparkles").frame(maxWidth: .infinity).padding(.vertical, 4)
            }.buttonStyle(.borderedProminent).tint(.teal).disabled(!model.canStart || otherIsBusy)
            Button { preferences.page = .models; openSettings() } label: {
                HStack(spacing: 6) {
                    Image(systemName: "cpu")
                    Text((preferences.value.defaultModel?.name ?? "配置模型服务") + " · " + (preferences.value.defaultModel?.reasoningEffortLabel ?? "默认")).lineLimit(1)
                    Spacer(); Image(systemName: "chevron.right").font(.caption2)
                }.font(.caption).foregroundStyle(.secondary)
            }.buttonStyle(.plain).help("管理模型服务与价格")
        }
    }

    private var recordingControls: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button(action: model.importRecording) { Label("选择录音", systemImage: "waveform.badge.plus").frame(maxWidth: .infinity) }
                .disabled(model.isBusy || otherIsBusy)
            if model.recordingURL != nil {
                Text(model.title).font(.callout).lineLimit(2)
                Text("普通话 · 保留录音副本").font(.caption).foregroundStyle(.secondary)
            } else { Text("M4A、MP3、WAV · 最长 50 分钟").font(.caption).foregroundStyle(.secondary) }
            Picker("说话人", selection: $model.diarizationMode) {
                Text("二人 · 本地区分").tag("two")
                Text("不区分").tag("off")
            }.disabled(model.isBusy || model.currentJobIsArchived || !model.canTranscribeRecording && model.currentJob != nil)
            Picker("转录模型", selection: $model.whisperModel) {
                Text("Large v3 Turbo").tag("turbo")
                Text("Small").tag("small")
            }.disabled(model.isBusy || !model.segments.isEmpty)
            if model.segments.isEmpty || model.canTranscribeRecording {
                Button(action: model.transcribeRecording) { Label("开始转录", systemImage: "waveform").frame(maxWidth: .infinity) }
                    .buttonStyle(.borderedProminent).tint(.teal).disabled(!model.canTranscribeRecording || otherIsBusy)
            }
            if !model.segments.isEmpty {
                Button(action: model.summarizeRecording) { Label(model.notes == nil ? "整理与总结" : "重新整理与总结", systemImage: "sparkles").frame(maxWidth: .infinity) }
                    .buttonStyle(.borderedProminent).tint(.teal).disabled(!model.canSummarizeRecording || otherIsBusy)
            }
            Button { preferences.page = .models; openSettings() } label: {
                Label((preferences.value.defaultModel?.name ?? "配置模型服务") + " · " + (preferences.value.defaultModel?.reasoningEffortLabel ?? "默认"), systemImage: "cpu")
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }.buttonStyle(.plain)
            Text("转录在本机完成。点击整理与总结后，将文字发送给所选模型服务。")
                .font(.caption).foregroundStyle(.secondary)
            if model.diarizationMode == "two" {
                Text("首次准备约 11 MB 分人模型；短应答和重叠讲话可能需要校对。")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private func collectionButton(_ collection: TaskCollection, title: String, icon: String) -> some View {
        Button { model.collection = collection; model.historyQuery = "" } label: {
            HStack {
                Label(title, systemImage: icon).lineLimit(1)
                Spacer()
                Text("\(model.taskCount(in: collection))").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }.padding(.horizontal, 8).padding(.vertical, 6).contentShape(Rectangle())
                .background(model.collection == collection ? Color.teal.opacity(0.12) : Color.clear, in: RoundedRectangle(cornerRadius: 6))
        }.buttonStyle(.plain).accessibilityLabel(title)
    }

    private func taskRow(_ job: ProcessingJob) -> some View {
        Button { model.openJob(job) } label: {
            VStack(alignment: .leading, spacing: 6) {
                Text(job.title).font(.callout).lineLimit(2).multilineTextAlignment(.leading)
                Text("\(job.summaryStrategyLabel) · \(job.source)").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                HStack {
                    Text(model.isBusy && model.currentJob?.id == job.id ? "处理中" : job.stateLabel)
                    Spacer()
                    Text(Date(timeIntervalSince1970: job.updatedAt), style: .date)
                }.font(.caption2).foregroundStyle(.secondary)
                if model.collection == .recent || model.collection == .archived, let project = model.library.project(for: job.id) {
                    Label(project.name, systemImage: "folder").font(.caption2).foregroundStyle(.teal).lineLimit(1)
                }
            }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
                .background(model.currentJob?.id == job.id ? Color.teal.opacity(0.12) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
        }.buttonStyle(.plain).disabled(model.isBusy)
            .contextMenu {
                Menu("移至项目") { moveDestinations(job) }
                Divider()
                taskLifecycleActions(job)
            }
    }

    @ViewBuilder private func taskLifecycleActions(_ job: ProcessingJob) -> some View {
        if model.library.isArchived(job.id) {
            Button { model.restoreJob(job) } label: { Label("恢复任务", systemImage: "arrow.uturn.backward") }
                .disabled(!model.canManageTasks)
        } else {
            Button { model.archiveJob(job) } label: { Label("归档任务", systemImage: "archivebox") }
                .disabled(!model.canManageTasks)
        }
        Button(role: .destructive) { deletingJob = job } label: { Label("删除任务…", systemImage: "trash") }
            .disabled(!model.canManageTasks)
    }

    @ViewBuilder private func moveDestinations(_ job: ProcessingJob) -> some View {
        Button { model.moveJob(job, to: nil) } label: {
            Label("未归属", systemImage: model.library.project(for: job.id) == nil ? "checkmark" : "tray")
        }
        ForEach(model.library.projects) { project in
            Button { model.moveJob(job, to: project.id) } label: {
                Label(project.name, systemImage: model.library.project(for: job.id)?.id == project.id ? "checkmark" : "folder")
            }
        }
    }

    private func editProject(_ project: TaskProject?) {
        editingProject = project; projectName = project?.name ?? ""; projectError = nil; showsProjectEditor = true
    }

    private var projectEditor: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(editingProject == nil ? "新建项目" : "重命名项目").font(.title2.bold())
            Text(model.isRecording ? "项目只管理录音任务，例如工作交流、访谈记录。与视频项目各自独立。" : "按主题整理视频任务，例如 AI 研究、编程教程。移动任务时，原稿和笔记都会保留。")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextField("项目名称", text: $projectName).textFieldStyle(.roundedBorder).focused($projectNameFocused)
            if let projectError { Text(projectError).foregroundStyle(.red).font(.caption) }
            HStack {
                Spacer()
                Button("取消") { showsProjectEditor = false }.keyboardShortcut(.cancelAction)
                Button(editingProject == nil ? "创建项目" : "保存名称") {
                    if model.saveProject(id: editingProject?.id, name: projectName) { showsProjectEditor = false }
                    else { projectError = model.error; model.error = nil }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(projectName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(24).frame(width: 380).onAppear { projectNameFocused = true }
    }

    private var options: some View {
        HStack(alignment: .top, spacing: 22) {
            VStack(alignment: .leading, spacing: 10) {
                Label("文字来源", systemImage: "captions.bubble").font(.subheadline.bold())
                Picker("总结方式", selection: $model.summaryStrategy) {
                    Text("全文理解与核验").tag("structured")
                    Text("旧版分块 · 对照").tag("legacy")
                }.disabled(model.currentJob != nil)
                Picker("提取方式", selection: $model.transcriptionMode) {
                    Text("优先平台字幕，缺少时本地转录").tag("auto")
                    Text("强制本地 Whisper 转录").tag("whisper")
                }.labelsHidden()
                if model.transcriptionMode == "auto", let info = model.metadata, !info.tracks.isEmpty {
                    Picker("字幕语言", selection: $model.selectedTrackID) {
                        ForEach(info.tracks) { Text($0.label).tag($0.id) }
                    }.labelsHidden()
                    Button("先查看原始字幕", action: model.loadTrack).buttonStyle(.link)
                } else {
                    Picker("本地模型", selection: $model.whisperModel) {
                        Text("Large v3 Turbo · 约 1.6 GB").tag("turbo")
                        Text("Small · 约 0.5 GB").tag("small")
                    }.labelsHidden()
                    Text("优先复用本机模型；缺少时才下载。中文识别使用 MLX / Metal 加速。").font(.caption).foregroundStyle(.secondary)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
            VStack(alignment: .leading, spacing: 10) {
                Label("媒体下载", systemImage: "arrow.down.circle").font(.subheadline.bold())
                Picker("下载内容", selection: $model.mediaMode) {
                    Text("仅获取生成笔记所需内容").tag("needed")
                    Text("同时下载音频").tag("audio")
                    Text("同时下载视频").tag("video")
                }.labelsHidden().onChange(of: model.mediaMode) { _, mode in if mode != "needed" { model.keepMedia = true } }
                if model.mediaMode == "video" {
                    Picker("视频规格", selection: $model.videoFormatID) {
                        ForEach(model.videoFormats) { Text($0.label).tag($0.id) }
                    }.labelsHidden()
                    Picker("封装", selection: $model.container) { Text("MP4").tag("mp4"); Text("MKV").tag("mkv") }.pickerStyle(.segmented)
                }
                if model.mediaMode != "needed", !model.audioFormats.isEmpty {
                    Picker("音频规格", selection: $model.audioFormatID) {
                        ForEach(model.audioFormats) { Text($0.label).tag($0.id) }
                    }.labelsHidden()
                }
                Toggle("完成后保留媒体文件", isOn: $model.keepMedia).font(.callout)
                Text(model.keepMedia ? "媒体保存在本任务文件夹，可随时清理。" : "三份文字保存成功后清理媒体；失败或取消时保留进度。")
                    .font(.caption).foregroundStyle(.secondary)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }.font(.callout).disabled(model.isBusy)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(model.title).font(.title2.bold()).lineLimit(2).textSelection(.enabled)
            HStack(spacing: 8) {
                if !model.source.isEmpty {
                    Text(model.source)
                    Text("· \(model.segments.count) 段原稿")
                } else { Text(model.isRecording ? "保留原话、说话人和时间。" : "字幕优先，保留时间与出处。") }
                Spacer()
                if let job = model.currentJob {
                    if model.currentJobIsArchived { Label("已归档", systemImage: "archivebox").foregroundStyle(.teal) }
                    Menu { moveDestinations(job) } label: {
                        Label(model.library.project(for: job.id)?.name ?? "未归属", systemImage: "folder")
                    }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel("移动任务到项目")
                    Menu {
                        Button("在 Finder 中打开任务文件夹", action: model.revealJob)
                        if !model.isRecording { Button("清理本任务媒体（保留文字）", action: model.cleanMedia).disabled(model.isBusy) }
                        Divider()
                        taskLifecycleActions(job)
                    } label: { Image(systemName: "ellipsis.circle") }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel("任务操作").help("任务文件、归档与删除")
                } else if model.newTaskProjectID != nil {
                    Label("保存到「\(model.collectionTitle)」", systemImage: "folder")
                }
            }.font(.caption).foregroundStyle(.secondary)
            if model.currentJob != nil && !model.segments.isEmpty && !model.isRecording {
                Menu {
                    Button("全文理解与核验") { model.compareSummary(strategy: "structured") }
                    Button("旧版分块") { model.compareSummary(strategy: "legacy") }
                } label: { Label("新建对照总结", systemImage: "square.on.square") }
                    .disabled(model.isBusy || otherIsBusy || model.currentJobIsArchived)
            }
        }.padding(24)
    }

    private var emptyState: some View {
        VStack(spacing: 15) {
            Image(systemName: model.isBusy ? "waveform" : "text.book.closed").font(.system(size: 44, weight: .light)).foregroundStyle(.teal)
            Text(model.isBusy ? model.stageLabel : model.currentJobIsArchived ? "任务已归档" : model.currentJob != nil ? "任务已保存，可以继续处理" : model.isRecording ? "从一段录音开始" : model.metadata == nil ? "从一个视频开始" : "视频已准备好").font(.title2.bold())
            Text(model.isBusy ? "处理过程中可以取消；完成的阶段会留在本机。" : model.currentJobIsArchived ? "已完成的阶段保留在本机。恢复任务后可以继续处理。" : model.isRecording ? "导入手机录音，在本机提取原话和区分说话人。\n检查原稿后，再整理双方观点、问答与共识。" : "原始台词、精简台词与学习笔记分别保存。\n技术细节和来源时间一起保留，方便回看核对。")
                .multilineTextAlignment(.center).foregroundStyle(.secondary)
            if model.currentJobIsArchived, let job = model.currentJob {
                Button("恢复任务") { model.restoreJob(job) }.disabled(!model.canManageTasks)
            }
            if model.metadata == nil && !model.isBusy && model.currentJob == nil {
                Button(model.isRecording ? "选择录音文件" : "也可以导入字幕文件") { model.isRecording ? model.importRecording() : model.importSubtitle() }.disabled(otherIsBusy)
            }
        }.padding(24).frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
            .dropDestination(for: URL.self) { urls, _ in
                guard model.isRecording, !model.isBusy, !otherIsBusy, let url = urls.first else { return false }
                model.importRecording(url); return true
            }
    }

    private var readingContent: some View {
        VStack(alignment: .leading, spacing: 0) {
            Picker("阅读视图", selection: $model.selectedTab) {
                Text(model.isRecording ? "原始对话" : "原始台词").tag(0)
                Text(model.isRecording ? "整理对话" : "精简台词").tag(1)
                Text(model.isRecording ? "对话总结" : "学习笔记").tag(2)
            }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 420).padding(20)
            if model.isRecording {
                HStack(spacing: 14) {
                    SpeakerNameEditor(model: model, speaker: "A")
                    SpeakerNameEditor(model: model, speaker: "B")
                    Spacer()
                }.padding(.horizontal, 24).padding(.bottom, 12)
                if model.summaryStale {
                    Label("原稿已校订，当前整理稿与总结需重新生成。", systemImage: "exclamationmark.circle")
                        .font(.caption).foregroundStyle(.orange).padding(.horizontal, 24).padding(.bottom, 12)
                }
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 22) {
                    if model.selectedTab == 0 {
                        Text("来源文本原样保留。自动字幕与语音识别中的专名、数字仍可能有误。").font(.caption).foregroundStyle(.secondary)
                        ForEach(model.segments) { segment in
                            HStack(alignment: .top, spacing: 18) {
                                sourceButton([segment.id]).frame(width: 78, alignment: .leading)
                                if model.isRecording {
                                    VStack(alignment: .leading, spacing: 5) {
                                        Button { editingSegment = segment } label: {
                                            Label(model.speakerName(segment.speaker), systemImage: "person.crop.circle")
                                        }.buttonStyle(.borderless).font(.caption).foregroundStyle(segment.speaker == "B" ? .purple : .teal)
                                            .disabled(model.isBusy || model.currentJobIsArchived || !model.recordingEditsValid).help("校订这句原话与说话人")
                                        Text(segment.text).lineSpacing(6).textSelection(.enabled)
                                    }.frame(maxWidth: .infinity, alignment: .leading)
                                } else { Text(segment.text).lineSpacing(6).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                            }
                        }
                    } else if model.selectedTab == 1 {
                        if model.condensed.isEmpty { pendingText }
                        ForEach(Array(model.condensed.enumerated()), id: \.offset) { _, item in
                            VStack(alignment: .leading, spacing: 8) { sourceButton(item.source_ids); if model.isRecording { Text(model.speakerName(item.speaker)).font(.caption).foregroundStyle(.teal) }; Text(model.displayText(item.text)).lineSpacing(6).textSelection(.enabled) }
                        }
                    } else if let notes = model.notes {
                        notesView(notes)
                    } else { pendingText }
                }.font(.system(size: CGFloat(preferences.value.general.textSize)))
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 26).padding(.bottom, 24)
            }.id("\(model.currentJob?.id ?? model.title)-\(model.selectedTab)")
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var pendingText: some View {
        Text(model.isBusy ? "正在整理，原稿可以先行阅读。" : model.isRecording ? "检查原话与说话人后，点击“整理与总结”。" : "点击“生成学习笔记”或“继续处理”，整理这份原稿。")
            .foregroundStyle(.secondary).padding(.vertical, 24)
    }
    private func sourceButton(_ ids: [Int]) -> some View {
        let ranges = model.sourceRanges(ids)
        return ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) {
                ForEach(Array(ranges.enumerated()), id: \.offset) { _, range in
                    Button(model.sourceLabel(range)) { model.openSource(range) }
                }
            }
            VStack(alignment: .leading, spacing: 5) {
                ForEach(Array(ranges.enumerated()), id: \.offset) { _, range in
                    Button(model.sourceLabel(range)) { model.openSource(range) }
                }
            }
        }.buttonStyle(.link).font(.system(.caption, design: .monospaced)).tint(.teal)
            .help("分别回看每处原文的时间；跨章节引用保留独立入口。")
    }
    private func notesView(_ notes: StudyNotes) -> some View {
        VStack(alignment: .leading, spacing: 24) {
            HStack(spacing: 10) {
                Label(notes.summaryStrategy == "structured" ? "全文理解" : notes.summaryStrategy == "compact" ? "紧凑输入对照" : "旧版分块", systemImage: "text.book.closed")
                if let read = notes.reviewedParagraphs, let count = notes.paragraphCount {
                    Text("核验已读 \(read)/\(count) 段")
                }
                if let count = notes.rereadCount, count > 0 { Text("定向回查 \(count) 处") }
            }.font(.caption).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 10) {
                Label("概览", systemImage: "sparkles").font(.headline).foregroundStyle(.teal)
                Text(model.displayText(notes.overview)).lineSpacing(7).textSelection(.enabled)
            }.padding(20).frame(maxWidth: .infinity, alignment: .leading).background(Color.teal.opacity(0.07), in: RoundedRectangle(cornerRadius: 12))
            Text(model.isRecording ? "主题与双方观点" : "分章节学习笔记").font(.title3.bold())
            ForEach(Array(notes.chapters.enumerated()), id: \.offset) { index, item in
                VStack(alignment: .leading, spacing: 10) {
                    Text("\(index + 1). \(model.displayText(item.title))").font(.headline)
                    if let claims = item.claims, !claims.isEmpty {
                        ForEach(Array(claims.enumerated()), id: \.offset) { _, claim in
                            VStack(alignment: .leading, spacing: 5) {
                                Text(model.displayText(claim.text)).lineSpacing(7).textSelection(.enabled)
                                sourceButton(claim.source_ids)
                            }
                        }
                    } else {
                        Text(model.displayText(item.detail)).lineSpacing(7).textSelection(.enabled)
                        sourceButton(item.source_ids)
                    }
                }
            }
            Divider()
            Text(model.isRecording ? "共识与主要结论" : "关键结论").font(.title3.bold())
            ForEach(Array(notes.key_points.enumerated()), id: \.offset) { _, item in
                VStack(alignment: .leading, spacing: 8) { Text(model.displayText(item.text)).lineSpacing(6).textSelection(.enabled); sourceButton(item.source_ids) }
            }
            if !notes.uncertainties.isEmpty {
                Text("待核对内容").font(.title3.bold())
                ForEach(Array(notes.uncertainties.enumerated()), id: \.offset) { _, item in
                    VStack(alignment: .leading, spacing: 8) { Text(model.displayText(item.text)).lineSpacing(6).textSelection(.enabled); sourceButton(item.source_ids) }
                }
            }
            Text("依据来源文字整理 · \(notes.model)\n" + (model.isRecording ? "忠实整理对话中的观点，不将个人说法视为已核实事实。" : "视频中的新闻、观点和推测未做外部事实核查。")).font(.caption).foregroundStyle(.secondary)
        }
    }
    private var footer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if model.isBusy, let progress = model.progress { ProgressView(value: progress).tint(.teal) }
            HStack(spacing: 10) {
                if model.isBusy { ProgressView().controlSize(.small) }
                else { Image(systemName: model.currentJob?.state == "completed" ? "checkmark.circle.fill" : "circle.fill").foregroundStyle(.teal).font(.caption) }
                Text(model.status).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                Spacer()
                if model.isBusy { Button("取消", action: model.cancel).buttonStyle(.borderless) }
            }
        }.padding(14)
    }
}
