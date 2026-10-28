import Foundation
import Darwin
import Testing
@testable import VideoCore

private struct TaskFixture {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    var job = ProcessingJob(title: "测试任务 · 原始标题", url: "", source: "导入字幕")
    var folder: URL { root.appendingPathComponent("tasks/\(job.id)") }
    var libraryURL: URL { root.appendingPathComponent("library.json") }
    var trashURL: URL { root.appendingPathComponent("test-trash/\(job.id)") }
    var usageURL: URL { folder.appendingPathComponent("checkpoints/summary/usage.jsonl") }

    init() throws {
        job.state = "running" // Interrupted jobs must remain manageable without a live lock.
        try AppStorage.write(job, to: folder.appendingPathComponent("job.json"))
        try AppStorage.write(["text": "原稿、笔记、检查点应完整保留"], to: folder.appendingPathComponent("notes.json"))
        try AppStorage.write(["media": "sample"], to: folder.appendingPathComponent("media/source.json"))
        var library = TaskLibrary()
        let project = try library.createProject(name: "保留归属")
        try library.moveTask(job.id, to: project.id)
        try library.save(to: libraryURL)
    }
    func cleanup() { try? FileManager.default.removeItem(at: root) }
    func moveToTestTrash(_ source: URL) throws -> URL {
        try FileManager.default.createDirectory(at: trashURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.moveItem(at: source, to: trashURL)
        return trashURL
    }
    func writeUsage(_ rows: [[String: Any]]) throws {
        try FileManager.default.createDirectory(at: usageURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        let lines = try rows.map { String(decoding: try JSONSerialization.data(withJSONObject: $0, options: .sortedKeys), as: UTF8.self) }
        try lines.joined(separator: "\n").write(to: usageURL, atomically: true, encoding: .utf8)
    }
}

struct TaskLifecycleTests {
    @Test func archiveAndRestorePreserveFilesAndSurviveReload() throws {
        let fixture = try TaskFixture(); defer { fixture.cleanup() }
        let before = try Data(contentsOf: fixture.folder.appendingPathComponent("job.json"))
        let archived = try TaskLifecycle.setArchived(true, taskID: fixture.job.id, root: fixture.root)
        #expect(archived.tasks([fixture.job], in: .recent).isEmpty)
        #expect(try TaskLibrary.load(from: fixture.libraryURL).isArchived(fixture.job.id))
        let restored = try TaskLifecycle.setArchived(false, taskID: fixture.job.id, root: fixture.root)
        #expect(restored.project(for: fixture.job.id)?.name == "保留归属")
        #expect(restored.tasks([fixture.job], in: .recent).count == 1)
        #expect(try Data(contentsOf: fixture.folder.appendingPathComponent("job.json")) == before)
        #expect(FileManager.default.fileExists(atPath: fixture.folder.appendingPathComponent("media/source.json").path))
    }

    @Test func activeWorkerLockBlocksArchiveAndTrash() throws {
        let fixture = try TaskFixture(); defer { fixture.cleanup() }
        let descriptor = open(fixture.folder.appendingPathComponent("run.lock").path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        #expect(descriptor >= 0)
        defer { close(descriptor) }
        #expect(flock(descriptor, LOCK_EX | LOCK_NB) == 0)
        #expect(throws: (any Error).self) { try TaskLifecycle.setArchived(true, taskID: fixture.job.id, root: fixture.root) }
        #expect(throws: (any Error).self) { try TaskLifecycle.trash(taskID: fixture.job.id, root: fixture.root, moveToTrash: fixture.moveToTestTrash) }
        #expect(!FileManager.default.fileExists(atPath: fixture.trashURL.path))
        #expect(try TaskLibrary.load(from: fixture.libraryURL).project(for: fixture.job.id) != nil)
        #expect(flock(descriptor, LOCK_UN) == 0)
        _ = try TaskLifecycle.setArchived(true, taskID: fixture.job.id, root: fixture.root)
    }

    @Test func trashMovesWholeTaskAndForgetsOnlyItsOrganization() throws {
        let fixture = try TaskFixture(); defer { fixture.cleanup() }
        let sibling = ProcessingJob(title: "保留任务", url: "", source: "字幕")
        let siblingURL = fixture.root.appendingPathComponent("tasks/\(sibling.id)/job.json")
        try AppStorage.write(sibling, to: siblingURL)
        _ = try TaskLifecycle.setArchived(true, taskID: fixture.job.id, root: fixture.root)
        let library = try TaskLifecycle.trash(taskID: fixture.job.id, root: fixture.root, moveToTrash: fixture.moveToTestTrash)
        #expect(!FileManager.default.fileExists(atPath: fixture.folder.path))
        #expect(FileManager.default.fileExists(atPath: fixture.trashURL.appendingPathComponent("notes.json").path))
        #expect(FileManager.default.fileExists(atPath: fixture.trashURL.appendingPathComponent("media/source.json").path))
        #expect(FileManager.default.fileExists(atPath: siblingURL.path))
        #expect(library.project(for: fixture.job.id) == nil)
        #expect(!library.isArchived(fixture.job.id))
        #expect(library.projects.count == 1)
        try FileManager.default.moveItem(at: fixture.trashURL, to: fixture.folder)
        #expect(library.tasks([fixture.job], in: .unassigned).count == 1)
    }

    @Test func trashFailurePreservesTaskAndOrganization() throws {
        let fixture = try TaskFixture(); defer { fixture.cleanup() }
        _ = try TaskLifecycle.setArchived(true, taskID: fixture.job.id, root: fixture.root)
        #expect(throws: (any Error).self) {
            try TaskLifecycle.trash(taskID: fixture.job.id, root: fixture.root, moveToTrash: { _ in
                throw CocoaError(.fileWriteNoPermission)
            })
        }
        #expect(FileManager.default.fileExists(atPath: fixture.folder.appendingPathComponent("notes.json").path))
        let library = try TaskLibrary.load(from: fixture.libraryURL)
        #expect(library.isArchived(fixture.job.id))
        #expect(library.project(for: fixture.job.id)?.name == "保留归属")
    }

    @Test func failedOrganizationCommitRollsTaskBackOutOfTrash() throws {
        let fixture = try TaskFixture(); defer { fixture.cleanup() }
        let original = try Data(contentsOf: fixture.libraryURL)
        #expect(throws: (any Error).self) {
            try TaskLifecycle.trash(taskID: fixture.job.id, root: fixture.root, moveToTrash: { folder in
                let destination = try fixture.moveToTestTrash(folder)
                // Simulate a filesystem failure arising after the preflight write.
                try FileManager.default.removeItem(at: fixture.libraryURL)
                try FileManager.default.createDirectory(at: fixture.libraryURL, withIntermediateDirectories: false)
                return destination
            })
        }
        #expect(FileManager.default.fileExists(atPath: fixture.folder.appendingPathComponent("notes.json").path))
        #expect(!FileManager.default.fileExists(atPath: fixture.trashURL.path))
        try FileManager.default.removeItem(at: fixture.libraryURL)
        try original.write(to: fixture.libraryURL)
        #expect(try TaskLibrary.load(from: fixture.libraryURL).project(for: fixture.job.id) != nil)
    }

    @Test func malformedLibraryOrLegacyUsagePreventsDeletion() throws {
        let fixture = try TaskFixture(); defer { fixture.cleanup() }
        let original = try Data(contentsOf: fixture.libraryURL)
        try Data("{broken".utf8).write(to: fixture.libraryURL)
        #expect(throws: (any Error).self) { try TaskLifecycle.trash(taskID: fixture.job.id, root: fixture.root, moveToTrash: fixture.moveToTestTrash) }
        #expect(try Data(contentsOf: fixture.libraryURL) == Data("{broken".utf8))
        try original.write(to: fixture.libraryURL)
        try fixture.writeUsage([["time": 100, "usage": ["prompt_tokens": 10, "completion_tokens": 2]]])
        let file = try FileHandle(forWritingTo: fixture.usageURL)
        try file.seekToEnd(); try file.write(contentsOf: Data("\n{broken".utf8)); try file.close()
        #expect(throws: (any Error).self) { try TaskLifecycle.trash(taskID: fixture.job.id, root: fixture.root, moveToTrash: fixture.moveToTestTrash) }
        #expect(!FileManager.default.fileExists(atPath: fixture.trashURL.path))
        #expect(FileManager.default.fileExists(atPath: fixture.folder.appendingPathComponent("job.json").path))
    }

    @Test func pathsAndSymlinksCannotSelectUnrelatedFiles() throws {
        let fixture = try TaskFixture(); defer { fixture.cleanup() }
        for id in ["../escape", "", "/tmp"] {
            #expect(throws: (any Error).self) { try TaskLifecycle.trash(taskID: id, root: fixture.root, moveToTrash: fixture.moveToTestTrash) }
        }
        let alias = UUID().uuidString
        try FileManager.default.createSymbolicLink(at: fixture.root.appendingPathComponent("tasks/\(alias)"), withDestinationURL: fixture.folder)
        #expect(throws: (any Error).self) { try TaskLifecycle.trash(taskID: alias, root: fixture.root, moveToTrash: fixture.moveToTestTrash) }
        try FileManager.default.createSymbolicLink(at: fixture.folder.appendingPathComponent("run.lock"), withDestinationURL: fixture.libraryURL)
        #expect(throws: (any Error).self) { try TaskLifecycle.setArchived(true, taskID: fixture.job.id, root: fixture.root) }
        #expect(try TaskLibrary.load(from: fixture.libraryURL).project(for: fixture.job.id) != nil)
    }

    @Test func modernAndLegacyUsageAndCostsSurviveTrashAndRestoreWithoutDuplication() throws {
        let fixture = try TaskFixture(); defer { fixture.cleanup() }
        var modern = UsageEvent(id: UUID().uuidString, timestamp: 200, purpose: "summary")
        modern.taskID = fixture.job.id; modern.status = "response"
        modern.usage = TokenUsage(response: ["prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": ["cached_tokens": 50]])
        try UsageRepository.save(modern, root: fixture.root)
        try fixture.writeUsage([
            ["eventID": modern.id, "time": 200, "usage": ["prompt_tokens": 100, "completion_tokens": 20]],
            ["time": 100, "usage": ["prompt_tokens": 40, "completion_tokens": 10], "modelID": "old-model",
             "pricing": ["currency": "CNY", "inputPerMillion": 2.0, "outputPerMillion": 8.0]]
        ])
        let preferences = AppPreferences()
        let before = UsageRepository.load(root: fixture.root)
        #expect(before.events.count == 2)
        let csv = UsageRepository.csv(before.events, preferences: preferences, currentPrices: false)
        _ = try TaskLifecycle.trash(taskID: fixture.job.id, root: fixture.root, moveToTrash: fixture.moveToTestTrash)
        let after = UsageRepository.load(root: fixture.root)
        #expect(after.events.count == 2)
        #expect(after.unreadableRecords == 0)
        #expect(UsageRepository.csv(after.events, preferences: preferences, currentPrices: false) == csv)
        try FileManager.default.moveItem(at: fixture.trashURL, to: fixture.folder)
        #expect(UsageRepository.load(root: fixture.root).events.count == 2)
        _ = try TaskLifecycle.trash(taskID: fixture.job.id, root: fixture.root, moveToTrash: fixture.moveToTestTrash)
        #expect(UsageRepository.csv(UsageRepository.load(root: fixture.root).events, preferences: preferences, currentPrices: false) == csv)
    }
}
