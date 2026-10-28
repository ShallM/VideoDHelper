import SwiftUI
import AppKit
import VideoCore

struct SettingsCard<Content: View>: View {
    let title: String
    var subtitle: String = ""
    var icon: String = ""
    @ViewBuilder let content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 10) {
                if !icon.isEmpty { Image(systemName: icon).foregroundStyle(.teal).font(.title3) }
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.headline)
                    if !subtitle.isEmpty { Text(subtitle).font(.caption).foregroundStyle(.secondary) }
                }
            }
            content
        }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(.primary.opacity(0.06)))
    }
}

struct SettingsRow<Content: View>: View {
    let title: String
    var detail: String = ""
    @ViewBuilder let content: Content
    var body: some View {
        HStack(spacing: 24) {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.callout.weight(.medium))
                if !detail.isEmpty { Text(detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
            }
            Spacer(minLength: 12)
            content.frame(maxWidth: 290, alignment: .trailing)
        }
    }
}

struct SettingsView: View {
    @ObservedObject var store: PreferencesStore
    @StateObject private var editor: PreferencesEditor
    @State private var showsProviderEditor = false
    @State private var modelEditor: ModelEditorContext?
    @State private var removingProvider: ModelProvider?
    @State private var showsLogin = false
    @State private var showsKey = false
    init(store: PreferencesStore) { self.store = store; _editor = StateObject(wrappedValue: PreferencesEditor(store: store)) }

