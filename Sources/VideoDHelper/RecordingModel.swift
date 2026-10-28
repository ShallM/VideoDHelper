import AppKit
import AVFoundation
import UniformTypeIdentifiers
import VideoCore

extension WorkspaceModel {
    var isRecording: Bool { workspace == .recording }
    var summaryStale: Bool { isRecording && notes != nil && (notes?.recordingRevision ?? "original") != editsRevision }
    var canSummarizeRecording: Bool { isRecording && !isBusy && !segments.isEmpty && currentJob != nil && !currentJobIsArchived && recordingEditsValid }
    var canTranscribeRecording: Bool { isRecording && !isBusy && recordingURL != nil && !currentJobIsArchived && editsRevision == "original" && recordingAction == "transcribe" && currentJob?.state != "transcribed" && currentJob?.state != "completed" }

    func importRecording() {
        guard isRecording, !isBusy else { return }
        let panel = NSOpenPanel()
        panel.title = "选择手机录音"
        panel.allowedContentTypes = ["m4a", "mp3", "wav", "aac", "flac", "aiff", "caf"].compactMap { UTType(filenameExtension: $0) }
        panel.allowsMultipleSelection = false; panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        importRecording(url)
    }

    func importRecording(_ url: URL) {
        guard isRecording, !isBusy else { return }
        start(status: "正在检查录音并保存本机副本…") {
            let ext = url.pathExtension.lowercased()
            guard ["m4a", "mp3", "wav", "aac", "flac", "aiff", "caf"].contains(ext) else {
                throw VideoError.toolFailed("请导入 M4A、MP3、WAV、AAC、FLAC、AIFF 或 CAF 录音。")
            }
            let access = url.startAccessingSecurityScopedResource()
            defer { if access { url.stopAccessingSecurityScopedResource() } }
            let file = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
            guard file.isRegularFile == true, file.isSymbolicLink != true, (file.fileSize ?? 0) > 0,
                  (file.fileSize ?? 0) <= 2_000_000_000 else { throw VideoError.toolFailed("请选择不超过 2 GB 的有效录音文件。") }
            let asset = AVURLAsset(url: url)
            let tracks = try await asset.loadTracks(withMediaType: .audio)
            let duration = try await asset.load(.duration).seconds
            guard !tracks.isEmpty, duration.isFinite, duration > 0, duration <= 3001 else {
                throw VideoError.toolFailed("当前支持最长 50 分钟的有效录音，请检查文件或先拆分长录音。")
            }
            try Task.checkCancellation()
            if self.collection == .archived { self.collection = .recent }
            let projectID = self.newTaskProjectID
            let job = ProcessingJob(title: url.deletingPathExtension().lastPathComponent, url: "", source: "手机录音 · 普通话", summaryStrategy: "structured", workspace: .recording)
            let relative = "source/original.\(ext)"
            let destination = job.directory.appendingPathComponent(relative)
            var committed = false
            defer { if !committed { try? FileManager.default.removeItem(at: job.directory) } }
            try await Task.detached(priority: .userInitiated) {
                let manager = FileManager.default
                try manager.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
                try manager.copyItem(at: url, to: destination)
                try manager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
            }.value
            var request = ProcessingRequest(title: job.title, url: "", source: job.source, segments: [], useWhisper: true,
                mediaMode: "needed", keepMedia: true, videoFormatID: "", audioFormatID: "", videoIncludesAudio: false,
                container: "m4a", whisperModel: self.whisperModel, summaryStrategy: "structured")
            request.sourceKind = .recording; request.localAudioFile = relative
            request.recordingAction = "transcribe"; request.diarizationMode = self.diarizationMode
            try Task.checkCancellation()
            try AppStorage.write(request, to: job.directory.appendingPathComponent("request.json"))
            try AppStorage.write(job, to: job.directory.appendingPathComponent("job.json"))
            committed = true
            self.currentJob = job; self.recordingAction = "transcribe"
            self.metadata = nil; self.link = ""; self.segments = []; self.condensed = []; self.notes = nil
            self.title = job.title; self.source = job.source; self.recordingURL = destination; self.recordingDuration = duration
            self.editsRevision = "original"; self.recordingEditsValid = true
            self.speakerNames = ["A": "说话人 A", "B": "说话人 B"]
            self.playback.load(destination, duration: duration); self.selectedTab = 0; self.historyQuery = ""
            self.history = AppStorage.history().filter { $0.workspace == self.workspace }
            if let projectID { self.moveJob(job, to: projectID) }
            try Task.checkCancellation()
            self.status = "录音副本已保存，点击“开始转录”。原文件不会被修改。"
        }
    }

