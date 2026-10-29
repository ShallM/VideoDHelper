import Foundation
import Darwin
import Testing
@testable import VideoCore

private struct DialogueComparisonFixture {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    let job = ProcessingJob(title: "同一段普通话", url: "", source: "手机录音", summaryStrategy: "structured", workspace: .recording)
    let raw = Data(#"[{"id":0,"start":0,"end":1.2,"text":"下周再说。","speaker":"A","words":[{"word":"下周","start":0,"end":0.5}]}]"#.utf8)
    let edits = RecordingEdits(segments: [TranscriptSegment(id: 0, start: 0, end: 1.2, text: "下周再说吧。", speaker: "B")], speakers: ["A": "甲", "B": "乙"])

    init() throws {
        try AppStorage.write(job, to: folder.appendingPathComponent("job.json"))
        var request = ProcessingRequest(title: job.title, url: "", source: job.source, segments: [], useWhisper: true,
            mediaMode: "needed", keepMedia: true, videoFormatID: "", audioFormatID: "", videoIncludesAudio: false,
            container: "m4a", whisperModel: "large-v3-turbo")
        request.sourceKind = .recording; request.recordingAction = "summarize"
        request.localAudioFile = "source/original.m4a"; request.diarizationMode = "two"
        try AppStorage.write(request, to: folder.appendingPathComponent("request.json"))
        try raw.write(to: folder.appendingPathComponent("transcript.json"))
        try AppStorage.write(edits, to: folder.appendingPathComponent("transcript-edits.json"))
        try AppStorage.write(edits.speakers, to: folder.appendingPathComponent("speakers.json"))
        try FileManager.default.createDirectory(at: folder.appendingPathComponent("source"), withIntermediateDirectories: true)
        try Data("independent audio bytes".utf8).write(to: folder.appendingPathComponent("source/original.m4a"))
        for name in ["asr-transcript.json", "recording-source.json", "transcription-model.json", "transcription-runtime.json", "recording-transcript.json", "diarization.json"] {
            try AppStorage.write(["fixture": name], to: folder.appendingPathComponent(name))
        }
        for name in ["notes.json", "condensed.json", "dialogue-results.json", "dialogue-checkpoint.json", "summary-checkpoint.json", "usage.jsonl"] {
            try Data("source result must not be reused".utf8).write(to: folder.appendingPathComponent(name))
        }
    }
    var folder: URL { directory(job.id) }
    func directory(_ id: String) -> URL { root.appendingPathComponent("recording-tasks/\(id)") }
    func cleanup() { try? FileManager.default.removeItem(at: root) }
    func taskIDs() throws -> [String] {
        try FileManager.default.contentsOfDirectory(at: root.appendingPathComponent("recording-tasks"), includingPropertiesForKeys: nil).map(\.lastPathComponent).sorted()
    }
}

struct DialogueStrategyTests {
    @Test func missingStrategyPreservesLegacyAndExplicitNewStrategyRoundTrips() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        var request = try AppStorage.read(ProcessingRequest.self, from: fixture.folder.appendingPathComponent("request.json"))
        #expect(request.dialogueStrategy == nil)
        #expect(request.effectiveDialogueStrategy == "legacy")
        let original = try AppStorage.read(ProcessingJob.self, from: fixture.folder.appendingPathComponent("job.json"))
        #expect(original.dialogueStrategy == nil)
        #expect(original.summaryStrategyLabel == "旧版对话流程")
        request.dialogueStrategy = "context-v2"
        let restored = try JSONDecoder().decode(ProcessingRequest.self, from: JSONEncoder().encode(request))
        #expect(restored.effectiveDialogueStrategy == "context-v2")
        #expect(DialogueStrategy.label(for: restored.dialogueStrategy) == "全文对话理解 · 预览")
    }

