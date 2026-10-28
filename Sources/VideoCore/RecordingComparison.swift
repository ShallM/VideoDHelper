import Foundation

/// Copies only immutable/local inputs into an independent recording task.
/// Summary results, usage and checkpoints deliberately stay with their source task.
public enum RecordingComparison {
    public static func create(taskID: String, strategy: DialogueStrategy, root: URL = AppStorage.root) throws -> ProcessingJob {
        var created: ProcessingJob?
        try TaskLifecycle.editRecording(taskID: taskID, root: root) { source in
            let original = try AppStorage.read(ProcessingJob.self, from: source.appendingPathComponent("job.json"))
            var request = try AppStorage.read(ProcessingRequest.self, from: source.appendingPathComponent("request.json"))
            guard request.sourceKind == .recording else { throw VideoError.toolFailed("只能为录音任务创建对照。") }
            let raw = try AppStorage.read([TranscriptSegment].self, from: source.appendingPathComponent("transcript.json"))
            guard !raw.isEmpty else { throw VideoError.toolFailed("请先取得录音原稿，再创建对照。") }
            let editURL = source.appendingPathComponent("transcript-edits.json")
            if FileManager.default.fileExists(atPath: editURL.path) {
                let edits = try AppStorage.read(RecordingEdits.self, from: editURL)
                guard edits.version == 1, UUID(uuidString: edits.revision) != nil,
                      edits.segments.count == raw.count, zip(edits.segments, raw).allSatisfy({ edit, original in
                          edit.id == original.id && edit.start == original.start && edit.end == original.end &&
                          !edit.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
                          ["A", "B", "unknown"].contains(edit.speaker ?? "unknown")
                      }) else { throw VideoError.toolFailed("录音校订数据无效，未创建对照任务。") }
            }

            var copy = ProcessingJob(title: original.title, url: "", source: original.source, summaryStrategy: "structured", workspace: .recording)
            copy.dialogueStrategy = strategy.rawValue
            copy.state = "transcribed"; copy.stage = "transcribed"; copy.segmentCount = raw.count
            copy.message = "已复制同一份原稿与校订，可使用\(DialogueStrategy.label(for: strategy.rawValue))整理。原任务保持不变。"
            let target = root.appendingPathComponent(WorkspaceKind.recording.tasksDirectoryName, isDirectory: true).appendingPathComponent(copy.id, isDirectory: true)
            let manager = FileManager.default
            var committed = false
            defer { if !committed { try? manager.removeItem(at: target) } }
            try manager.createDirectory(at: target, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])

            func copyInput(_ relative: String, required: Bool = false) throws -> Bool {
                let input = source.appendingPathComponent(relative)
                guard manager.fileExists(atPath: input.path) else {
                    if required { throw VideoError.toolFailed("录音输入文件缺失，未创建对照任务。") }
                    return false
                }
                let values = try input.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
                guard values.isRegularFile == true, values.isSymbolicLink != true,
                      input.resolvingSymlinksInPath().path.hasPrefix(source.resolvingSymlinksInPath().path + "/") else {
                    throw VideoError.toolFailed("录音输入文件位置无效，未创建对照任务。")
                }
                let output = target.appendingPathComponent(relative)
                try manager.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
                try manager.copyItem(at: input, to: output)
                try manager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: output.path)
                return true
            }
            try Task.checkCancellation()
            _ = try copyInput("transcript.json", required: true)
            for name in ["asr-transcript.json", "transcript-edits.json", "speakers.json", "diarization.json",
                         "recording-source.json", "recording-transcript.json", "transcription-model.json",
                         "transcription-runtime.json", "transcript.txt", "source.srt"] {
                _ = try copyInput(name)
            }
            if let relative = request.localAudioFile {
                let components = relative.split(separator: "/", omittingEmptySubsequences: false)
                guard components.count == 2, components[0] == "source", !components[1].isEmpty,
                      components[1] != ".", components[1] != "..", !relative.contains("\\") else {
                    throw VideoError.toolFailed("录音原件路径无效，未创建对照任务。")
                }
                if try copyInput(relative) { copy.mediaFiles = [relative] }
            }
            request.recordingAction = "summarize"
            request.dialogueStrategy = strategy.rawValue
            request.summaryStrategy = "structured"
            try Task.checkCancellation()
            try AppStorage.write(request, to: target.appendingPathComponent("request.json"))
            try AppStorage.write(["sourceTaskID": original.id, "dialogueStrategy": strategy.rawValue], to: target.appendingPathComponent("comparison-source.json"))
            // Publish the task only after every input has been copied successfully.
            try AppStorage.write(copy, to: target.appendingPathComponent("job.json"))
            committed = true
            created = copy
        }
        guard let created else { throw VideoError.toolFailed("无法创建录音对照任务。") }
        return created
    }
}
