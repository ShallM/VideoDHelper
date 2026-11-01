import Foundation
import Darwin
import Testing
@testable import VideoCore

private struct RecordingWorkspaceFixture {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    let video = ProcessingJob(title: "同名任务", url: "https://www.bilibili.com/video/test", source: "平台字幕")
    let recording = ProcessingJob(title: "同名任务", url: "", source: "手机录音", workspace: .recording)

    init() throws {
        for job in [video, recording] {
            try AppStorage.write(job, to: folder(job).appendingPathComponent("job.json"))
            try AppStorage.write(["text": "保留的机器原稿"], to: folder(job).appendingPathComponent("transcript.json"))
        }
        for workspace in WorkspaceKind.allCases {
            var library = TaskLibrary(workspace: workspace)
            let project = try library.createProject(name: "研究")
            try library.moveTask(workspace == .bilibili ? video : recording, to: project.id)
            try library.save(to: libraryURL(workspace))
        }
    }
    func folder(_ job: ProcessingJob) -> URL { root.appendingPathComponent("\(job.workspace.tasksDirectoryName)/\(job.id)") }
    func libraryURL(_ workspace: WorkspaceKind) -> URL { TaskLibrary.fileURL(for: workspace, root: root) }
    func cleanup() { try? FileManager.default.removeItem(at: root) }
    func moveToTestTrash(_ source: URL) throws -> URL {
        let destination = root.appendingPathComponent("test-trash/\(source.lastPathComponent)")
        try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.moveItem(at: source, to: destination)
        return destination
    }
}

