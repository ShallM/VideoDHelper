import Foundation
import Testing
@testable import VideoCore

struct TaskLibraryTests {
    @Test func oldLibraryDecodesAndArchivesStayOutOfActiveCollections() throws {
        var library = try JSONDecoder().decode(TaskLibrary.self, from: Data(#"{"projects":[],"assignments":{}}"#.utf8))
        let project = try library.createProject(name: "研究")
        let job = ProcessingJob(title: "Attention", url: "", source: "Whisper")
        try library.moveTask(job.id, to: project.id)
        try library.setArchived(true, taskID: job.id)
        #expect(library.tasks([job], in: .recent).isEmpty)
        #expect(library.tasks([job], in: .unassigned).isEmpty)
        #expect(library.tasks([job], in: .project(project.id)).isEmpty)
        #expect(library.tasks([job], in: .archived, matching: "attention").map(\.id) == [job.id])
        #expect(library.tasks([job], in: .archived, matching: "missing").isEmpty)
        let decoded = try JSONDecoder().decode(TaskLibrary.self, from: JSONEncoder().encode(library))
        #expect(decoded.isArchived(job.id))
        #expect(decoded.project(for: job.id)?.id == project.id)
        try library.renameProject(project.id, name: "新名称")
        #expect(library.project(for: job.id)?.name == "新名称")
        library.removeProject(project.id)
        #expect(library.isArchived(job.id))
        #expect(library.tasks([job], in: .unassigned).isEmpty)
        try library.setArchived(false, taskID: job.id)
        #expect(library.tasks([job], in: .unassigned).map(\.id) == [job.id])
    }

    @Test func existingTasksStartUnassignedAndRemainInRecent() throws {
        let missing = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).appendingPathComponent("library.json")
        var library = try TaskLibrary.load(from: missing)
        var older = ProcessingJob(title: "Attention 论文", url: "", source: "本地 Whisper")
        var newer = ProcessingJob(title: "Swift 入门", url: "", source: "平台字幕")
        older.updatedAt = 100; newer.updatedAt = 200
        let jobs = [older, newer]
        #expect(library.tasks(jobs, in: .unassigned).map(\.id) == [newer.id, older.id])
        let project = try library.createProject(name: "AI 研究")
        try library.moveTask(older.id, to: project.id)
        #expect(library.tasks(jobs, in: .recent).count == 2)
        #expect(library.tasks(jobs, in: .unassigned).map(\.id) == [newer.id])
        #expect(library.tasks(jobs, in: .project(project.id), matching: " attention ").map(\.id) == [older.id])
        #expect(library.tasks(jobs, in: .project(project.id), matching: "Swift").isEmpty)
        try library.moveTask(older.id, to: nil)
        #expect(library.tasks(jobs, in: .unassigned).count == 2)
    }

    @Test func organizationSurvivesRestartAndWorkerProgressWrites() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let libraryURL = folder.appendingPathComponent("library.json")
        var library = TaskLibrary()
        let project = try library.createProject(name: "编程教程")
        var job = ProcessingJob(title: "长视频", url: "", source: "Whisper")
        try AppStorage.write(job, to: folder.appendingPathComponent("job.json"))
        try library.moveTask(job.id, to: project.id)
        try library.save(to: libraryURL)
        job.state = "running"; job.updatedAt += 10
        try AppStorage.write(job, to: folder.appendingPathComponent("job.json"))
        var restored = try TaskLibrary.load(from: libraryURL)
        #expect(restored.project(for: job.id)?.name == "编程教程")
        try restored.renameProject(project.id, name: "Swift 与 Python")
        try restored.save(to: libraryURL)
        #expect(try TaskLibrary.load(from: libraryURL).project(for: job.id)?.name == "Swift 与 Python")
        #expect((try FileManager.default.attributesOfItem(atPath: libraryURL.path)[.posixPermissions] as? Int) == 0o600)
    }

    @Test func removingProjectUnassignsWithoutChangingResults() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let result = folder.appendingPathComponent("study-notes.md")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try "来源细节与时间戳".write(to: result, atomically: true, encoding: .utf8)
        var library = TaskLibrary()
        let project = try library.createProject(name: "临时分类")
        let job = ProcessingJob(title: "已完成", url: "", source: "字幕")
        try library.moveTask(job.id, to: project.id)
        library.removeProject(project.id)
        try library.save(to: folder.appendingPathComponent("library.json"))
        #expect(library.project(for: job.id) == nil)
        #expect(library.assignments.isEmpty)
        #expect(library.tasks([job], in: .unassigned).count == 1)
        #expect(try String(contentsOf: result, encoding: .utf8) == "来源细节与时间戳")
    }

    @Test func invalidAndDuplicateNamesAndMissingDestinationsAreRejected() throws {
        var library = TaskLibrary()
        let project = try library.createProject(name: "  AI 研究  ")
        #expect(project.name == "AI 研究")
        for name in [" ", "AI 研究", "ai 研究", "a\nb", String(repeating: "长", count: 41)] {
            #expect(throws: (any Error).self) { try library.createProject(name: name) }
        }
        try library.renameProject(project.id, name: "AI 研究")
        #expect(throws: (any Error).self) { try library.moveTask(UUID().uuidString, to: UUID().uuidString) }
        #expect(throws: (any Error).self) { try library.moveTask("../invalid", to: project.id) }
    }

    @Test func unreadableLibraryIsNotSilentlyReset() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let path = folder.appendingPathComponent("library.json")
        let damaged = "{incomplete"
        try damaged.write(to: path, atomically: true, encoding: .utf8)
        #expect(throws: (any Error).self) { try TaskLibrary.load(from: path) }
        #expect(try String(contentsOf: path, encoding: .utf8) == damaged)
    }
}
