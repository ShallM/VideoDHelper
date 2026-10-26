import Foundation
import CoreFoundation
import CryptoKit

public struct TokenUsage: Codable, Equatable, Sendable {
    public var inputTokens: Int?
    public var outputTokens: Int?
    public var cachedInputTokens: Int?
    public var reasoningTokens: Int?
    public var reportedTotalTokens: Int?
    public var invalid = false
    public init() {}
    public var totalTokens: Int? {
        if let inputTokens, let outputTokens { return inputTokens + outputTokens }
        return reportedTotalTokens
    }
    public var cacheHitRate: Double? {
        guard !invalid, let inputTokens, inputTokens > 0, let cachedInputTokens else { return nil }
        return Double(cachedInputTokens) / Double(inputTokens)
    }
    public init(response: [String: Any]) {
        func number(_ raw: Any?) -> Int? {
            guard let raw, !(raw is NSNull) else { return nil }
            guard let n = raw as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite,
                  n.doubleValue >= 0, n.doubleValue <= 9_000_000_000_000_000, n.doubleValue.rounded() == n.doubleValue else {
                invalid = true; return nil
            }
            return n.intValue
        }
        inputTokens = number(response["prompt_tokens"] ?? response["input_tokens"])
        outputTokens = number(response["completion_tokens"] ?? response["output_tokens"])
        let inputDetails = response["prompt_tokens_details"] as? [String: Any] ?? [:]
        let outputDetails = response["completion_tokens_details"] as? [String: Any] ?? [:]
        cachedInputTokens = number(inputDetails["cached_tokens"] ?? response["prompt_cache_hit_tokens"])
        if inputTokens == nil, let hit = cachedInputTokens, let miss = number(response["prompt_cache_miss_tokens"]) { inputTokens = hit + miss }
        reasoningTokens = number(outputDetails["reasoning_tokens"])
        reportedTotalTokens = number(response["total_tokens"])
        if let cachedInputTokens, let inputTokens, cachedInputTokens > inputTokens { invalid = true }
        if let reasoningTokens, let outputTokens, reasoningTokens > outputTokens { invalid = true }
        if let inputTokens, let outputTokens, let reportedTotalTokens, inputTokens + outputTokens != reportedTotalTokens { invalid = true }
    }
}

public extension TokenPricing {
    func estimate(_ usage: TokenUsage) -> Double? {
        guard !usage.invalid, let input = usage.inputTokens, let output = usage.outputTokens,
              let inputRate = inputPerMillion, let outputRate = outputPerMillion,
              (try? validate()) != nil else { return nil }
        if let cacheRate = cachedInputPerMillion, cacheRate != inputRate, usage.cachedInputTokens == nil { return nil }
        let cached = usage.cachedInputTokens ?? 0
        return (Double(input - cached) * inputRate + Double(cached) * (cachedInputPerMillion ?? inputRate) + Double(output) * outputRate) / 1_000_000
    }
}

public struct UsageEvent: Codable, Identifiable, Sendable {
    public var schemaVersion = 1
    public var id: String
    public var timestamp: Double
    public var taskID: String?
    public var taskTitle: String?
    public var providerID: String?
    public var providerName: String?
    public var modelConfigID: String?
    public var modelID: String?
    public var modelName: String?
    public var purpose: String
    public var phase: String?
    public var status: String
    public var httpStatus: Int?
    public var finishReason: String?
    public var durationSeconds: Double?
    public var usage: TokenUsage
    public var pricing: TokenPricing?
    public init(id: String = UUID().uuidString, timestamp: Double = Date().timeIntervalSince1970,
                provider: ModelProvider? = nil, model: LanguageModel? = nil, purpose: String = "connection-test") {
        self.id = id; self.timestamp = timestamp; self.purpose = purpose; status = "in_flight"; usage = TokenUsage()
        providerID = provider?.id; providerName = provider?.name
        modelConfigID = model?.id; modelID = model?.modelID; modelName = model?.name; pricing = model?.pricing
    }
    public var displayModel: String { modelName ?? modelID ?? "历史未记录模型" }
    public var displayProvider: String { providerName ?? "历史未归属" }
    public var displayTask: String { taskTitle ?? (purpose == "connection-test" ? "模型连接测试" : "已移除的任务") }
    public var phaseLabel: String {
        switch phase {
        case "whole": return "短稿整体阅读"
        case "plan", "plan_merge": return "全文结构"
        case "chapter": return "主题笔记"
        case "verify": return "遗漏与依据核验"
        case "reread": return "定向回读"
        case "synthesis": return "综合概览"
        case "final_check": return "概览回查"
        case "chunk": return "旧版分块"
        case "stitch": return "旧版接缝合并"
        case "reduce": return "旧版分层压缩"
        case "overview": return "旧版概览"
        default: return purpose == "connection-test" ? "连接测试" : "总结"
        }
    }
    public var statusLabel: String {
        if usage.invalid { return "用量数据异常" }
        if finishReason == "length" { return "输出截断" }
        switch status {
        case "response": return usage.totalTokens == nil ? "已返回 · 用量未知" : "已返回"
        case "http_error": return "HTTP \(httpStatus.map(String.init) ?? "错误")"
        case "network_error": return "连接失败 · 用量未知"
        case "invalid_response": return "响应无效 · 用量未知"
        case "cancelled": return "已取消 · 用量未知"
        default: return "进行中或中断 · 用量未知"
        }
    }
    public func price(in preferences: AppPreferences, current: Bool) -> TokenPricing? {
        guard current else { return pricing }
        return preferences.providers.first { $0.id == providerID }?.models.first { $0.id == modelConfigID && $0.modelID == modelID }?.pricing
    }
    public func cost(in preferences: AppPreferences, current: Bool) -> Double? { price(in: preferences, current: current)?.estimate(usage) }
    public func unpricedReason(in preferences: AppPreferences, current: Bool) -> String {
        if usage.invalid { return "用量数据异常" }
        guard usage.inputTokens != nil, usage.outputTokens != nil else { return "用量未返回" }
        guard let price = price(in: preferences, current: current), price.isConfigured else { return "未配置价格" }
        return "缺少缓存用量"
    }
}

