import Foundation
import Testing
@testable import VideoCore

@Suite("Usage source and request snapshots")
struct UsageScopeTests {
    @Test func legacyEventsRemainReadableAndDoNotTurnTestsIntoVideoUsage() throws {
        let old = Data(#"{"schemaVersion":1,"id":"old","timestamp":1,"taskID":"old-task","purpose":"summary","status":"response","usage":{"invalid":false}}"#.utf8)
        let video = try JSONDecoder().decode(UsageEvent.self, from: old)
        #expect(video.sourceKind == nil && video.reasoningEffort == nil)
        #expect(UsageSourceFilter.bilibili.includes(video))
        #expect(video.requestedReasoningLabel == "历史未记录")
        var connection = UsageEvent()
        connection.sourceKind = "bilibili" // Purpose wins even for malformed old metadata.
        #expect(UsageSourceFilter.connectionTests.includes(connection))
        #expect(!UsageSourceFilter.bilibili.includes(connection))
        #expect(!UsageSourceFilter.recording.includes(connection))
        var unknown = UsageEvent(purpose: "summary")
        #expect(UsageSourceFilter.all.includes(unknown))
        #expect(!UsageSourceFilter.bilibili.includes(unknown))
        #expect(!UsageSourceFilter.connectionTests.includes(unknown))
        unknown.taskID = "some-task"; unknown.sourceKind = "future-workspace"
        #expect(!UsageSourceFilter.bilibili.includes(unknown))
        #expect(unknown.sourceLabel == "来源未记录")
    }

    @Test func connectionTestEffortSnapshotSurvivesLaterConfigurationEdits() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        var model = LanguageModel(modelID: "compatible-model")
        model.reasoningEffort = "max"
        let event = UsageEvent(model: model)
        try UsageRepository.save(event, root: root)
        model.reasoningEffort = "low"
        let saved = try #require(UsageRepository.load(root: root).events.first)
        #expect(saved.reasoningEffort == "max")
        #expect(saved.requestedReasoningLabel == ReasoningEffort.max.label)
        #expect(saved.sourceKind == nil && saved.resolvedSourceKind == nil)
        var defaultEvent = UsageEvent(); defaultEvent.reasoningEffort = "default"
        #expect(defaultEvent.requestedReasoningLabel == "服务商默认（未传参数）")
    }

    @Test func recordingUsageAndCostsSurviveTrashAndRestoreWithStableScope() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let job = ProcessingJob(title: "=录音测试", url: "", source: "本机录音", workspace: .recording)
        let folder = root.appendingPathComponent("recording-tasks/\(job.id)")
        let trash = root.appendingPathComponent("test-trash")
        try AppStorage.write(job, to: folder.appendingPathComponent("job.json"))
        var event = UsageEvent(timestamp: 200, purpose: "summary")
        event.taskID = job.id; event.sourceKind = "recording"; event.reasoningEffort = "high"
        event.status = "response"; event.phase = "whole"
        event.usage = TokenUsage(response: ["prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 40])
        event.pricing = TokenPricing(input: 2, output: 8, cached: 0.2)
        try UsageRepository.save(event, root: root)
        let legacy: [[String: Any]] = [
            ["eventID": event.id, "time": 200, "sourceKind": "recording", "reasoningEffort": "high", "usage": ["prompt_tokens": 100, "completion_tokens": 20]],
            ["time": 100, "sourceKind": "recording", "reasoningEffort": "default", "phase": "synthesis", "usage": ["prompt_tokens": 40, "completion_tokens": 10],
             "pricing": ["currency": "CNY", "inputPerMillion": 2.0, "outputPerMillion": 8.0]]
        ]
        let log = folder.appendingPathComponent("checkpoints/summary/usage.jsonl")
        try FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
        try legacy.map { String(decoding: try JSONSerialization.data(withJSONObject: $0, options: .sortedKeys), as: UTF8.self) }
            .joined(separator: "\n").write(to: log, atomically: true, encoding: .utf8)
        var unrelated = UsageEvent(purpose: "summary")
        unrelated.taskID = UUID().uuidString
        try UsageRepository.save(unrelated, root: root)
        let preferences = AppPreferences()
        let before = UsageRepository.load(root: root)
        let recordings = before.events.filter(UsageSourceFilter.recording.includes)
        #expect(recordings.count == 2 && before.unreadableRecords == 0)
        #expect(before.events.filter(UsageSourceFilter.bilibili.includes).count == 1)
        let csv = UsageRepository.csv(recordings, preferences: preferences, currentPrices: false)
        let totals = UsageTotals(events: recordings, preferences: preferences, currentPrices: false)
        #expect(totals.input == 140 && totals.output == 30 && totals.pricedCalls == 2)
        #expect(csv.contains("用量来源") && csv.contains("请求思考强度") && csv.contains("'="))
        _ = try TaskLifecycle.trash(taskID: job.id, root: root, workspace: .recording, moveToTrash: { path in
            try FileManager.default.moveItem(at: path, to: trash)
            return trash
        })
        #expect(!FileManager.default.fileExists(atPath: folder.path))
        let after = UsageRepository.load(root: root).events.filter(UsageSourceFilter.recording.includes)
        #expect(after.count == 2)
        #expect(UsageRepository.csv(after, preferences: preferences, currentPrices: false) == csv)
        #expect(UsageTotals(events: after, preferences: preferences, currentPrices: false).costs == totals.costs)
        try FileManager.default.moveItem(at: trash, to: folder)
        let restored = UsageRepository.load(root: root).events.filter(UsageSourceFilter.recording.includes)
        #expect(restored.count == 2)
        #expect(UsageRepository.csv(restored, preferences: preferences, currentPrices: false) == csv)
    }
}