    @Test func oldSingleSpeakerAndNewMixedPartsRemainReadableWithoutInventingThirdSpeaker() throws {
        let decoder = JSONDecoder()
        let old = try decoder.decode(CitedText.self, from: Data(#"{"source_ids":[0],"text":"以后再说。","speaker":"A"}"#.utf8))
        #expect(old.id == nil && old.parts == nil)
        #expect(old.dialogueText(names: ["A": "甲"]) == "甲：以后再说。")
        let mixed = try decoder.decode(CitedText.self, from: Data(#"{"id":"dialogue-0","source_ids":[0,1,2],"text":"嗯，下周再说吧。","parts":[{"id":"part-0","speaker":"unknown","text":"嗯，","source_ids":[0]},{"id":"part-1","speaker":"A","text":"下周再说","source_ids":[1]},{"id":"part-2","speaker":"B","text":"吧。","source_ids":[2]}]}"#.utf8))
        #expect(mixed.id == "dialogue-0")
        #expect(mixed.speaker == nil)
        #expect(mixed.parts?.map(\.id) == ["part-0", "part-1", "part-2"])
        #expect(mixed.parts?.map(\.text).joined() == mixed.text)
        #expect(mixed.dialogueText(names: ["A": "甲", "B": "乙", "unknown": "不能当第三位"]) == "[归属未确定] 嗯， [甲] 下周再说 [乙] 吧。")
        #expect(mixed.parts?.first?.source_ids == [0])
        let continuous = try decoder.decode(CitedText.self, from: Data(#"{"source_ids":[0],"text":"下周再说。","parts":[{"id":"p-0","speaker":"A","text":"下周","source_ids":[0]},{"id":"p-1","speaker":"A","text":"再说。","source_ids":[0]}]}"#.utf8))
        #expect(continuous.dialogueText(names: ["A": "甲"]) == "[甲] 下周再说。")
    }

    @Test func structuredReviewMetadataAndClaimIDsDecodeWithExistingNotes() throws {
        let data = Data(#"{"overview":"两人谈了后续安排。","overviewSourceIDs":[0],"chapters":[{"title":"下一步","detail":"仍待确认。","source_ids":[0],"claims":[{"id":"claim-0","text":"暂未定时间。","source_ids":[0]}]}],"key_points":[{"id":"key-0","text":"先试用。","source_ids":[0]}],"uncertainties":[],"model":"test","tokensThisRun":20,"summaryStrategy":"context-v2","summaryRoute":"hierarchical","reviewedParagraphs":3,"paragraphCount":3,"rereadCount":1,"recordingRevision":"original"}"#.utf8)
        let notes = try JSONDecoder().decode(StudyNotes.self, from: data)
        #expect(notes.summaryStrategy == "context-v2")
        #expect(notes.summaryRoute == "hierarchical")
        #expect(notes.chapters.first?.claims?.first?.id == "claim-0")
        #expect(notes.key_points.first?.id == "key-0")
        #expect(notes.reviewedParagraphs == notes.paragraphCount)
        #expect(notes.overviewSourceIDs == [0])
    }

    @Test func comparisonCopiesExactInputsButKeepsIdentityResultsUsageAndAudioIndependent() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        let requestBefore = try Data(contentsOf: fixture.folder.appendingPathComponent("request.json"))
        let jobBefore = try Data(contentsOf: fixture.folder.appendingPathComponent("job.json"))
        let copy = try RecordingComparison.create(taskID: fixture.job.id, strategy: .contextV2, root: fixture.root)
        let target = fixture.directory(copy.id)
        #expect(copy.id != fixture.job.id)
        #expect(copy.dialogueStrategy == "context-v2")
        #expect(copy.state == "transcribed" && copy.workspace == .recording)
        #expect(copy.mediaFiles == ["source/original.m4a"])
        let request = try AppStorage.read(ProcessingRequest.self, from: target.appendingPathComponent("request.json"))
        #expect(request.recordingAction == "summarize" && request.effectiveDialogueStrategy == "context-v2")
        #expect(request.whisperModel == "large-v3-turbo" && request.diarizationMode == "two")
        for name in ["transcript.json", "transcript-edits.json", "speakers.json", "asr-transcript.json", "recording-source.json", "transcription-model.json", "transcription-runtime.json", "recording-transcript.json", "diarization.json", "source/original.m4a"] {
            #expect(try Data(contentsOf: target.appendingPathComponent(name)) == Data(contentsOf: fixture.folder.appendingPathComponent(name)))
        }
        for name in ["notes.json", "condensed.json", "dialogue-results.json", "dialogue-checkpoint.json", "summary-checkpoint.json", "usage.jsonl", "run.lock"] {
            #expect(!FileManager.default.fileExists(atPath: target.appendingPathComponent(name).path))
        }
        let copiedEdits = try AppStorage.read(RecordingEdits.self, from: target.appendingPathComponent("transcript-edits.json"))
        #expect(copiedEdits.revision == fixture.edits.revision)
        try Data("changed new audio".utf8).write(to: target.appendingPathComponent("source/original.m4a"))
        #expect(try Data(contentsOf: fixture.folder.appendingPathComponent("source/original.m4a")) == Data("independent audio bytes".utf8))
        #expect(try Data(contentsOf: fixture.folder.appendingPathComponent("request.json")) == requestBefore)
        #expect(try Data(contentsOf: fixture.folder.appendingPathComponent("job.json")) == jobBefore)
        #expect(try Data(contentsOf: fixture.folder.appendingPathComponent("transcript.json")) == fixture.raw)
    }

    @Test func lockedOrArchivedSourceCannotProduceComparison() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        let descriptor = open(fixture.folder.appendingPathComponent("run.lock").path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        #expect(descriptor >= 0); defer { close(descriptor) }
        #expect(flock(descriptor, LOCK_EX | LOCK_NB) == 0)
        #expect(throws: (any Error).self) { try RecordingComparison.create(taskID: fixture.job.id, strategy: .contextV2, root: fixture.root) }
        #expect(flock(descriptor, LOCK_UN) == 0)
        _ = try TaskLifecycle.setArchived(true, taskID: fixture.job.id, root: fixture.root, workspace: .recording)
        #expect(throws: (any Error).self) { try RecordingComparison.create(taskID: fixture.job.id, strategy: .contextV2, root: fixture.root) }
        #expect(try fixture.taskIDs() == [fixture.job.id])
    }

    @Test func invalidMediaPathOrEditedTranscriptRollsBackWithoutPublishingPartialTask() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        let requestURL = fixture.folder.appendingPathComponent("request.json")
        var request = try AppStorage.read(ProcessingRequest.self, from: requestURL)
        request.localAudioFile = "source/../private.m4a"
        try AppStorage.write(request, to: requestURL)
        #expect(throws: (any Error).self) { try RecordingComparison.create(taskID: fixture.job.id, strategy: .contextV2, root: fixture.root) }
        #expect(try fixture.taskIDs() == [fixture.job.id])
        request.localAudioFile = "source/original.m4a"
        try AppStorage.write(request, to: requestURL)
        let badEdits = RecordingEdits(segments: [TranscriptSegment(id: 99, start: 0, end: 1.2, text: "不匹配", speaker: "A")], speakers: [:])
        try AppStorage.write(badEdits, to: fixture.folder.appendingPathComponent("transcript-edits.json"))
        #expect(throws: (any Error).self) { try RecordingComparison.create(taskID: fixture.job.id, strategy: .contextV2, root: fixture.root) }
        #expect(try fixture.taskIDs() == [fixture.job.id])
    }

    @Test func cleanedUpSourceAudioDoesNotPreventTextOnlyComparison() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        try FileManager.default.removeItem(at: fixture.folder.appendingPathComponent("source/original.m4a"))
        let copy = try RecordingComparison.create(taskID: fixture.job.id, strategy: .legacy, root: fixture.root)
        #expect(copy.dialogueStrategy == "legacy")
        #expect(copy.mediaFiles.isEmpty)
        #expect(try Data(contentsOf: fixture.directory(copy.id).appendingPathComponent("transcript.json")) == fixture.raw)
    }

    @Test func symlinkedSourceAudioIsRejectedInsteadOfCopiedFromOutsideTask() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        let audio = fixture.folder.appendingPathComponent("source/original.m4a")
        let external = fixture.root.appendingPathComponent("outside.m4a")
        try Data("outside".utf8).write(to: external)
        try FileManager.default.removeItem(at: audio)
        try FileManager.default.createSymbolicLink(at: audio, withDestinationURL: external)
        #expect(throws: (any Error).self) { try RecordingComparison.create(taskID: fixture.job.id, strategy: .contextV2, root: fixture.root) }
        #expect(try fixture.taskIDs() == [fixture.job.id])
        #expect(try Data(contentsOf: external) == Data("outside".utf8))
    }

    @Test func dialogueUsagePhasesRemainDistinctFromVideoReadingAndBilling() {
        let expected = ["dialogue_summary": "对话全文理解", "dialogue_merge": "对话分层综合",
                        "dialogue_condense": "独立整理原话", "dialogue_review": "对照原文复核",
                        "dialogue_reread": "对话定向回读"]
        for (phase, label) in expected {
            var event = UsageEvent(purpose: "summary"); event.sourceKind = "recording"; event.phase = phase
            #expect(event.phaseLabel == label)
            #expect(event.sourceLabel == "录音整理")
            #expect(event.usage.totalTokens == nil)
        }
    }

    @Test func atomicResultsBundleTakesPriorityOverOlderIndividualFiles() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        var request = try AppStorage.read(ProcessingRequest.self, from: fixture.folder.appendingPathComponent("request.json"))
        request.dialogueStrategy = DialogueStrategy.contextV2.rawValue
        try AppStorage.write(request, to: fixture.folder.appendingPathComponent("request.json"))
        let notes = #"{"overview":"同一代概览","chapters":[],"key_points":[],"uncertainties":[{"text":"请回听原话。","source_ids":[0],"status":"needs_audio"}],"model":"test","tokensThisRun":0,"summaryStrategy":"context-v2"}"#
        let data = Data("{\"schemaVersion\":1,\"generationID\":\"new-generation\",\"notes\":\(notes),\"condensed\":[{\"text\":\"同一代整理原话\",\"source_ids\":[0]}]}".utf8)
        try data.write(to: fixture.folder.appendingPathComponent("dialogue-results.json"))
        let result = try RecordingResults.load(from: fixture.folder)
        #expect(result.notes?.overview == "同一代概览")
        #expect(result.notes?.uncertainties.first?.status == "needs_audio")
        #expect(result.condensed.first?.text == "同一代整理原话")
        let bundle = try JSONDecoder().decode(DialogueResultsBundle.self, from: data)
        #expect(bundle.generationID == "new-generation")
        try FileManager.default.removeItem(at: fixture.folder.appendingPathComponent("request.json"))
        #expect(try RecordingResults.load(from: fixture.folder).notes?.overview == "同一代概览")
    }

    @Test func rollbackStrategyConflictPreservesEveryResultAndAllowsIndependentRecovery() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        let notes = #"{"overview":"回退前的全文理解成果","chapters":[],"key_points":[],"uncertainties":[],"model":"test","tokensThisRun":0,"summaryStrategy":"context-v2"}"#
        let bundle = Data("{\"schemaVersion\":1,\"generationID\":\"before-rollback\",\"notes\":\(notes),\"condensed\":[{\"text\":\"回退前整理稿\",\"source_ids\":[0]}]}".utf8)
        try bundle.write(to: fixture.folder.appendingPathComponent("dialogue-results.json"))
        let legacyNotes = Data(#"{"overview":"旧版重新生成的成果","chapters":[],"key_points":[],"uncertainties":[],"model":"test","tokensThisRun":0,"summaryStrategy":"structured"}"#.utf8)
        let legacyCondensed = Data(#"[{"text":"旧版重新生成的整理稿","source_ids":[0],"speaker":"A"}]"#.utf8)
        try legacyNotes.write(to: fixture.folder.appendingPathComponent("notes.json"))
        try legacyCondensed.write(to: fixture.folder.appendingPathComponent("condensed.json"))
        let requestURL = fixture.folder.appendingPathComponent("request.json")
        var request = try AppStorage.read(ProcessingRequest.self, from: requestURL)
        // The previous app rewrites its Codable request without the new field.
        for strategy in [nil, "legacy", "future-reader"] as [String?] {
            request.dialogueStrategy = strategy
            try AppStorage.write(request, to: requestURL)
            #expect(throws: (any Error).self) { try RecordingResults.load(from: fixture.folder) }
        }
        #expect(try Data(contentsOf: fixture.folder.appendingPathComponent("dialogue-results.json")) == bundle)
        #expect(try Data(contentsOf: fixture.folder.appendingPathComponent("notes.json")) == legacyNotes)
        #expect(try Data(contentsOf: fixture.folder.appendingPathComponent("condensed.json")) == legacyCondensed)
        #expect(try Data(contentsOf: fixture.folder.appendingPathComponent("transcript.json")) == fixture.raw)
        let comparison = try RecordingComparison.create(taskID: fixture.job.id, strategy: .contextV2, root: fixture.root)
        let recovered = try RecordingResults.load(from: fixture.directory(comparison.id))
        #expect(recovered.notes == nil && recovered.condensed.isEmpty)
        #expect(try Data(contentsOf: fixture.directory(comparison.id).appendingPathComponent("transcript.json")) == fixture.raw)
        #expect(try AppStorage.read(ProcessingRequest.self, from: fixture.directory(comparison.id).appendingPathComponent("request.json")).effectiveDialogueStrategy == "context-v2")
    }

    @Test func damagedOrFutureBundleNeverFallsBackToLegacyFiles() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        try FileManager.default.removeItem(at: fixture.folder.appendingPathComponent("dialogue-results.json"))
        let notes = #"{"overview":"旧概览不能冒充新结果","chapters":[],"key_points":[],"uncertainties":[],"model":"test","tokensThisRun":0}"#
        try Data(notes.utf8).write(to: fixture.folder.appendingPathComponent("notes.json"))
        try Data(#"[{"text":"旧整理稿","source_ids":[0]}]"#.utf8).write(to: fixture.folder.appendingPathComponent("condensed.json"))
        let legacy = try RecordingResults.load(from: fixture.folder)
        #expect(legacy.notes?.overview == "旧概览不能冒充新结果")
        #expect(legacy.condensed.first?.text == "旧整理稿")
        for value in ["{broken", "{\"schemaVersion\":2,\"generationID\":\"future\",\"notes\":\(notes),\"condensed\":[]}", "{\"schemaVersion\":1,\"generationID\":\"\",\"notes\":\(notes),\"condensed\":[]}"] {
            try Data(value.utf8).write(to: fixture.folder.appendingPathComponent("dialogue-results.json"))
            #expect(throws: (any Error).self) { try RecordingResults.load(from: fixture.folder) }
        }
        #expect(try String(contentsOf: fixture.folder.appendingPathComponent("notes.json"), encoding: .utf8) == notes)
    }

    @Test func meetingStrategiesCopyInputsWithoutRunningModelsOrChangingOldTask() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        let oldRequest = try Data(contentsOf: fixture.folder.appendingPathComponent("request.json"))
        for strategy in [DialogueStrategy.meetingV3, .meetingBaseline] {
            let comparison = try RecordingComparison.create(taskID: fixture.job.id, strategy: strategy, root: fixture.root)
            #expect(comparison.state == "transcribed")
            let target = fixture.directory(comparison.id)
            #expect(try Data(contentsOf: target.appendingPathComponent("transcript.json")) == fixture.raw)
            #expect(try AppStorage.read(ProcessingRequest.self, from: target.appendingPathComponent("request.json")).effectiveDialogueStrategy == strategy.rawValue)
            #expect(!FileManager.default.fileExists(atPath: target.appendingPathComponent("checkpoints").path))
            #expect(try Data(contentsOf: fixture.folder.appendingPathComponent("request.json")) == oldRequest)
        }
    }

    @Test func meetingBundlePreservesActionMetadataAndRejectsMismatchedStrategyEvenWithoutRequest() throws {
        let fixture = try DialogueComparisonFixture(); defer { fixture.cleanup() }
        let action = #"{"id":"action-0","text":"测试通过后发送结果。","source_ids":[0],"metadata":{"text":"发送结果。","kind":"action","state":"pending","conditions":["测试通过"],"scope":null,"speaker":"B","owner":"A","due":null}}"#
        let notes = "{\"overview\":\"资料已具备\",\"chapters\":[],\"key_points\":[],\"action_items\":[\(action)],\"uncertainties\":[],\"model\":\"test\",\"tokensThisRun\":0,\"summaryStrategy\":\"meeting-v3\",\"evidenceItemCount\":2,\"assessmentCount\":2}"
        let row = #"{"id":"row-0","text":"不。","source_ids":[0],"continuousReading":true,"parts":[{"id":"part-0","speaker":"unknown","text":"不。","source_ids":[0]}]}"#
        let bundle = Data("{\"schemaVersion\":2,\"generationID\":\"meeting-generation\",\"strategy\":\"meeting-v3\",\"notes\":\(notes),\"condensed\":[\(row)]}".utf8)
        try bundle.write(to: fixture.folder.appendingPathComponent("dialogue-results.json"))
        let requestURL = fixture.folder.appendingPathComponent("request.json")
        var request = try AppStorage.read(ProcessingRequest.self, from: requestURL)
        #expect(throws: (any Error).self) { try RecordingResults.load(from: fixture.folder) }
        request.dialogueStrategy = "meeting-v3"; try AppStorage.write(request, to: requestURL)
        let result = try RecordingResults.load(from: fixture.folder)
        #expect(result.notes?.isMeeting == true)
        #expect(result.notes?.action_items?.first?.metadata?.conditions == ["测试通过"])
        #expect(result.notes?.action_items?.first?.metadata?.due == nil)
        #expect(result.notes?.action_items?.first?.metadata?.attribution_parts == nil)
        let withAttribution = action.replacingOccurrences(of: "\"speaker\":\"B\"", with: "\"speaker\":\"B\",\"attribution_parts\":[3]")
        #expect(try JSONDecoder().decode(CitedText.self, from: Data(withAttribution.utf8)).metadata?.attribution_parts == [3])
        #expect(result.condensed.first?.continuousReading == true)
        #expect(result.condensed.first?.parts?.first?.speaker == "unknown")
        #expect(result.condensed.first?.dialogueText(names: [:]) == "[归属未确定] 不。")
        try FileManager.default.removeItem(at: requestURL)
        #expect(try RecordingResults.load(from: fixture.folder).notes?.isMeeting == true)
        let invalid = String(decoding: bundle, as: UTF8.self).replacingOccurrences(of: "\"strategy\":\"meeting-v3\"", with: "\"strategy\":\"meeting-baseline\"")
        try Data(invalid.utf8).write(to: fixture.folder.appendingPathComponent("dialogue-results.json"))
        #expect(throws: (any Error).self) { try RecordingResults.load(from: fixture.folder) }
    }

    @Test func meetingStagesKeepUsageUnknownUntilProviderReportsIt() {
        for phase in ["meeting_draft", "meeting_evidence", "meeting_review", "meeting_reread", "meeting_punctuation"] {
            var event = UsageEvent(purpose: "summary"); event.sourceKind = "recording"; event.phase = phase
            #expect(event.phaseLabel != "总结")
            #expect(event.sourceLabel == "录音整理")
            #expect(event.usage.totalTokens == nil)
        }
    }
}