    func transcribeRecording() {
        guard let job = currentJob, canTranscribeRecording else { return }
        start(status: "准备在本机转录录音…") {
            var request = try AppStorage.read(ProcessingRequest.self, from: job.directory.appendingPathComponent("request.json"))
            request.recordingAction = "transcribe"; request.diarizationMode = self.diarizationMode
            request.whisperModel = self.whisperModel
            try AppStorage.write(request, to: job.directory.appendingPathComponent("request.json"))
            try await self.run(job)
        }
    }
    func summarizeRecording() {
        guard let job = currentJob, canSummarizeRecording else { return }
        start(status: "正在整理双方观点、问答与共识…") {
            var request = try AppStorage.read(ProcessingRequest.self, from: job.directory.appendingPathComponent("request.json"))
            request.recordingAction = "summarize"
            self.recordingAction = "summarize"
            try AppStorage.write(request, to: job.directory.appendingPathComponent("request.json"))
            try await self.run(job)
        }
    }
    func loadRecording(_ job: ProcessingJob) {
        recordingURL = nil; recordingDuration = 0; recordingAction = "transcribe"
        playback.stop()
        speakerNames = (try? AppStorage.read([String: String].self, from: job.directory.appendingPathComponent("speakers.json"))) ?? ["A": "说话人 A", "B": "说话人 B"]
        editsRevision = "original"; recordingEditsValid = true
        let editURL = job.directory.appendingPathComponent("transcript-edits.json")
        if FileManager.default.fileExists(atPath: editURL.path) {
            do {
                let edits = try AppStorage.read(RecordingEdits.self, from: editURL)
                guard edits.version == 1, UUID(uuidString: edits.revision) != nil, edits.segments.count == segments.count,
                      zip(edits.segments, segments).allSatisfy({ corrected, original in
                          corrected.id == original.id && corrected.start == original.start && corrected.end == original.end &&
                          !corrected.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && ["A", "B", "unknown"].contains(corrected.speaker ?? "unknown")
                      }) else { throw VideoError.toolFailed("录音校订数据无效。") }
                segments = edits.segments; editsRevision = edits.revision
            } catch {
                recordingEditsValid = false
                self.error = "无法读取录音校订，原文件已保留；当前显示机器原稿。修复校订文件后可继续编辑和总结。"
            }
        }
        if let request = try? AppStorage.read(ProcessingRequest.self, from: job.directory.appendingPathComponent("request.json")),
           let relative = request.localAudioFile, relative.hasPrefix("source/"), !relative.contains("..") {
            recordingAction = request.recordingAction ?? "transcribe"
            whisperModel = request.whisperModel
            diarizationMode = request.diarizationMode ?? "off"
            struct SourceInfo: Decodable { let duration: Double }
            recordingDuration = (try? AppStorage.read(SourceInfo.self, from: job.directory.appendingPathComponent("recording-source.json")))?.duration ?? 0
            let url = job.directory.appendingPathComponent(relative).resolvingSymlinksInPath()
            if url.path.hasPrefix(job.directory.resolvingSymlinksInPath().path + "/source/"), FileManager.default.fileExists(atPath: url.path) {
                recordingURL = url; playback.load(url, duration: recordingDuration)
            }
        }
    }
    func speakerName(_ id: String?) -> String { speakerNames[id ?? "unknown"] ?? (id == "A" ? "说话人 A" : id == "B" ? "说话人 B" : "待确认") }
    func displayText(_ text: String) -> String {
        guard isRecording else { return text }
        return (["A", "B", "unknown"] as [String]).reduce(text) { value, id in
            value.replacingOccurrences(of: "[\(id)]", with: speakerName(id))
                .replacingOccurrences(of: "[speaker:\(id)]", with: speakerName(id))
        }
    }
    func renameSpeaker(_ id: String, name: String) {
        guard let job = currentJob, !isBusy, !currentJobIsArchived else { return }
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name.count <= 30, name.rangeOfCharacter(from: .controlCharacters) == nil else { error = "说话人名称需为 1～30 个字符。"; return }
        var names = speakerNames; names[id] = name
        do {
            try TaskLifecycle.editRecording(taskID: job.id) { folder in try AppStorage.write(names, to: folder.appendingPathComponent("speakers.json")) }
            speakerNames = names; status = "已更新说话人名称，无需重新转录或总结。"
        } catch { self.error = error.localizedDescription }
    }
    func correctSegment(_ segment: TranscriptSegment, text: String, speaker: String) {
        guard recordingEditsValid, let job = currentJob, !isBusy, !currentJobIsArchived, let index = segments.firstIndex(where: { $0.id == segment.id }) else { return }
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.count <= 10000, ["A", "B", "unknown"].contains(speaker) else { error = "请填写有效的原话和说话人。"; return }
        var updated = segments
        updated[index] = TranscriptSegment(id: segment.id, start: segment.start, end: segment.end, text: text, speaker: speaker)
        let edits = RecordingEdits(segments: updated, speakers: speakerNames)
        do {
            try TaskLifecycle.editRecording(taskID: job.id) { folder in try AppStorage.write(edits, to: folder.appendingPathComponent("transcript-edits.json")) }
            segments = updated; editsRevision = edits.revision
            status = "已保存校订，机器原稿保留。已有整理稿与总结需重新生成。"
        } catch { self.error = error.localizedDescription }
    }
    func recordingMarkdown(notesExport: Bool) -> String {
        var lines = ["# \(title) · \(notesExport ? "对话总结" : "整理对话")", ""]
        if notesExport, let notes {
            lines += [displayText(notes.overview), ""]
            for chapter in notes.chapters {
                lines += ["## " + displayText(chapter.title), ""]
                if let claims = chapter.claims, !claims.isEmpty {
                    for claim in claims { lines += [displayText(claim.text), "[" + sourceLabel(claim.source_ids) + "]", ""] }
                } else { lines += [displayText(chapter.detail), "[" + sourceLabel(chapter.source_ids) + "]", ""] }
            }
            for (heading, items) in [("共识与主要结论", notes.key_points), ("待核对内容", notes.uncertainties)] where !items.isEmpty {
                lines += ["## " + heading, ""]
                for item in items { lines += [displayText(item.text), "[" + sourceLabel(item.source_ids) + "]", ""] }
            }
        } else {
            for item in condensed { lines += ["[" + sourceLabel(item.source_ids) + "] " + speakerName(item.speaker) + "：" + displayText(item.text), ""] }
        }
        return lines.joined(separator: "\n")
    }
    func exportRecordingText(format: String) -> String {
        let rows = segments.map { segment -> TranscriptSegment in
            TranscriptSegment(id: segment.id, start: segment.start, end: segment.end,
                              text: "\(speakerName(segment.speaker))：\(segment.text)")
        }
        return format == "srt" ? TranscriptExport.srt(rows) : TranscriptExport.text(rows)
    }
}