struct RecordingWorkspaceTests {
    @Test func legacyJobRequestAndLibraryRemainVideoWithoutWorkspaceFields() throws {
        let original = ProcessingJob(title: "旧视频", url: "", source: "字幕")
        var data = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(original)) as? [String: Any])
        data.removeValue(forKey: "sourceKind")
        let decoded = try JSONDecoder().decode(ProcessingJob.self, from: JSONSerialization.data(withJSONObject: data))
        #expect(decoded.sourceKind == nil)
        #expect(decoded.workspace == .bilibili)
        #expect(decoded.id == original.id)
        let library = try JSONDecoder().decode(TaskLibrary.self, from: Data(#"{"projects":[],"assignments":{}}"#.utf8))
        #expect(library.workspace == .bilibili)
        #expect(library.archivedTaskIDs.isEmpty)
        #expect(library.tasks([decoded], in: .recent).map(\.id) == [decoded.id])

        let request = ProcessingRequest(title: "旧视频", url: "", source: "字幕", segments: [], useWhisper: false,
            mediaMode: "needed", keepMedia: false, videoFormatID: "", audioFormatID: "", videoIncludesAudio: false,
            container: "mp4", whisperModel: "turbo")
        let oldRequest = try JSONDecoder().decode(ProcessingRequest.self, from: JSONEncoder().encode(request))
        #expect(oldRequest.sourceKind == nil)
        #expect(oldRequest.recordingAction == nil)
        #expect(oldRequest.localAudioFile == nil)
    }

    @Test func separateLibrariesAllowSameProjectNameAndIsolateAllCollections() throws {
        let fixture = try RecordingWorkspaceFixture(); defer { fixture.cleanup() }
        let videos = try TaskLibrary.load(workspace: .bilibili, root: fixture.root)
        let recordings = try TaskLibrary.load(workspace: .recording, root: fixture.root)
        #expect(fixture.libraryURL(.bilibili) != fixture.libraryURL(.recording))
        #expect(fixture.folder(fixture.video).deletingLastPathComponent().lastPathComponent == "tasks")
        #expect(fixture.folder(fixture.recording).deletingLastPathComponent().lastPathComponent == "recording-tasks")
        // A previous app version enumerating only tasks/ must never discover
        // new recording jobs, even though their IDs remain ordinary UUIDs.
        let legacyFolders = try FileManager.default.contentsOfDirectory(at: fixture.root.appendingPathComponent("tasks"), includingPropertiesForKeys: nil)
        #expect(legacyFolders.map(\.lastPathComponent) == [fixture.video.id])
        #expect(!FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent("tasks/\(fixture.recording.id)").path))
        #expect(videos.projects.first?.name == recordings.projects.first?.name)
        #expect(videos.projects.first?.id != recordings.projects.first?.id)
        let mixed = [fixture.video, fixture.recording]
        for (library, expected) in [(videos, fixture.video), (recordings, fixture.recording)] {
            #expect(library.tasks(mixed, in: .recent).map(\.id) == [expected.id])
            #expect(library.tasks(mixed, in: .unassigned).isEmpty)
            #expect(library.tasks(mixed, in: .archived).isEmpty)
            let project = try #require(library.project(for: expected.id))
            #expect(library.tasks(mixed, in: .project(project.id), matching: "同名").map(\.id) == [expected.id])
        }
        var updated = recordings
        updated.removeProject(try #require(updated.projects.first).id)
        try updated.save(to: fixture.libraryURL(.recording))
        #expect(updated.tasks(mixed, in: .unassigned).map(\.id) == [fixture.recording.id])
        #expect(try TaskLibrary.load(workspace: .bilibili, root: fixture.root).project(for: fixture.video.id)?.name == "研究")
    }

    @Test func missingRecordingLibraryStartsEmptyButWrongWorkspaceFileIsRejected() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let missing = try TaskLibrary.load(workspace: .recording, root: root)
        #expect(missing.workspace == .recording)
        #expect(missing.projects.isEmpty)
        let path = TaskLibrary.fileURL(for: .recording, root: root)
        let legacy = Data(#"{"projects":[],"assignments":{}}"#.utf8)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try legacy.write(to: path)
        #expect(throws: (any Error).self) { try TaskLibrary.load(workspace: .recording, root: root) }
        #expect(try Data(contentsOf: path) == legacy)
    }

    @Test func crossWorkspaceMoveArchiveAndTrashFailBeforeDataChanges() throws {
        let fixture = try RecordingWorkspaceFixture(); defer { fixture.cleanup() }
        let beforeVideo = try Data(contentsOf: fixture.libraryURL(.bilibili))
        let beforeRecording = try Data(contentsOf: fixture.libraryURL(.recording))
        let beforeJob = try Data(contentsOf: fixture.folder(fixture.recording).appendingPathComponent("job.json"))
        var videos = try TaskLibrary.load(workspace: .bilibili, root: fixture.root)
        var recordings = try TaskLibrary.load(workspace: .recording, root: fixture.root)
        let videoProjectID = try #require(videos.projects.first).id
        let recordingProjectID = try #require(recordings.projects.first).id
        #expect(throws: (any Error).self) { try videos.moveTask(fixture.recording, to: videoProjectID) }
        #expect(throws: (any Error).self) { try recordings.moveTask(fixture.video, to: recordingProjectID) }
        #expect(throws: (any Error).self) { try recordings.moveTask(fixture.recording, to: videoProjectID) }
        for (job, wrongWorkspace) in [(fixture.recording, WorkspaceKind.bilibili), (fixture.video, .recording)] {
            #expect(throws: (any Error).self) {
                try TaskLifecycle.setArchived(true, taskID: job.id, root: fixture.root, workspace: wrongWorkspace)
            }
            var moved = false
            #expect(throws: (any Error).self) {
                try TaskLifecycle.trash(taskID: job.id, root: fixture.root, workspace: wrongWorkspace, moveToTrash: { _ in
                    moved = true
                    return fixture.root.appendingPathComponent("must-not-run")
                })
            }
            #expect(!moved)
        }
        #expect(try Data(contentsOf: fixture.libraryURL(.bilibili)) == beforeVideo)
        #expect(try Data(contentsOf: fixture.libraryURL(.recording)) == beforeRecording)
        #expect(try Data(contentsOf: fixture.folder(fixture.recording).appendingPathComponent("job.json")) == beforeJob)
        #expect(FileManager.default.fileExists(atPath: fixture.folder(fixture.video).path))
        #expect(FileManager.default.fileExists(atPath: fixture.folder(fixture.recording).path))
    }

    @Test func recordingArchiveRestoreAndTrashNeverTouchVideoLibrary() throws {
        let fixture = try RecordingWorkspaceFixture(); defer { fixture.cleanup() }
        let videoBytes = try Data(contentsOf: fixture.libraryURL(.bilibili))
        let mixed = [fixture.video, fixture.recording]
        var library = try TaskLifecycle.setArchived(true, taskID: fixture.recording.id, root: fixture.root, workspace: .recording)
        #expect(library.tasks(mixed, in: .archived).map(\.id) == [fixture.recording.id])
        #expect(library.tasks(mixed, in: .recent).isEmpty)
        library = try TaskLifecycle.setArchived(false, taskID: fixture.recording.id, root: fixture.root, workspace: .recording)
        #expect(library.tasks(mixed, in: .recent).map(\.id) == [fixture.recording.id])
        #expect(library.project(for: fixture.recording.id)?.name == "研究")
        let original = fixture.folder(fixture.recording).appendingPathComponent("source/original.m4a")
        try FileManager.default.createDirectory(at: original.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("synthetic original".utf8).write(to: original)
        library = try TaskLifecycle.trash(taskID: fixture.recording.id, root: fixture.root, workspace: .recording, moveToTrash: fixture.moveToTestTrash)
        #expect(library.project(for: fixture.recording.id) == nil)
        #expect(library.tasks(mixed.filter { $0.id != fixture.recording.id }, in: .recent).isEmpty)
        #expect(try Data(contentsOf: fixture.libraryURL(.bilibili)) == videoBytes)
        #expect(FileManager.default.fileExists(atPath: fixture.folder(fixture.video).appendingPathComponent("job.json").path))
        #expect(try Data(contentsOf: fixture.root.appendingPathComponent("test-trash/\(fixture.recording.id)/source/original.m4a")) == Data("synthetic original".utf8))
    }

    @Test func recordingEditsRequireIdleLockAndDoNotOverwriteMachineTranscript() throws {
        let fixture = try RecordingWorkspaceFixture(); defer { fixture.cleanup() }
        let folder = fixture.folder(fixture.recording)
        let original = try Data(contentsOf: folder.appendingPathComponent("transcript.json"))
        let editURL = folder.appendingPathComponent("transcript-edits.json")
        let edits = RecordingEdits(segments: [TranscriptSegment(id: 0, start: 0, end: 1, text: "不是。", speaker: "B")], speakers: ["A": "甲", "B": "乙"])
        let descriptor = open(folder.appendingPathComponent("run.lock").path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        #expect(descriptor >= 0)
        defer { close(descriptor) }
        #expect(flock(descriptor, LOCK_EX | LOCK_NB) == 0)
        #expect(throws: (any Error).self) {
            try TaskLifecycle.editRecording(taskID: fixture.recording.id, root: fixture.root) { directory in
                try AppStorage.write(edits, to: directory.appendingPathComponent("transcript-edits.json"))
            }
        }
        #expect(!FileManager.default.fileExists(atPath: editURL.path))
        #expect(flock(descriptor, LOCK_UN) == 0)
        try TaskLifecycle.editRecording(taskID: fixture.recording.id, root: fixture.root) { directory in
            try AppStorage.write(edits, to: directory.appendingPathComponent("transcript-edits.json"))
        }
        let saved = try AppStorage.read(RecordingEdits.self, from: editURL)
        #expect(saved.revision == edits.revision)
        #expect(saved.segments.first?.speaker == "B")
        #expect(try Data(contentsOf: folder.appendingPathComponent("transcript.json")) == original)
    }

    @Test func archivedRecordingAndVideoRejectRecordingEdits() throws {
        let fixture = try RecordingWorkspaceFixture(); defer { fixture.cleanup() }
        _ = try TaskLifecycle.setArchived(true, taskID: fixture.recording.id, root: fixture.root, workspace: .recording)
        for job in [fixture.recording, fixture.video] {
            var performed = false
            #expect(throws: (any Error).self) {
                try TaskLifecycle.editRecording(taskID: job.id, root: fixture.root) { _ in performed = true }
            }
            #expect(!performed)
        }
        _ = try TaskLifecycle.setArchived(false, taskID: fixture.recording.id, root: fixture.root, workspace: .recording)
        var performed = false
        try TaskLifecycle.editRecording(taskID: fixture.recording.id, root: fixture.root) { _ in performed = true }
        #expect(performed)
    }

    @Test func speakerAndRecordingMetadataAreOptionalForLegacyJSON() throws {
        let old = try JSONDecoder().decode(TranscriptSegment.self, from: Data(#"{"id":0,"start":0,"end":1,"text":"原话"}"#.utf8))
        #expect(old.speaker == nil)
        for speaker in ["A", "B", "unknown"] {
            let value = TranscriptSegment(id: 0, start: 0, end: 1, text: "原话", speaker: speaker)
            let roundTrip = try JSONDecoder().decode(TranscriptSegment.self, from: JSONEncoder().encode(value))
            #expect(roundTrip == value)
        }
        let withWords = try JSONDecoder().decode(TranscriptSegment.self, from: Data(#"{"id":0,"start":0,"end":1,"text":"不是","speaker":"B","words":[{"start":0,"end":1,"word":"不是"}]}"#.utf8))
        #expect(withWords.speaker == "B")
        let cited = try JSONDecoder().decode(CitedText.self, from: Data(#"{"source_ids":[0],"text":"旧精简稿"}"#.utf8))
        #expect(cited.speaker == nil)
        let dialogue = try JSONDecoder().decode(CitedText.self, from: Data(#"{"source_ids":[0],"text":"新整理稿","speaker":"A"}"#.utf8))
        #expect(dialogue.speaker == "A")
        let legacyNotes = Data(#"{"overview":"旧概览","chapters":[],"key_points":[],"uncertainties":[],"model":"test","tokensThisRun":0}"#.utf8)
        #expect(try JSONDecoder().decode(StudyNotes.self, from: legacyNotes).recordingRevision == nil)
        let edits = RecordingEdits(segments: [withWords], speakers: ["B": "乙"])
        #expect(UUID(uuidString: edits.revision) != nil)
        let restored = try JSONDecoder().decode(RecordingEdits.self, from: JSONEncoder().encode(edits))
        #expect(restored.version == 1)
        #expect(restored.segments == [withWords])
        #expect(restored.speakers["B"] == "乙")
    }
}
