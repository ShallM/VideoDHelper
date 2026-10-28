import Foundation

/// A default is a preference; a started transcription is a durable model choice.
public enum ASRTaskSelection {
    public static func isFrozen(at directory: URL) -> Bool {
        let manager = FileManager.default
        for name in ["transcription-model.json", "asr-transcript.json", "transcript.json"] {
            if manager.fileExists(atPath: directory.appendingPathComponent(name).path) { return true }
        }
        for name in ["checkpoints/whisper", "checkpoints/asr"] {
            if let files = manager.enumerator(at: directory.appendingPathComponent(name), includingPropertiesForKeys: nil),
               files.contains(where: { ($0 as? URL)?.pathExtension == "json" }) { return true }
        }
        return false
    }

    public static func selecting(_ modelID: String, in request: ProcessingRequest, directory: URL) throws -> ProcessingRequest {
        if isFrozen(at: directory) {
            guard request.effectiveASRModelID == modelID else {
                throw VideoError.toolFailed("这个任务已开始转录，不能更换模型或混用检查点。请新建任务使用其他模型。")
            }
            return request
        }
        var updated = request
        updated.asrModelID = modelID
        // Unsupported new IDs make old workers stop instead of silently using Turbo.
        updated.whisperModel = modelID
        return updated
    }

    public static func needsModel(for request: ProcessingRequest, directory: URL) -> Bool {
        if request.sourceKind == .recording {
            return request.recordingAction != "summarize"
                && !FileManager.default.fileExists(atPath: directory.appendingPathComponent("asr-transcript.json").path)
        }
        return request.useWhisper
            && !FileManager.default.fileExists(atPath: directory.appendingPathComponent("transcript.json").path)
    }
}