    var body: some View {
        HStack(spacing: 0) {
            navigation
            Divider()
            VStack(spacing: 0) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 7) {
                        Text(store.page.title).font(.system(size: 25, weight: .bold))
                        Text(pageDescription).font(.callout).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if editor.isDirty { Text("未保存更改").font(.caption.weight(.medium)).padding(.horizontal, 10).padding(.vertical, 6).background(.orange.opacity(0.12), in: Capsule()).foregroundStyle(.orange) }
                }.padding(26)
                if let error = store.loadError { feedback(error, error: true) }
                if let message = editor.feedback { feedback(message, error: editor.feedbackIsError) }
                if editor.isConnecting {
                    HStack { ProgressView().controlSize(.small); Text(editor.operation).font(.callout); Spacer(); Button("取消", action: editor.cancelConnection) }
                        .padding(.horizontal, 26).padding(.bottom, 12)
                }
                Group {
                    switch store.page {
                    case .general: generalPage
                    case .models: modelsPage
                    case .usage: UsageDashboard(editor: editor, preferences: store.value)
                    }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
                Divider()
                HStack {
                    Text(editor.isDirty ? "保存后应用新设置；当前运行保持原配置。" : "设置保存在本机。API Key 使用私有文件权限保存。")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("撤销更改", action: editor.resetDraft).disabled(!editor.isDirty || editor.isConnecting)
                    Button("保存设置", action: editor.save).buttonStyle(.borderedProminent).tint(.teal)
                        .keyboardShortcut("s", modifiers: .command).disabled(!editor.isDirty || !store.writable || editor.isConnecting)
                }.padding(18)
            }.background(Color(nsColor: .windowBackgroundColor))
        }.frame(minWidth: 1040, idealWidth: 1160, minHeight: 740, idealHeight: 820)
            .preferredColorScheme(store.colorScheme)
            .sheet(isPresented: $showsProviderEditor) { NewProviderSheet { editor.addProvider($0) } }
            .sheet(item: $modelEditor) { context in
                ModelEditorSheet(model: context.model, otherIDs: editor.draft.providers.first { $0.id == context.providerID }?.models.filter { $0.id != context.model.id }.map(\.modelID) ?? []) {
                    editor.upsertModel($0, providerID: context.providerID)
                }
            }
            .sheet(isPresented: $editor.showsCatalog) { catalogSheet }
            .sheet(isPresented: $showsLogin) {
                BilibiliLoginView(onSaved: {
                    showsLogin = false; editor.show("B 站登录会话已保存在本机。")
                    NotificationCenter.default.post(name: .videoPreferencesChanged, object: nil)
                })
            }
            .confirmationDialog("移除服务商", isPresented: Binding(get: { removingProvider != nil }, set: { if !$0 { removingProvider = nil } })) {
                Button("移除服务商", role: .destructive) { if let provider = removingProvider { editor.removeProvider(provider.id) }; removingProvider = nil }
            } message: { Text("从配置中移除「\(removingProvider?.name ?? "")」及其模型。已有任务和用量记录会保留，保存设置后生效。") }
            .onChange(of: store.page) { _, page in if page == .usage { editor.refreshUsage() } }
            .onChange(of: editor.selectedProviderID) { _, _ in showsKey = false; editor.feedback = nil }
            .onAppear { if store.page == .usage { editor.refreshUsage() } }
            .onDisappear { editor.cancelConnection() }
    }

    private var navigation: some View {
        VStack(alignment: .leading, spacing: 24) {
            HStack(spacing: 11) {
                Image(systemName: "play.rectangle.on.rectangle.fill").font(.system(size: 26)).foregroundStyle(.teal)
                VStack(alignment: .leading, spacing: 3) { Text("VideoDHelper").font(.headline); Text("设置").font(.caption).foregroundStyle(.secondary) }
            }.padding(.top, 10)
            VStack(spacing: 6) {
                ForEach(SettingsPage.allCases) { page in
                    Button { store.page = page; editor.feedback = nil } label: {
                        HStack(spacing: 12) { Image(systemName: page.icon).frame(width: 20); Text(page.title); Spacer() }
                            .font(.callout.weight(store.page == page ? .semibold : .regular)).padding(.horizontal, 12).padding(.vertical, 11)
                            .foregroundStyle(store.page == page ? Color.teal : Color.primary)
                            .background(store.page == page ? Color.teal.opacity(0.12) : .clear, in: RoundedRectangle(cornerRadius: 9))
                    }.buttonStyle(.plain)
                }
            }
            Spacer()
            VStack(alignment: .leading, spacing: 7) {
                Label("本机工作空间", systemImage: "internaldrive").font(.caption.weight(.medium))
                Text("VideoDHelper \(AppInfo.version)\nApple Silicon · macOS").font(.caption2).foregroundStyle(.secondary).lineSpacing(4)
            }
        }.padding(20).frame(width: 188).frame(maxHeight: .infinity).background(.thinMaterial)
    }
    private var pageDescription: String {
        switch store.page {
        case .general: return "为新任务选择默认处理方式，调整阅读与本机存储。"
        case .models: return "管理服务商、可用模型和价格，让每次调用都有清晰归属。"
        case .usage: return "查看实际请求用量，按价格表估算费用。"
        }
    }
    private func feedback(_ text: String, error: Bool) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: error ? "exclamationmark.circle" : "checkmark.circle")
            Text(text).font(.callout).textSelection(.enabled)
            Spacer()
        }.foregroundStyle(error ? Color.orange : Color.teal).padding(12)
            .background((error ? Color.orange : Color.teal).opacity(0.08), in: RoundedRectangle(cornerRadius: 9))
            .padding(.horizontal, 26).padding(.bottom, 12)
    }

    private var generalPage: some View {
        ScrollView {
            VStack(spacing: 18) {
                SettingsCard(title: "新任务默认值", subtitle: "每个视频仍可单独调整。", icon: "play.square.stack") {
                    SettingsRow(title: "文字来源", detail: "优先字幕通常更快；无字幕时本机转录。") {
                        Picker("默认文字来源", selection: $editor.draft.general.transcriptionMode) { Text("优先平台字幕").tag("auto"); Text("强制本地 Whisper").tag("whisper") }.labelsHidden()
                    }
                    Divider()
                    SettingsRow(title: "本地转录模型", detail: "优先复用已有权重，使用 MLX / Metal 加速。") {
                        Picker("默认转录模型", selection: $editor.draft.general.whisperModel) { Text("Large v3 Turbo · 更高质量").tag("turbo"); Text("Small · 较小模型").tag("small") }.labelsHidden()
                    }
                    Divider()
                    SettingsRow(title: "媒体获取") {
                        Picker("默认媒体获取", selection: $editor.draft.general.mediaMode) { Text("仅获取笔记所需内容").tag("needed"); Text("同时下载音频").tag("audio"); Text("同时下载视频").tag("video") }.labelsHidden()
                    }
                    SettingsRow(title: "视频封装") {
                        Picker("默认封装", selection: $editor.draft.general.container) { Text("MP4").tag("mp4"); Text("MKV").tag("mkv") }.labelsHidden().pickerStyle(.segmented).frame(width: 180)
                    }
                    Divider()
                    SettingsRow(title: "完成后保留媒体", detail: "关闭时，三份文字成功保存后清理媒体；中断仍保留进度。") {
                        Toggle("默认保留媒体", isOn: $editor.draft.general.keepMedia).labelsHidden().toggleStyle(.switch)
                    }
                    SettingsRow(title: "模型请求并行数", detail: "服务商有限流时可改为 1。") {
                        Picker("并行请求数", selection: $editor.draft.general.summaryConcurrency) { Text("1 · 顺序处理").tag(1); Text("2 · 更快完成").tag(2) }.labelsHidden()
                    }
                    SettingsRow(title: "默认总结方式", detail: "仅影响新任务。已有任务可创建对照总结，保留两份结果。") {
                        Picker("默认总结方式", selection: $editor.draft.general.effectiveSummaryStrategy) {
                            Text("全文理解与核验").tag("structured")
                            Text("旧版分块 · 对照").tag("legacy")
                        }.labelsHidden()
                    }
                }
                SettingsCard(title: "阅读与外观", icon: "textformat.size") {
                    SettingsRow(title: "外观") {
                        Picker("外观模式", selection: $editor.draft.general.appearance) { Text("跟随系统").tag("system"); Text("浅色").tag("light"); Text("深色").tag("dark") }.labelsHidden()
                    }
                    SettingsRow(title: "打开任务时显示") {
                        Picker("默认阅读视图", selection: $editor.draft.general.readingTab) { Text("原始台词").tag(0); Text("精简台词").tag(1); Text("学习笔记").tag(2) }.labelsHidden()
                    }
                    SettingsRow(title: "正文字号") {
                        Picker("正文字号", selection: $editor.draft.general.textSize) { Text("紧凑 · 13").tag(13); Text("标准 · 15").tag(15); Text("舒适 · 17").tag(17); Text("大字 · 19").tag(19) }.labelsHidden()
                    }
                }
                SettingsCard(title: "本机数据", subtitle: "原稿、笔记、媒体与模型均保存在此工作空间。", icon: "internaldrive") {
                    HStack { Text(AppStorage.root.path).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled); Spacer(); Button("在 Finder 中打开") { NSWorkspace.shared.open(AppStorage.root) } }
                    Divider()
                    SettingsRow(title: "B 站登录", detail: FileManager.default.fileExists(atPath: BilibiliCookies.fileURL.path) ? "已保存本机登录会话" : "尚未保存登录会话") {
                        Button("登录或更新会话") { showsLogin = true }
                    }
                }
                HStack { Button("恢复通用默认值") { editor.draft.general = GeneralPreferences() }.buttonStyle(.link); Spacer() }
            }.padding(.horizontal, 26).padding(.bottom, 26).disabled(!store.writable)
        }
    }

    private var modelsPage: some View {
        HStack(alignment: .top, spacing: 20) {
            VStack(alignment: .leading, spacing: 14) {
                HStack { Text("服务商").font(.caption.weight(.semibold)).foregroundStyle(.secondary); Spacer(); Button { showsProviderEditor = true } label: { Image(systemName: "plus") }.buttonStyle(.borderless).accessibilityLabel("添加服务商") }
                ScrollView {
                    VStack(spacing: 7) {
                        ForEach(editor.draft.providers) { provider in
                            Button { editor.selectedProviderID = provider.id } label: {
                                HStack(spacing: 9) {
                                    Text(String(provider.name.prefix(1))).font(.headline).frame(width: 30, height: 30).background(.teal.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(provider.name).font(.callout.weight(.medium)).lineLimit(1)
                                        Text("\(provider.models.count) 个模型 · \(provider.enabled ? "已启用" : "已停用")").font(.caption2).foregroundStyle(.secondary)
                                    }
                                    Spacer(minLength: 0)
                                    if provider.id == editor.draft.defaultProviderID { Image(systemName: "checkmark.circle.fill").foregroundStyle(.teal).font(.caption).accessibilityHidden(true) }
                                }.padding(10).background(provider.id == editor.selectedProviderID ? Color.teal.opacity(0.09) : .clear, in: RoundedRectangle(cornerRadius: 10))
                            }.buttonStyle(.plain).accessibilityAddTraits(provider.id == editor.selectedProviderID ? .isSelected : [])
                        }
                    }
                }
            }.frame(width: 190)
            if let index = editor.draft.providers.firstIndex(where: { $0.id == editor.selectedProviderID }) {
                providerDetail(index)
            } else { ContentUnavailableView("添加一个服务商", systemImage: "network", description: Text("配置 API 地址与 Key 后，添加要使用的模型。")) }
        }.padding(.horizontal, 26).padding(.bottom, 20).disabled(!store.writable)
    }
    private func providerDetail(_ index: Int) -> some View {
        let provider = editor.draft.providers[index]
        return ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    VStack(alignment: .leading, spacing: 5) { Text(provider.name).font(.title2.bold()); Text("OpenAI 兼容 · Chat Completions").font(.caption).foregroundStyle(.secondary) }
                    Spacer()
                    Toggle("启用服务商", isOn: $editor.draft.providers[index].enabled).toggleStyle(.switch).labelsHidden()
                        .disabled(provider.id == editor.draft.defaultProviderID).help(provider.id == editor.draft.defaultProviderID ? "切换默认模型后可停用此服务商" : "启用或停用")
                }
                SettingsCard(title: "连接", subtitle: "只在同步模型或测试时发送请求。") {
                    VStack(alignment: .leading, spacing: 7) { Text("显示名称").font(.caption).foregroundStyle(.secondary); TextField("服务商名称", text: $editor.draft.providers[index].name).accessibilityLabel("服务商名称") }
                    VStack(alignment: .leading, spacing: 7) {
                        Text("API 根地址").font(.caption).foregroundStyle(.secondary)
                        TextField("https://example.com/v1", text: $editor.draft.providers[index].baseURL).accessibilityLabel("API 根地址")
                        Text((try? APIEndpoint.url(provider.baseURL, path: "chat/completions").absoluteString) ?? "填写到 /v1 或服务商指定的 API 根地址。")
                            .font(.caption2.monospaced()).foregroundStyle(.tertiary).textSelection(.enabled).lineLimit(2)
                    }
                    VStack(alignment: .leading, spacing: 7) {
                        HStack { Text("API Key").font(.caption).foregroundStyle(.secondary); Spacer(); Text(provider.apiKey.isEmpty ? "未填写" : "已配置").font(.caption2).foregroundStyle(.secondary) }
                        HStack {
                            if showsKey { TextField("粘贴 API Key", text: $editor.draft.providers[index].apiKey).accessibilityLabel("API Key") }
                            else { SecureField("粘贴 API Key", text: $editor.draft.providers[index].apiKey).accessibilityLabel("API Key") }
                            Button { showsKey.toggle() } label: { Image(systemName: showsKey ? "eye.slash" : "eye") }.buttonStyle(.borderless).accessibilityLabel(showsKey ? "隐藏 Key" : "显示 Key")
                        }
                    }
                }.textFieldStyle(.roundedBorder).disabled(editor.isConnecting)
                HStack {
                    Text("模型").font(.headline); Text("\(provider.models.count)").font(.caption).foregroundStyle(.secondary); Spacer()
                    Button(action: editor.syncModels) { Label("同步模型", systemImage: "arrow.triangle.2.circlepath") }.disabled(editor.isConnecting)
                    Button { modelEditor = ModelEditorContext(providerID: provider.id, model: LanguageModel(modelID: "")) } label: { Image(systemName: "plus") }.accessibilityLabel("手动添加模型")
                }
                if provider.models.isEmpty {
                    VStack(spacing: 9) { Image(systemName: "square.stack.3d.up").font(.largeTitle).foregroundStyle(.tertiary); Text("选择你要使用的模型").font(.headline); Text("同步服务商列表，或手动输入模型 ID。").font(.caption).foregroundStyle(.secondary) }.frame(maxWidth: .infinity).padding(30)
                }
                ForEach(provider.models) { model in
                    modelCard(model, provider: provider)
                }
                Text("测试会发起一次模型调用，并计入用量；较高思考强度可能消耗更多输出 Tokens。测试通过表示请求被接收，不保证服务商后台执行了对应强度。").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
                if provider.id != editor.draft.defaultProviderID {
                    Button("移除此服务商…", role: .destructive) { removingProvider = provider }.buttonStyle(.link).padding(.top, 6)
                }
            }.padding(.bottom, 12)
        }
    }
    private func modelCard(_ model: LanguageModel, provider: ModelProvider) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 5) { Text(model.name).font(.callout.weight(.semibold)); Text(model.modelID).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled) }
                Spacer()
                if model.id == editor.draft.defaultModelID { Text("默认").font(.caption.weight(.medium)).foregroundStyle(.teal).padding(.horizontal, 9).padding(.vertical, 4).background(.teal.opacity(0.1), in: Capsule()) }
            }
            HStack {
                Label("思考强度", systemImage: "brain").font(.caption).foregroundStyle(.secondary)
                Picker("\(model.name) 的思考强度", selection: Binding(get: { model.reasoningEffort }, set: { effort in
                    guard let p = editor.draft.providers.firstIndex(where: { $0.id == provider.id }),
                          let m = editor.draft.providers[p].models.firstIndex(where: { $0.id == model.id }) else { return }
                    editor.draft.providers[p].models[m].reasoningEffort = effort
                })) {
                    ForEach(ReasoningEffort.options(for: model.modelID)) { effort in Text(effort.label).tag(effort.rawValue) }
                }.labelsHidden().frame(width: 145)
                Spacer()
            }.controlSize(.small)
            HStack {
                Label(model.pricing.isConfigured ? "\(model.pricing.currency) · 已配置单价" : "未配置价格", systemImage: "tag").font(.caption).foregroundStyle(.secondary)
                Spacer()
                if model.id != editor.draft.defaultModelID { Button("设为默认") { editor.makeDefault(providerID: provider.id, modelID: model.id) }.disabled(!provider.enabled) }
                Button("测试") { editor.test(model, provider: provider) }.disabled(editor.isConnecting)
                Button { modelEditor = ModelEditorContext(providerID: provider.id, model: model) } label: { Label("编辑模型与价格", systemImage: "slider.horizontal.3") }.accessibilityLabel("编辑模型与价格 \(model.name)")
                Menu {
                    Button("移除模型", role: .destructive) { editor.removeModel(model.id, providerID: provider.id) }.disabled(model.id == editor.draft.defaultModelID)
                } label: { Image(systemName: "ellipsis") }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel("模型更多操作")
            }.controlSize(.small)
        }.padding(16).background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(model.id == editor.draft.defaultModelID ? Color.teal.opacity(0.3) : Color.primary.opacity(0.07)))
    }

    private var catalogSheet: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("添加服务商模型").font(.title2.bold())
            Text("\(editor.draft.providers.first { $0.id == editor.catalogProviderID }?.name ?? "服务商") · 同步到 \(editor.catalog.count) 个模型，勾选后加入你的模型列表。").font(.callout).foregroundStyle(.secondary)
            TextField("搜索模型 ID", text: $editor.catalogSearch).textFieldStyle(.roundedBorder)
            let existing = Set(editor.draft.providers.first { $0.id == editor.catalogProviderID }?.models.map(\.modelID) ?? [])
            let visible = editor.catalog.filter { editor.catalogSearch.isEmpty || $0.localizedCaseInsensitiveContains(editor.catalogSearch) }
            List(visible, id: \.self) { id in
                HStack {
                    Toggle(id, isOn: Binding(get: { existing.contains(id) || editor.catalogSelection.contains(id) }, set: { if $0 { editor.catalogSelection.insert(id) } else { editor.catalogSelection.remove(id) } })).disabled(existing.contains(id))
                    Spacer()
                    if existing.contains(id) { Text("已添加").font(.caption).foregroundStyle(.secondary) }
                }.padding(.vertical, 4)
            }.listStyle(.inset).overlay { if visible.isEmpty { ContentUnavailableView("没有匹配模型", systemImage: "magnifyingglass", description: Text("可手动添加服务商提供的模型 ID。")) } }
            HStack { Text("已选择 \(editor.catalogSelection.count) 个").font(.caption).foregroundStyle(.secondary); Spacer(); Button("取消") { editor.showsCatalog = false }; Button("添加所选", action: editor.addCatalogSelection).buttonStyle(.borderedProminent).disabled(editor.catalogSelection.isEmpty) }
        }.padding(26).frame(width: 590, height: 540)
    }
}

