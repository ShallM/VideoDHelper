import Foundation
import Testing
@testable import VideoCore

@Suite("Processing feedback and citation indexing")
struct ProcessingActivityTests {
    let start = Date(timeIntervalSince1970: 1000)

    @Test func reportsOnlyProvidedProgressAndKeepsRetryInItsStage() {
        var activity = ProcessingActivity(message: "准备", at: start)
        activity.receive(stage: "transcribe", message: "转录", progress: 0.4, at: start.addingTimeInterval(2))
        #expect(activity.progress == 0.4)
        activity.receive(stage: "meeting_evidence", message: "证据", progress: nil, at: start.addingTimeInterval(10))
        #expect(activity.progress == nil)
        activity.receive(stage: "summary", message: "修正一次", progress: nil, at: start.addingTimeInterval(20))
        #expect(activity.current.stage == "meeting_evidence")
        #expect(activity.current.startedAt == start.addingTimeInterval(10))
        #expect(activity.steps.count == 3)
        activity.receive(stage: "transcribe", message: "转录", progress: .nan)
        #expect(activity.progress == nil)
        activity.receive(stage: "transcribe", message: "转录", progress: 1.1)
        #expect(activity.progress == 1)
    }

    @Test func cancellationCannotBeOverwrittenByQueuedWorkerEvents() {
        var activity = ProcessingActivity(message: "准备", at: start)
        activity.cancel()
        activity.receive(stage: "completed", message: "晚到事件", progress: 1)
        #expect(activity.outcome == .cancelling)
        #expect(activity.current.stage == "prepare")
        activity.finish(.cancelled, at: start.addingTimeInterval(45))
        #expect(activity.elapsed(at: start.addingTimeInterval(100)) == 45)
        activity.finish(.succeeded, at: start.addingTimeInterval(110))
        #expect(activity.outcome == .cancelled)
        #expect(activity.elapsed() == 45)
    }

    @Test func boundedHistoryAndElapsedSurviveLongSessions() {
        var activity = ProcessingActivity(message: "准备", at: start)
        for i in 0..<100 { activity.receive(stage: "stage-\(i)", message: "阶段", progress: nil, at: start.addingTimeInterval(Double(i))) }
        #expect(activity.steps.count == 12)
        #expect(activity.current.id == 100)
        activity.finish(.failed, at: start.addingTimeInterval(3661))
        #expect(ProcessingActivity.duration(activity.elapsed()) == "1:01:01")
        #expect(ProcessingActivity.duration(-1) == "00:00")
    }

    @Test func indexPreservesSparseUnsortedAndDuplicateCitationSemantics() {
        let rows = [TranscriptSegment(id: 7, start: 20, end: 21, text: "甲"),
                    TranscriptSegment(id: 2, start: 3, end: 5, text: "乙"),
                    TranscriptSegment(id: 7, start: 80, end: 81, text: "重复编号")]
        let index = TranscriptTimeIndex(rows)
        #expect(index.start(for: 7) == 20)
        #expect(index.start(for: 9) == nil)
        #expect(index.range(for: []) == nil)
        #expect(index.range(for: [9]) == nil)
        #expect(index.range(for: [7, 99, 2, 7])?.first == 3)
        #expect(index.range(for: [7, 99, 2, 7])?.last == 20)
        let updated = TranscriptTimeIndex([TranscriptSegment(id: 7, start: 30, end: 31, text: "修改后")])
        #expect(updated.start(for: 7) == 30)
        #expect(updated.start(for: 2) == nil)
    }
}
