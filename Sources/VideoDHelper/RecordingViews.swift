import SwiftUI
import VideoCore

struct AudioPlayerBar: View {
    @ObservedObject var player: AudioPlayback
    var body: some View {
        VStack(spacing: 8) {
            Divider()
            HStack(spacing: 14) {
                Button(action: player.toggle) { Image(systemName: player.isPlaying ? "pause.fill" : "play.fill") }
                    .buttonStyle(.borderless).accessibilityLabel(player.isPlaying ? "暂停录音" : "播放录音")
                    .disabled(!player.isLoaded)
                Slider(value: Binding(get: { min(player.position, max(1, player.duration)) }, set: { player.seek(to: $0) }), in: 0...max(1, player.duration))
                    .accessibilityLabel("录音播放进度").disabled(!player.isLoaded)
                Text("\(TranscriptExport.timestamp(player.position)) / \(TranscriptExport.timestamp(player.duration))")
                    .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                Picker("播放速度", selection: $player.speed) {
                    Text("0.75×").tag(Float(0.75)); Text("1×").tag(Float(1)); Text("1.25×").tag(Float(1.25))
                    Text("1.5×").tag(Float(1.5)); Text("2×").tag(Float(2))
                }.labelsHidden().frame(width: 80)
            }.padding(.horizontal, 24).padding(.vertical, 10)
        }
    }
}

struct SpeakerNameEditor: View {
    @ObservedObject var model: WorkspaceModel
    let speaker: String
    @State private var showsEditor = false
    @State private var name = ""
    var body: some View {
        Button {
            name = model.speakerName(speaker); showsEditor = true
        } label: {
            Label(model.speakerName(speaker), systemImage: "person.crop.circle.fill")
                .foregroundStyle(speaker == "B" ? .purple : .teal)
        }.buttonStyle(.borderless).font(.callout)
            .disabled(model.isBusy || model.currentJobIsArchived)
            .help("修改说话人显示名称")
            .popover(isPresented: $showsEditor) {
                VStack(alignment: .leading, spacing: 12) {
                    Text("说话人名称").font(.headline)
                    TextField("例如 女1、男1或姓名", text: $name).textFieldStyle(.roundedBorder)
                    Text("只更新名称，不重新识别声音或生成总结。").font(.caption).foregroundStyle(.secondary)
                    HStack {
                        Spacer()
                        Button("保存") { model.renameSpeaker(speaker, name: name); showsEditor = false }
                            .buttonStyle(.borderedProminent).disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }.padding(18).frame(width: 300)
            }
    }
}

struct RecordingSegmentEditor: View {
    @ObservedObject var model: WorkspaceModel
    let segment: TranscriptSegment
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var speaker = "unknown"
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("校订原话").font(.title2.bold())
            HStack {
                Button { model.openSource([segment.id]) } label: { Label(TranscriptExport.timestamp(segment.start), systemImage: "play.circle") }
                Spacer()
                Picker("说话人", selection: $speaker) {
                    Text(model.speakerName("A")).tag("A")
                    Text(model.speakerName("B")).tag("B")
                    Text("待确认 / 重叠讲话").tag("unknown")
                }.frame(width: 240)
            }
            TextEditor(text: $text).font(.body).frame(height: 180)
                .padding(8).overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
            Text("机器转录原稿会保留。修改原话或归属后，已有整理稿和总结需要重新生成。")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("保存校订") { model.correctSegment(segment, text: text, speaker: speaker); dismiss() }
                    .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || text.count > 10000)
            }
        }.padding(24).frame(width: 540)
            .onAppear { text = segment.text; speaker = segment.speaker ?? "unknown" }
    }
}
