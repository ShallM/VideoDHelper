import Foundation

/// User background is separate from the immutable transcript and its evidence.
public enum RecordingContext {
    public static func normalized(_ text: String?) throws -> String {
        let text = text ?? ""
        guard text.unicodeScalars.count <= 4000 else {
            throw VideoError.toolFailed("背景与关注点请控制在 4000 字符以内。")
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    @discardableResult
    public static func save(_ text: String, taskID: String, root: URL = AppStorage.root) throws -> String {
        let value = try normalized(text)
        try TaskLifecycle.editRecording(taskID: taskID, root: root) { folder in
            let url = folder.appendingPathComponent("request.json")
            var request = try AppStorage.read(ProcessingRequest.self, from: url)
            guard request.sourceKind == .recording,
                  DialogueStrategy(rawValue: request.effectiveDialogueStrategy)?.supportsContext == true else {
                throw VideoError.toolFailed("请先新建会议纪要或任务理解对照，再补充背景。")
            }
            request.recordingContext = value.isEmpty ? nil : value
            try AppStorage.write(request, to: url)
        }
        return value
    }
}