public struct UsageSnapshot: Sendable {
    public var events: [UsageEvent] = []
    public var unreadableRecords = 0
    public init() {}
}

public enum UsageRepository {
    public static func save(_ event: UsageEvent, root: URL = AppStorage.root) throws {
        guard UUID(uuidString: event.id) != nil else { throw VideoError.toolFailed("无效用量记录。") }
        try AppStorage.write(event, to: root.appendingPathComponent("usage/events/\(event.id).json"))
    }
    public static func load(root: URL = AppStorage.root) -> UsageSnapshot {
        var snapshot = UsageSnapshot(), seen = Set<String>()
        let files = (try? FileManager.default.contentsOfDirectory(at: root.appendingPathComponent("usage/events"), includingPropertiesForKeys: nil)) ?? []
        for file in files where file.pathExtension == "json" {
            do {
                let event = try AppStorage.read(UsageEvent.self, from: file)
                guard event.schemaVersion == 1, event.timestamp.isFinite, seen.insert(event.id).inserted else { continue }
                snapshot.events.append(event)
            } catch { snapshot.unreadableRecords += 1 }
        }
        let jobs = (try? FileManager.default.contentsOfDirectory(at: root.appendingPathComponent("tasks"), includingPropertiesForKeys: nil)) ?? []
        var titles: [String: String] = [:]
        for folder in jobs where UUID(uuidString: folder.lastPathComponent) != nil {
            if let job = try? AppStorage.read(ProcessingJob.self, from: folder.appendingPathComponent("job.json")) { titles[folder.lastPathComponent] = job.title }
            let path = folder.appendingPathComponent("checkpoints/summary/usage.jsonl")
            guard FileManager.default.fileExists(atPath: path.path) else { continue }
            do {
                let data = try Data(contentsOf: path)
                guard data.count <= 50 * 1024 * 1024, let lines = String(data: data, encoding: .utf8) else {
                    snapshot.unreadableRecords += 1; continue
                }
                for (index, line) in lines.split(separator: "\n").enumerated() {
                    guard let raw = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                          let timestamp = raw["time"] as? Double, timestamp.isFinite else { snapshot.unreadableRecords += 1; continue }
                    let hash = SHA256.hash(data: Data("\(folder.lastPathComponent):\(index):\(line)".utf8)).map { String(format: "%02x", $0) }.joined()
                    let id = raw["eventID"] as? String ?? "legacy-\(hash)"
                    guard seen.insert(id).inserted else { continue }
                    var event = UsageEvent(id: id, timestamp: timestamp, purpose: "summary")
                    event.taskID = folder.lastPathComponent; event.status = "response"
                    event.usage = TokenUsage(response: raw["usage"] as? [String: Any] ?? [:])
                    event.finishReason = raw["finish_reason"] as? String
                    // Old logs had no reliable model identity. Never infer it from current settings or final notes.
                    event.providerID = raw["providerID"] as? String; event.providerName = raw["providerName"] as? String
                    event.modelConfigID = raw["modelConfigID"] as? String; event.modelID = raw["modelID"] as? String
                    event.modelName = raw["modelName"] as? String
                    if let pricing = raw["pricing"], let data = try? JSONSerialization.data(withJSONObject: pricing) {
                        event.pricing = try? JSONDecoder().decode(TokenPricing.self, from: data)
                    }
                    snapshot.events.append(event)
                }
            } catch { snapshot.unreadableRecords += 1 }
        }
        for index in snapshot.events.indices {
            if let taskID = snapshot.events[index].taskID { snapshot.events[index].taskTitle = titles[taskID] ?? snapshot.events[index].taskTitle }
        }
        snapshot.events.sort { $0.timestamp == $1.timestamp ? $0.id < $1.id : $0.timestamp > $1.timestamp }
        return snapshot
    }

