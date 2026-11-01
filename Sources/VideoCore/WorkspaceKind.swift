import Foundation

public enum WorkspaceKind: String, Codable, CaseIterable, Identifiable, Sendable {
    case bilibili
    case recording
    public var id: String { rawValue }
    public var title: String { self == .bilibili ? "B站视频" : "录音整理" }
    public var symbol: String { self == .bilibili ? "play.rectangle" : "waveform" }
    public var libraryFilename: String { self == .bilibili ? "library.json" : "library-recording.json" }
    public var tasksDirectoryName: String { self == .bilibili ? "tasks" : "recording-tasks" }
}

public struct RecordingEdits: Codable, Sendable {
    public var version = 1
    public var revision: String
    public var segments: [TranscriptSegment]
    public var speakers: [String: String]
    public init(segments: [TranscriptSegment], speakers: [String: String]) {
        revision = UUID().uuidString
        self.segments = segments
        self.speakers = speakers
    }
}
