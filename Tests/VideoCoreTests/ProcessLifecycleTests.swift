import Foundation
import Testing
import Darwin
@testable import VideoCore

@Suite("Application process lifecycle")
struct ProcessLifecycleTests {
    @Test
    func quitStopsOwnedProcessAndRejectsNewLaunches() async throws {
        let registry = ProcessRegistry()
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let pidFile = folder.appendingPathComponent("pid")
        let script = "import os,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
        let task = Task { try await ProcessRunner.run(path: "/usr/bin/python3", arguments: ["-c", script, pidFile.path], registry: registry) }
        defer { task.cancel() }
        for _ in 0..<100 where !FileManager.default.fileExists(atPath: pidFile.path) { try await Task.sleep(nanoseconds: 20_000_000) }
        let pid = try #require(Int32(String(contentsOf: pidFile, encoding: .utf8)))
        #expect(registry.running)
        let start = Date()
        await registry.terminate()
        do { _ = try await task.value; Issue.record("Forced shutdown should interrupt the process") } catch { }
        #expect(Date().timeIntervalSince(start) < 5)
        #expect(!registry.running)
        #expect(kill(pid, 0) != 0)
        await #expect(throws: CancellationError.self) {
            _ = try await ProcessRunner.run(path: "/usr/bin/true", arguments: [], registry: registry)
        }
    }

    @Test
    func completedJobsReleaseOutputCapturesWithoutWaitingForDeadline() async throws {
        final class Token: @unchecked Sendable { }
        weak var released: Token?
        func execute() async throws {
            let token = Token()
            released = token
            _ = try await ProcessRunner.run(path: "/usr/bin/printf", arguments: ["done"], timeoutSeconds: 14_400) { [token] _ in
                _ = token
            }
        }
        try await execute()
        for _ in 0..<20 where released != nil { try await Task.sleep(nanoseconds: 10_000_000) }
        #expect(released == nil)
    }
}