struct ModelEditorContext: Identifiable {
    let providerID: String
    let model: LanguageModel
    var id: String { model.id }
}

struct NewProviderSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var baseURL = ""
    @State private var error: String?
    let onAdd: (ModelProvider) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("添加服务商").font(.title2.bold())
            Text("支持 OpenAI 兼容接口。Key 和模型可在下一步配置。").font(.callout).foregroundStyle(.secondary)
            TextField("显示名称，例如我的模型服务", text: $name).accessibilityLabel("新服务商名称")
            TextField("API 根地址，例如 https://example.com/v1", text: $baseURL).accessibilityLabel("新服务商 API 地址")
            if let error { Text(error).font(.caption).foregroundStyle(.orange) }
            HStack {
                Spacer(); Button("取消") { dismiss() }
                Button("添加服务商") {
                    do {
                        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !name.isEmpty, name.count <= 60 else { throw VideoError.toolFailed("请输入 1～60 个字符的名称。") }
                        let base = try APIEndpoint.base(baseURL)
                        onAdd(ModelProvider(name: name, baseURL: base.absoluteString)); dismiss()
                    } catch { self.error = error.localizedDescription }
                }.buttonStyle(.borderedProminent)
            }
        }.textFieldStyle(.roundedBorder).padding(28).frame(width: 510)
    }
}

