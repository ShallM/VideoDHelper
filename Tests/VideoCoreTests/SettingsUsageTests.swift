import Foundation
import Testing
@testable import VideoCore

@Suite("Settings and accounting")
struct SettingsUsageTests {
    @Test func olderPreferencesRequestsAndNotesKeepTheirMeaning() throws {
        var prefs = AppPreferences()
        var object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(prefs)) as! [String: Any]
        var general = object["general"] as! [String: Any]
        general.removeValue(forKey: "summaryStrategy"); object["general"] = general
        let restored = try JSONDecoder().decode(AppPreferences.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(restored.general.effectiveSummaryStrategy == "structured")
        prefs.general.effectiveSummaryStrategy = "legacy"
        try prefs.validate()
        prefs.general.effectiveSummaryStrategy = "unknown"
        #expect(throws: (any Error).self) { try prefs.validate() }
        let request = ProcessingRequest(title: "source", url: "", source: "字幕", segments: [], useWhisper: false,
            mediaMode: "needed", keepMedia: false, videoFormatID: "", audioFormatID: "", videoIncludesAudio: false,
            container: "mp4", whisperModel: "turbo")
        var raw = try JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as! [String: Any]
        #expect(request.summaryStrategy == "structured")
        raw.removeValue(forKey: "summaryStrategy")
        let old = try JSONDecoder().decode(ProcessingRequest.self, from: JSONSerialization.data(withJSONObject: raw))
        #expect(old.summaryStrategy == nil) // Worker routes old requests to legacy.
        let notes = Data(#"{"overview":"旧概览","chapters":[{"title":"章节","detail":"内容","source_ids":[0]}],"key_points":[],"uncertainties":[],"model":"old","tokensThisRun":12}"#.utf8)
        let decoded = try JSONDecoder().decode(StudyNotes.self, from: notes)
        #expect(decoded.summaryStrategy == nil && decoded.chapters[0].claims == nil)
    }
    @Test func cacheRateWeightsTokensAndExcludesUnknownReports() {
        func event(_ raw: [String: Any]) -> UsageEvent { var event = UsageEvent(); event.usage = TokenUsage(response: raw); return event }
        let first = event(["prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 80])
        let second = event(["prompt_tokens": 900, "completion_tokens": 10, "prompt_cache_hit_tokens": 90])
        let unknown = event(["prompt_tokens": 5_000, "completion_tokens": 10])
        let invalid = event(["prompt_tokens": 10, "prompt_cache_hit_tokens": 20])
        let totals = UsageTotals(events: [first, second, unknown, invalid], preferences: AppPreferences(), currentPrices: false)
        #expect(totals.cacheMeasuredCalls == 2 && totals.cacheMeasuredInput == 1000)
        #expect(totals.cached == 170 && totals.cacheMisses == 830)
        #expect(abs(totals.cacheHitRate! - 0.17) < 0.000001)
        #expect(first.usage.cacheHitRate == 0.8 && unknown.usage.cacheHitRate == nil && invalid.usage.cacheHitRate == nil)
        let empty = event(["prompt_tokens": 0, "prompt_cache_hit_tokens": 0])
        #expect(UsageTotals(events: [empty], preferences: AppPreferences(), currentPrices: false).cacheHitRate == nil)
        #expect(UsageTotals(events: [unknown], preferences: AppPreferences(), currentPrices: false).reportedCacheCalls == 0)
    }
    @Test func manualPricesNormalizeUnitsWithoutGuessingBlanks() throws {
        let perMillion = try TokenPricing.fromInput(currency: "CNY", input: "2", output: "8", cached: "0.2")
        let perThousand = try TokenPricing.fromInput(currency: "CNY", input: " 0.002 ", output: "0.008", cached: "0.0002", tokensPerUnit: 1000)
        #expect(perMillion == perThousand)
        let blank = try TokenPricing.fromInput(currency: "USD", input: "", output: "0", cached: "")
        #expect(blank.inputPerMillion == nil && blank.outputPerMillion == 0 && blank.cachedInputPerMillion == nil)
        #expect(throws: (any Error).self) { try TokenPricing.fromInput(currency: "CNY", input: "-1", output: "8", cached: "") }
        #expect(throws: (any Error).self) { try TokenPricing.fromInput(currency: "CNY", input: "nan", output: "8", cached: "") }
        #expect(throws: (any Error).self) { try TokenPricing.fromInput(currency: "CNY", input: "2000000", output: "8", cached: "", tokensPerUnit: 1000) }
    }
    private func temporary() throws -> URL {
        let path = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: path, withIntermediateDirectories: true)
        return path
    }
    @Test func migrationAndPersistencePreserveCredentialsAndPrices() throws {
        let root = try temporary(); defer { try? FileManager.default.removeItem(at: root) }
        let path = root.appendingPathComponent("preferences.json")
        var legacy = APISettings(); legacy.apiKey = "test-only"; legacy.model = "demo-model"
        var preferences = try AppPreferences.load(from: path, legacy: legacy)
        #expect(preferences.defaultModel?.modelID == "demo-model")
        #expect(preferences.defaultProvider?.apiKey == "test-only")
        preferences.general.keepMedia = true
        preferences.providers[0].models[0].pricing = TokenPricing(input: 2, output: 8, cached: 0.2)
        try preferences.save(to: path)
        #expect(try AppPreferences.load(from: path) == preferences)
        #expect((try FileManager.default.attributesOfItem(atPath: path.path)[.posixPermissions] as? Int) == 0o600)
        try Data("broken".utf8).write(to: path)
        #expect(throws: (any Error).self) { try AppPreferences.load(from: path, legacy: legacy) }
        #expect(try String(contentsOf: path, encoding: .utf8) == "broken")
    }
    @Test func defaultModelCannotBecomeDanglingOrDisabled() throws {
        var preferences = AppPreferences()
        #expect(throws: (any Error).self) { try preferences.removeProvider(preferences.defaultProviderID) }
        #expect(throws: (any Error).self) { try preferences.removeModel(preferences.defaultModelID, from: preferences.defaultProviderID) }
        preferences.providers[0].enabled = false
        #expect(throws: (any Error).self) { try preferences.validate() }
        var second = ModelProvider(name: "Local", baseURL: "http://127.0.0.1:8123/v1")
        second.models = [LanguageModel(modelID: "local-model")]; preferences.providers.append(second)
        try preferences.setDefault(providerID: second.id, modelID: second.models[0].id)
        try preferences.validate()
        try preferences.removeProvider(preferences.providers[0].id)
        #expect(preferences.providers.count == 1)
        preferences.providers[0].models.append(LanguageModel(modelID: "local-model"))
        #expect(throws: (any Error).self) { try preferences.validate() }
    }
    @Test func endpointNormalizationAndCredentialBoundaries() throws {
        #expect(try APIEndpoint.base(" https://example.com/v1/chat/completions/ ").absoluteString == "https://example.com/v1")
        #expect(try APIEndpoint.url("https://example.com/proxy/v1", path: "models").absoluteString == "https://example.com/proxy/v1/models")
        #expect(try APIEndpoint.base("http://localhost:11434/v1").host == "localhost")
        #expect(try APIEndpoint.base("http://[::1]:8123/v1").scheme == "http")
        for value in ["http://example.com/v1", "https://key@example.com/v1", "https://example.com/v1?key=secret", "https://example.com/v1#secret", "http://127.0.0.1.evil.test/v1"] {
            #expect(throws: (any Error).self) { try APIEndpoint.base(value) }
        }
    }
    @Test func sharedNormalizationFixtures() throws {
        let file = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Fixtures/token-usage.json")
        let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as! [[String: Any]]
        for item in cases {
            let usage = TokenUsage(response: item["raw"] as! [String: Any])
            let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(usage)) as! NSDictionary
            #expect(encoded == item["expected"] as! NSDictionary, Comment(rawValue: item["name"] as! String))
        }
    }
    @Test func cacheAndReasoningAreNotDoubleCharged() throws {
        let usage = TokenUsage(response: ["prompt_tokens": 1000, "completion_tokens": 400, "total_tokens": 1400,
                                          "prompt_tokens_details": ["cached_tokens": 300], "completion_tokens_details": ["reasoning_tokens": 250]])
        let price = TokenPricing(input: 2, output: 8, cached: 0.2)
        #expect(abs(price.estimate(usage)! - 0.00466) < 0.00000001)
        #expect(usage.totalTokens == 1400)
        let withoutCache = TokenUsage(response: ["prompt_tokens": 1000, "completion_tokens": 400])
        #expect(price.estimate(withoutCache) == nil)
        #expect(abs(TokenPricing(input: 2, output: 8).estimate(withoutCache)! - 0.0052) < 0.00000001)
        #expect(TokenPricing().estimate(usage) == nil)
        #expect(TokenPricing(input: 0, output: 0).estimate(usage) == 0)
        #expect(price.estimate(TokenUsage()) == nil)
        let unknownTotals = UsageTotals(events: [UsageEvent()], preferences: AppPreferences(), currentPrices: false)
        #expect(unknownTotals.reportedInputCalls == 0 && unknownTotals.reportedOutputCalls == 0)
        var zero = UsageEvent(); zero.usage = TokenUsage(response: ["prompt_tokens": 0, "completion_tokens": 0])
        let freeTotals = UsageTotals(events: [zero], preferences: AppPreferences(), currentPrices: false)
        #expect(freeTotals.reportedInputCalls == 1 && freeTotals.input == 0 && freeTotals.reportedCacheCalls == 0)
        #expect(throws: (any Error).self) { try TokenPricing(input: -1, output: 8).validate() }
    }
    @Test func snapshotsAndCurrenciesRemainSeparateAfterPriceChanges() throws {
        var preferences = AppPreferences()
        preferences.providers[0].models[0].pricing = TokenPricing(input: 2, output: 8)
        var event = UsageEvent(provider: preferences.providers[0], model: preferences.providers[0].models[0])
        event.usage = TokenUsage(response: ["prompt_tokens": 1_000_000, "completion_tokens": 1_000_000])
        preferences.providers[0].models[0].pricing = TokenPricing(input: 3, output: 9)
        #expect(event.cost(in: preferences, current: false) == 10)
        #expect(event.cost(in: preferences, current: true) == 12)
        var dollar = event; dollar.pricing = TokenPricing(currency: "USD", input: 1, output: 3)
        let totals = UsageTotals(events: [event, dollar, UsageEvent()], preferences: preferences, currentPrices: false)
        #expect(totals.costs == ["CNY": 10, "USD": 4])
        #expect(totals.pricedCalls == 2 && totals.unpricedCalls == 1 && totals.unknownUsageCalls == 1)
        preferences.providers[0].models[0].modelID = "another-model"
        #expect(event.cost(in: preferences, current: true) == nil)
    }
    @Test func legacyRecordsAreUnknownAndNeverDoubleCounted() throws {
        let root = try temporary(); defer { try? FileManager.default.removeItem(at: root) }
        let id = UUID().uuidString, folder = root.appendingPathComponent("tasks/\(id)/checkpoints/summary")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let preferences = AppPreferences()
        var event = UsageEvent(provider: preferences.providers[0], model: preferences.providers[0].models[0]); event.status = "response"
        event.usage = TokenUsage(response: ["prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5])
        try UsageRepository.save(event, root: root)
        let lines = "{\"time\":100,\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":20}}\n{\"time\":101,\"eventID\":\"\(event.id)\",\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2}}\n{broken\n"
        try lines.write(to: folder.appendingPathComponent("usage.jsonl"), atomically: true, encoding: .utf8)
        let first = UsageRepository.load(root: root), second = UsageRepository.load(root: root)
        #expect(first.events.count == 2 && first.unreadableRecords == 1)
        #expect(first.events.map(\.id) == second.events.map(\.id))
        let historical = first.events.first { $0.id.hasPrefix("legacy-") }!
        #expect(historical.modelID == nil && historical.providerID == nil && historical.pricing == nil)
        #expect(UsageTotals(events: first.events, preferences: preferences, currentPrices: false).input == 13)
        #expect(try String(contentsOf: folder.appendingPathComponent("usage.jsonl"), encoding: .utf8) == lines)
    }
    @Test func csvPreservesUnknownsAndEscapesSpreadsheetFormulas() {
        var event = UsageEvent(); event.taskTitle = "=SUM(1,2)"; event.modelID = "model\"quoted"; event.status = "network_error"
        let csv = UsageRepository.csv([event], preferences: AppPreferences(), currentPrices: false)
        #expect(csv.contains("\"'=SUM(1,2)\""))
        #expect(csv.contains("model\"\"quoted"))
        #expect(csv.contains("连接失败 · 用量未知"))
        #expect(!csv.contains("0.00000000"))
    }
}
