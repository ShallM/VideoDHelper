import SwiftUI
import AppKit
import VideoCore

struct ASRSettingsView: View {
    @ObservedObject var preferences: PreferencesStore
    @ObservedObject var editor: PreferencesEditor
    @ObservedObject private var models = ASRModelStore.shared
    @State private var removing: LocalASRModel?
    @State private var clearing: LocalASRModel?
    @State private var expanded = Set<String>()
    @State private var showsLegacy = false

    private func bytes(_ count: Int64) -> String {
        count <= 0 ? "0 B" : ByteCountFormatter.string(fromByteCount: count, countStyle: .file)
    }
    private var defaultID: String { preferences.value.general.effectiveASRModelID }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                summary
                if preferences.value.general.asrLegacyNoticePending == true {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: "info.circle").foregroundStyle(.teal)
                        Text("新任务默认模型已从旧版 Small 调整为 Turbo。旧任务仍使用原型号，Small 文件未被删除。")
                            .font(.callout).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 6)
                        Button("知道了", action: editor.dismissASRMigrationNotice).disabled(!preferences.writable)
                    }.padding(14).background(.teal.opacity(0.07), in: RoundedRectangle(cornerRadius: 12))
                }
                if !ASRModelCatalog.primaryIDs.contains(defaultID) {
                    Label("当前默认型号无法在此版本使用，请从下方已安装模型中重新选择。", systemImage: "exclamationmark.triangle")
                        .font(.callout).foregroundStyle(.orange)
                }
                if let message = models.feedback {
                    Label(message, systemImage: models.feedbackIsError ? "exclamationmark.circle" : "checkmark.circle")
                        .font(.callout).foregroundStyle(models.feedbackIsError ? Color.orange : .teal)
                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
                if models.models.isEmpty {
                    ContentUnavailableView("正在读取模型目录", systemImage: "waveform", description: Text("模型目录随应用提供；下载权重前不会开始转录。"))
                        .frame(minHeight: 180)
                }
                ForEach(models.primaryModels) { model in modelCard(model) }
                if !models.legacyModels.isEmpty {
                    DisclosureGroup(isExpanded: $showsLegacy) {
                        VStack(spacing: 14) {
                            Text("仅用于恢复旧任务，不作为新任务默认选项。删除模型不影响已经保存的文字成果。")
                                .font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
                            ForEach(models.legacyModels) { model in modelCard(model) }
                        }.padding(.top, 12)
                    } label: { Label("旧版本资源", systemImage: "archivebox").font(.callout.weight(.medium)) }
                        .padding(18).background(.primary.opacity(0.025), in: RoundedRectangle(cornerRadius: 12))
                }
                Text("关闭设置后下载继续；退出应用会暂停，重开后可继续下载。识别在本机使用 MLX / Metal 加速，不消耗大语言模型 Tokens。说话人识别组件另行按需准备；安装模型不会自动开始转录。")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }.padding(.horizontal, 26).padding(.bottom, 26)
        }
        .task {
            models.refresh()
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 5_000_000_000) }
                catch { break }
                models.refresh()
            }
        }
        .confirmationDialog("删除本地模型", isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }), titleVisibility: .visible) {
            Button("删除模型", role: .destructive) { if let model = removing { models.remove(model.id) }; removing = nil }
            Button("取消", role: .cancel) { removing = nil }
        } message: {
            if let model = removing {
                Text("删除「\(model.name)」及应用管理的相关下载文件，预计释放 \(bytes(model.reclaimableBytes))。录音、原稿、笔记和外部共享缓存会保留。\(model.id == defaultID ? "它仍是默认选择，使用前需重新下载或选择其他模型。" : "已有任务继续转录时可能需要重新下载。")")
            }
        }
        .confirmationDialog("清理未完成下载", isPresented: Binding(get: { clearing != nil }, set: { if !$0 { clearing = nil } }), titleVisibility: .visible) {
            Button("取消并清理", role: .destructive) { if let model = clearing { models.clear(model.id) }; clearing = nil }
            Button("保留进度", role: .cancel) { clearing = nil }
        } message: { Text("仅清理「\(clearing?.name ?? "")」未完成的下载文件。下次下载将重新获取已清理的部分。") }
    }

    private var summary: some View {
        HStack(alignment: .center, spacing: 18) {
            Image(systemName: "waveform.circle.fill").font(.system(size: 37)).foregroundStyle(.teal)
            VStack(alignment: .leading, spacing: 5) {
                Text("新任务默认 · \(models.name(defaultID))").font(.headline)
                Text("已安装 \(models.installedCount) 套 · 占用 \(bytes(models.occupiedBytes))")
                    .font(.caption).foregroundStyle(.secondary)
                if let current = models.record(defaultID), !current.isReady {
                    Text("默认模型尚不可用；下载完成后可开始本地转录。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 8)
            if models.isRefreshing { ProgressView().controlSize(.small) }
            Button { models.refresh() } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.borderless).disabled(models.isRefreshing).help("刷新本地模型状态").accessibilityLabel("刷新本地模型状态")
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(.teal.opacity(0.065), in: RoundedRectangle(cornerRadius: 14))
    }

    private func modelCard(_ model: LocalASRModel) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: model.components.count > 1 ? "waveform.badge.magnifyingglass" : "waveform")
                    .font(.title2).foregroundStyle(.teal).frame(width: 38, height: 38)
                    .background(.teal.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 9) {
                        Text(model.name).font(.headline)
                        if model.id == defaultID { badge("新任务默认", color: .teal) }
                        if model.legacy { badge("旧版本", color: .secondary) }
                    }
                    Text(model.subtitle).font(.callout).foregroundStyle(.secondary)
                    HStack(spacing: 12) {
                        Label(models.isQueued(model.id) ? "排队中" : model.state.title,
                              systemImage: stateIcon(model))
                            .foregroundStyle(model.isReady ? Color.teal : Color.secondary)
                        Text(model.isReady ? "本机占用 \(bytes(model.installedBytes))" : "下载约 \(bytes(model.downloadBytes))")
                    }.font(.caption)
                }
                Spacer(minLength: 12)
                actions(model)
            }
            if model.isInUse {
                Label("正在转录 · \(model.inUseTask ?? "当前任务")，完成后可删除或修复。", systemImage: "waveform.path")
                    .font(.caption).foregroundStyle(.teal).fixedSize(horizontal: false, vertical: true)
            }
            if models.activeID == model.id || [.downloading, .verifying].contains(model.state) {
                VStack(alignment: .leading, spacing: 7) {
                    if let progress = model.progress { ProgressView(value: progress).tint(.teal) }
                    else { ProgressView().progressViewStyle(.linear).tint(.teal) }
                    HStack {
                        Text(model.message.isEmpty ? "准备模型…" : model.message)
                        Spacer()
                        if let downloaded = model.downloadedBytes, let total = model.totalBytes, total > 0 {
                            Text("\(bytes(downloaded)) / \(bytes(total))").monospacedDigit()
                        }
                    }.font(.caption).foregroundStyle(.secondary)
                }
            } else if !model.message.isEmpty && [.failed, .repair, .paused].contains(model.state) {
                Text(model.message).font(.caption).foregroundStyle(model.state == .paused ? Color.secondary : .orange)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            DisclosureGroup(isExpanded: Binding(get: { expanded.contains(model.id) }, set: { if $0 { expanded.insert(model.id) } else { expanded.remove(model.id) } })) {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(model.components) { component in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack { Text(component.name).font(.caption.weight(.medium)); Spacer(); Text(bytes(component.bytes)).font(.caption).foregroundStyle(.secondary) }
                            Text("来源：\(component.repository)\n版本：\(component.revision)").font(.caption2.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                        }
                    }
                    Text("许可：\(model.license)").font(.caption).foregroundStyle(.secondary)
                    if !model.path.isEmpty {
                        HStack(alignment: .top) {
                            Text(model.path).font(.caption2.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                            Spacer()
                            Button("在访达显示") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: model.path)]) }
                                .font(.caption).disabled(!FileManager.default.fileExists(atPath: model.path))
                        }
                    }
                }.padding(.top, 10)
            } label: { Text("组件与存储详情").font(.caption).foregroundStyle(.secondary) }
        }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(model.id == defaultID ? Color.teal.opacity(0.3) : .primary.opacity(0.06)))
    }

    @ViewBuilder private func actions(_ model: LocalASRModel) -> some View {
        HStack(spacing: 9) {
            if models.isMutating(model.id) { ProgressView().controlSize(.small) }
            else if models.isQueued(model.id) { Button("取消排队") { models.pause(model.id) } }
            else if models.activeID == model.id {
                if model.state == .verifying { Text("正在校验").font(.caption).foregroundStyle(.secondary) }
                else { Button("暂停") { models.pause(model.id) } }
            } else if model.isReady {
                if model.id == defaultID { Label("已选用", systemImage: "checkmark").font(.caption.weight(.medium)).foregroundStyle(.teal) }
                else if !model.legacy {
                    Button("设为默认") { editor.makeDefaultASR(model.id) }.disabled(!preferences.writable)
                }
            } else if [.paused, .failed].contains(model.state) {
                Button("继续下载") { models.download(model.id) }.buttonStyle(.borderedProminent).tint(.teal)
                    .accessibilityLabel("继续下载 \(model.name)")
                    .disabled(model.isInUse || !models.inventoryLoaded)
            } else if model.state == .repair {
                Button("修复下载") { models.download(model.id) }.buttonStyle(.borderedProminent).tint(.teal)
                    .accessibilityLabel("修复下载 \(model.name)")
                    .disabled(model.isInUse || !models.inventoryLoaded)
            } else if model.state == .uninstalled {
                Button("下载") { models.download(model.id) }.buttonStyle(.borderedProminent).tint(.teal)
                    .accessibilityLabel("下载 \(model.name)")
                    .disabled(!models.inventoryLoaded)
            }
            Menu {
                if models.activeID == model.id || model.partialBytes > 0 || [.paused, .failed].contains(model.state) {
                    Button("取消并清理未完成文件…", role: .destructive) { clearing = model }
                        .disabled(model.state == .verifying || model.isInUse || models.isMutating(model.id))
                }
                Button("删除模型…", role: .destructive) { removing = model }
                    .disabled(model.reclaimableBytes == 0 || model.isInUse || models.activeID == model.id || models.isQueued(model.id) || models.isMutating(model.id))
            } label: { Image(systemName: "ellipsis").frame(width: 18) }
                .menuStyle(.borderlessButton).fixedSize().accessibilityLabel("\(model.name)更多操作")
        }
    }
    private func badge(_ title: String, color: Color) -> some View {
        Text(title).font(.caption2.weight(.medium)).foregroundStyle(color)
            .padding(.horizontal, 7).padding(.vertical, 4).background(color.opacity(0.09), in: Capsule())
    }
    private func stateIcon(_ model: LocalASRModel) -> String {
        if models.isQueued(model.id) { return "clock" }
        switch model.state {
        case .ready: return "checkmark.circle.fill"
        case .downloading: return "arrow.down.circle"
        case .paused: return "pause.circle"
        case .verifying: return "checkmark.shield"
        case .repair, .failed, .unknown: return "exclamationmark.circle"
        case .uninstalled: return "arrow.down.to.line"
        }
    }
}