    public static func csv(_ events: [UsageEvent], preferences: AppPreferences, currentPrices: Bool) -> String {
        func field(_ value: String) -> String {
            let protected = ["=", "+", "-", "@", "\t", "\r"].contains(where: value.hasPrefix) ? "'" + value : value
            return "\"" + protected.replacingOccurrences(of: "\"", with: "\"\"") + "\""
        }
        let formatter = ISO8601DateFormatter()
        var rows = [["时间", "任务", "服务商", "模型ID", "输入Token", "输出Token", "缓存输入Token", "推理Token（含于输出）", "总Token", "币种", "估算费用", "状态", "计价方式", "总结阶段"]]
        rows += events.map { event in
            let price = event.price(in: preferences, current: currentPrices)
            return [formatter.string(from: Date(timeIntervalSince1970: event.timestamp)), event.displayTask, event.displayProvider,
                    event.modelID ?? "未记录", event.usage.inputTokens.map(String.init) ?? "", event.usage.outputTokens.map(String.init) ?? "",
                    event.usage.cachedInputTokens.map(String.init) ?? "", event.usage.reasoningTokens.map(String.init) ?? "",
                    event.usage.totalTokens.map(String.init) ?? "", price?.currency ?? "",
                    event.cost(in: preferences, current: currentPrices).map { String(format: "%.8f", locale: Locale(identifier: "en_US_POSIX"), $0) } ?? "",
                    event.statusLabel, currentPrices ? "当前价格预估" : "调用时价格快照", event.phaseLabel]
        }
        return "\u{FEFF}" + rows.map { $0.map(field).joined(separator: ",") }.joined(separator: "\r\n") + "\r\n"
    }
}

public struct UsageTotals: Sendable {
    public var calls = 0
    public var input = 0
    public var output = 0
    public var cached = 0
    public var reasoning = 0
    public var knownUsageCalls = 0
    public var unknownUsageCalls = 0
    public var reportedInputCalls = 0
    public var reportedOutputCalls = 0
    public var reportedCacheCalls = 0
    public var reportedReasoningCalls = 0
    public var cacheMeasuredCalls = 0
    public var cacheMeasuredInput = 0
    public var cacheMeasuredHits = 0
    public var cacheMisses: Int { cacheMeasuredInput - cacheMeasuredHits }
    public var cacheHitRate: Double? { cacheMeasuredInput > 0 ? Double(cacheMeasuredHits) / Double(cacheMeasuredInput) : nil }
    public var pricedCalls = 0
    public var unpricedCalls = 0
    public var costs: [String: Double] = [:]
    public init(events: [UsageEvent], preferences: AppPreferences, currentPrices: Bool) {
        calls = events.count
        for event in events {
            if event.usage.inputTokens == nil || event.usage.outputTokens == nil || event.usage.invalid { unknownUsageCalls += 1 }
            else { knownUsageCalls += 1 }
            if !event.usage.invalid {
                if event.usage.inputTokens != nil { reportedInputCalls += 1 }
                if event.usage.outputTokens != nil { reportedOutputCalls += 1 }
                if event.usage.cachedInputTokens != nil { reportedCacheCalls += 1 }
                if event.usage.reasoningTokens != nil { reportedReasoningCalls += 1 }
                if let input = event.usage.inputTokens, let cached = event.usage.cachedInputTokens {
                    cacheMeasuredCalls += 1; cacheMeasuredInput += input; cacheMeasuredHits += cached
                }
                input += event.usage.inputTokens ?? 0; output += event.usage.outputTokens ?? 0
                cached += event.usage.cachedInputTokens ?? 0; reasoning += event.usage.reasoningTokens ?? 0
            }
            if let cost = event.cost(in: preferences, current: currentPrices), let price = event.price(in: preferences, current: currentPrices) {
                pricedCalls += 1; costs[price.currency, default: 0] += cost
            } else { unpricedCalls += 1 }
        }
    }
}
