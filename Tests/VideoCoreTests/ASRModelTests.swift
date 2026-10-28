import Foundation
import Testing
@testable import VideoCore

@Suite("Local ASR model settings")
struct ASRModelTests {
    @Test func taskSelectionFreezesWhenTranscriptionStartsAndLegacyRequestsRemainReadable() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        var request = ProcessingRequest(title: "test", url: "", source: "", segments: [], useWhisper: true,
            mediaMode: "needed", keepMedia: true, videoFormatID: "", audioFormatID: "", videoIncludesAudio: false,
            container: "mp4", whisperModel: "small")
        request.sourceKind = .recording; request.recordingAction = "transcribe"
        let old = try JSONDecoder().decode(ProcessingRequest.self, from: JSONEncoder().encode(request))
        #expect(old.asrModelID == nil && old.effectiveASRModelID == "small")
        #expect(ASRTaskSelection.needsModel(for: old, directory: folder))
        let changed = try ASRTaskSelection.selecting("qwen3-asr-1.7b", in: old, directory: folder)
        #expect(changed.effectiveASRModelID == "qwen3-asr-1.7b")
        try Data("{}".utf8).write(to: folder.appendingPathComponent("transcription-model.json"))
        #expect(ASRTaskSelection.isFrozen(at: folder))
        #expect(throws: (any Error).self) { try ASRTaskSelection.selecting("turbo", in: changed, directory: folder) }
        #expect(try ASRTaskSelection.selecting("qwen3-asr-1.7b", in: changed, directory: folder).effectiveASRModelID == changed.effectiveASRModelID)
        try Data("[]".utf8).write(to: folder.appendingPathComponent("asr-transcript.json"))
        #expect(!ASRTaskSelection.needsModel(for: changed, directory: folder))
        request.recordingAction = "summarize"
        #expect(!ASRTaskSelection.needsModel(for: request, directory: folder))
        request.sourceKind = .bilibili; request.useWhisper = false
        #expect(!ASRTaskSelection.needsModel(for: request, directory: folder))
    }

    @Test func legacyCheckpointsAlsoFreezeSelection() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let checkpoint = folder.appendingPathComponent("checkpoints/whisper/old/0000.json")
        try FileManager.default.createDirectory(at: checkpoint.deletingLastPathComponent(), withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        #expect(!ASRTaskSelection.isFrozen(at: folder))
        try Data("[]".utf8).write(to: checkpoint)
        #expect(ASRTaskSelection.isFrozen(at: folder))
    }

    @Test func legacyPreferencesMigrateOnlyNewDefaultAndPreserveCloudSettings() throws {
        var original = AppPreferences()
        original.general.whisperModel = "small"
        original.providers[0].apiKey = "private-test-only"
        var raw = try JSONSerialization.jsonObject(with: JSONEncoder().encode(original)) as! [String: Any]
        var general = raw["general"] as! [String: Any]
        general.removeValue(forKey: "defaultASRModelID")
        general.removeValue(forKey: "asrLegacyNoticePending")
        raw["general"] = general
        var restored = try JSONDecoder().decode(AppPreferences.self, from: JSONSerialization.data(withJSONObject: raw))
        #expect(restored.general.defaultASRModelID == nil)
        #expect(restored.general.effectiveASRModelID == "turbo")
        let migrated = restored.migrateASRPreferences()
        let repeated = restored.migrateASRPreferences()
        #expect(migrated && !repeated)
        #expect(restored.general.asrLegacyNoticePending == true)
        #expect(restored.general.whisperModel == "small")
        #expect(restored.providers == original.providers)
        try restored.validate()
    }

    @Test func selectingASRDoesNotCommitOrDiscardOtherDraftFields() throws {
        let original = AppPreferences()
        var draft = original
        draft.general.textSize = 19
        draft.providers[0].apiKey = "unsaved-key-test-only"
        draft.providers[0].models[0].pricing = TokenPricing(input: 7, output: 9)
        var saved = original
        try saved.setDefaultASRModel("qwen3-asr-1.7b")
        draft.mergeASRPreferences(from: saved)
        #expect(saved.providers == original.providers)
        #expect(saved.general.textSize == original.general.textSize)
        #expect(draft.providers[0].apiKey == "unsaved-key-test-only")
        #expect(draft.providers[0].models[0].pricing.inputPerMillion == 7)
        #expect(draft.general.textSize == 19)
        #expect(draft.general.effectiveASRModelID == "qwen3-asr-1.7b")
        #expect(saved.general.whisperModel == "turbo")
        #expect(throws: (any Error).self) { try saved.setDefaultASRModel("small") }
        #expect(throws: (any Error).self) { try saved.setDefaultASRModel("unknown-model") }
    }

    @Test func unknownFutureASRDoesNotMakeCloudPreferencesUnreadable() throws {
        var value = AppPreferences()
        value.general.defaultASRModelID = "future-model"
        try value.validate()
        let decoded = try JSONDecoder().decode(AppPreferences.self, from: JSONEncoder().encode(value))
        #expect(decoded.general.effectiveASRModelID == "future-model")
        #expect(decoded.providers == value.providers)
    }

    @Test func statusSeparatesInstalledDefaultAndActiveUse() throws {
        let json = #"{"id":"turbo","name":"Turbo","state":"ready","downloadBytes":100,"installedBytes":100,"partialBytes":30,"inUseTask":"示例任务","components":[]}"#
        let model = try JSONDecoder().decode(LocalASRModel.self, from: Data(json.utf8))
        #expect(model.isReady && model.isInUse)
        #expect(model.reclaimableBytes == 130)
        let unknown = json.replacingOccurrences(of: "\"ready\"", with: "\"future-state\"")
        let future = try JSONDecoder().decode(LocalASRModel.self, from: Data(unknown.utf8))
        #expect(!future.isReady && future.state == .unknown)
    }

    @Test func progressParserHandlesSplitUTF8AndPreservesInventory() throws {
        let line = "{\"stage\":\"progress\",\"modelID\":\"turbo\",\"state\":\"downloading\",\"message\":\"正在下载模型\",\"progress\":0.5}\n"
        let data = Data(line.utf8)
        let buffer = ASRModelEventBuffer()
        var events: [ASRModelEvent] = []
        for byte in data { buffer.append(Data([byte])) { events.append($0) } }
        #expect(events.count == 1 && events[0].message == "正在下载模型")
        #expect(events[0].progress == 0.5)
        buffer.append(Data("{\"stage\":\"models\",\"models\":[]}\n".utf8)) { events.append($0) }
        #expect(buffer.result?.isEmpty == true)
        buffer.append(Data("{\"stage\":\"error\",\"message\":\"磁盘空间不足\"}\n".utf8)) { events.append($0) }
        #expect(buffer.failure == "磁盘空间不足")
    }

    @Test func catalogSumsAllComponentsWithoutMarkingWeightsInstalled() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let path = folder.appendingPathComponent("catalog.json")
        let raw = #"{"schemaVersion":1,"models":[{"id":"qwen3-asr-1.7b","name":"Qwen3-ASR 1.7B","subtitle":"含时间对齐组件","engine":"mlx-audio","license":"Apache-2.0","legacy":false,"components":[{"id":"asr","name":"识别","repository":"example/asr","revision":"a","files":[{"bytes":100},{"bytes":20}]},{"id":"aligner","name":"时间对齐","repository":"example/aligner","revision":"b","files":[{"bytes":80}]}]}]}"#
        try Data(raw.utf8).write(to: path)
        let models = try ASRModelCatalog.load(from: path)
        #expect(models.count == 1)
        #expect(models[0].downloadBytes == 200 && models[0].components.count == 2)
        #expect(models[0].installedBytes == 0 && !models[0].isReady)
    }
}