struct ModelEditorSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var draft: LanguageModel
    @State private var inputPrice: String
    @State private var outputPrice: String
    @State private var cachedPrice: String
    @State private var error: String?
    let otherIDs: [String]
    let onSave: (LanguageModel) -> Void
    init(model: LanguageModel, otherIDs: [String], onSave: @escaping (LanguageModel) -> Void) {
        _draft = State(initialValue: model); self.otherIDs = otherIDs; self.onSave = onSave
        _inputPrice = State(initialValue: model.pricing.inputPerMillion.map { String($0) } ?? "")
        _outputPrice = State(initialValue: model.pricing.outputPerMillion.map { String($0) } ?? "")
        _cachedPrice = State(initialValue: model.pricing.cachedInputPerMillion.map { String($0) } ?? "")
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack { Image(systemName: "cpu").font(.title2).foregroundStyle(.teal); Text(draft.modelID.isEmpty ? "添加模型" : "模型配置").font(.title2.bold()) }
            Form {
                TextField("模型 ID", text: $draft.modelID)
                TextField("显示名称", text: $draft.name)
                Picker("思考强度", selection: $draft.reasoningEffort) {
                    ForEach(ReasoningEffort.options(for: draft.modelID)) { effort in
                        Text(effort == .providerDefault ? effort.label : "\(effort.label) · \(effort.rawValue)").tag(effort.rawValue)
                    }
                    if !ReasoningEffort.options(for: draft.modelID).contains(where: { $0.rawValue == draft.reasoningEffort }) {
                        Text("\(draft.reasoningEffortLabel) · 请重新选择").tag(draft.reasoningEffort)
                    }
                }
            }
            VStack(alignment: .leading, spacing: 6) {
                Text(ReasoningEffort(rawValue: draft.reasoningEffort)?.detail ?? "请重新选择思考强度。")
                Text(ReasoningEffort.isDeepSeekV4(draft.modelID)
                     ? "DeepSeek V4 官方档位为低、高、最高；中档会映射为高。通过代理调用时，仍需核对服务商支持情况。"
                     : "这是 OpenAI 兼容接口的请求选项，并非已验证的模型能力。不支持时会明确报错，可自行改用服务商默认。")
            }.font(.caption).foregroundStyle(.secondary).lineSpacing(3)
            SettingsCard(title: "价格表", subtitle: "每百万 Tokens 的单价。留空为未配置，填 0 表示免费。", icon: "tag") {
                HStack { Text("币种").font(.callout); Spacer(); Picker("计费币种", selection: $draft.pricing.currency) { Text("人民币 CNY").tag("CNY"); Text("美元 USD").tag("USD"); Text("欧元 EUR").tag("EUR"); Text("港币 HKD").tag("HKD"); Text("日元 JPY").tag("JPY") }.labelsHidden().frame(width: 180) }
                HStack(spacing: 16) { priceField("输入", text: $inputPrice, placeholder: "未配置"); priceField("输出", text: $outputPrice, placeholder: "未配置") }
                priceField("缓存命中输入", text: $cachedPrice, placeholder: "留空时按普通输入单价")
                Text("缓存 Token 已包含在输入中；推理 Token 已包含在输出中，计费时不会重复相加。新调用保存价格快照，改价不会改写旧记录。").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
            }
            if let error { Text(error).font(.caption).foregroundStyle(.orange) }
            HStack { Spacer(); Button("取消") { dismiss() }; Button("完成", action: save).buttonStyle(.borderedProminent).tint(.teal) }
        }.textFieldStyle(.roundedBorder).padding(28).frame(width: 570)
    }
    private func priceField(_ title: String, text: Binding<String>, placeholder: String) -> some View {
        VStack(alignment: .leading, spacing: 7) { Text("\(title) / 百万 Tokens").font(.caption).foregroundStyle(.secondary); TextField(placeholder, text: text).accessibilityLabel("\(title)单价") }
    }
    private func save() {
        do {
            var model = draft
            model.modelID = model.modelID.trimmingCharacters(in: .whitespacesAndNewlines)
            model.name = model.name.trimmingCharacters(in: .whitespacesAndNewlines)
            if model.name.isEmpty { model.name = model.modelID }
            guard !model.modelID.isEmpty, model.modelID.count <= 200, model.name.count <= 100, !otherIDs.contains(model.modelID) else { throw VideoError.toolFailed("请输入不重复的模型 ID（最多 200 字符），名称最多 100 字符。") }
            _ = try ReasoningEffort.validated(model.reasoningEffort, modelID: model.modelID)
            model.pricing = try TokenPricing.fromInput(currency: model.pricing.currency, input: inputPrice, output: outputPrice, cached: cachedPrice)
            onSave(model); dismiss()
        } catch { self.error = error.localizedDescription }
    }
}
