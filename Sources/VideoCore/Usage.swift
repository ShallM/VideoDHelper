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
    /// Stored on the event so task deletion cannot change the accounting scope.
    public var sourceKind: String?
    /// The requested parameter, not a claim about the provider's execution.
    public var reasoningEffort: String?
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
        reasoningEffort = model?.reasoningEffort
    }
    public var displayModel: String { modelName ?? modelID ?? "历史未记录模型" }
    public var displayProvider: String { providerName ?? "历史未归属" }
    public var displayTask: String { taskTitle ?? (purpose == "connection-test" ? "模型连接测试" : "已移除的任务") }
    public var resolvedSourceKind: String? {
        guard purpose != "connection-test" else { return nil }
        if let sourceKind { return ["bilibili", "recording"].contains(sourceKind) ? sourceKind : nil }
        // Before workspaces existed every task was a video task. Do not infer a
        // source for unassociated calls, or consult today's mutable task list.
        return taskID == nil ? nil : "bilibili"
    }
    public var sourceLabel: String {
        if purpose == "connection-test" { return "连接测试" }
        switch resolvedSourceKind {
        case "bilibili": return "B站视频"
        case "recording": return "录音整理"
        default: return "来源未记录"
        }
    }
    public var requestedReasoningLabel: String {
        guard let reasoningEffort else { return "历史未记录" }
        if reasoningEffort == "default" { return "服务商默认（未传参数）" }
        return ReasoningEffort(rawValue: reasoningEffort)?.label ?? "未知档位"
    }
    public var phaseLabel: String {
        switch phase {
        case "meeting_draft": return "会议纪要草稿"
        case "meeting_merge": return "长纪要分层合并"
        case "meeting_evidence": return "独立关键证据检查"
        case "meeting_review": return "纪要对照核验"
        case "meeting_reread": return "纪要定向回读"
        case "meeting_punctuation": return "原话断句与标点"
        case "dialogue_summary": return "对话全文理解"
        case "dialogue_merge": return "对话分层综合"
        case "dialogue_condense": return "独立整理原话"
        case "dialogue_review": return "对照原文复核"
        case "dialogue_reread": return "对话定向回读"
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

public enum UsageSourceFilter: String, CaseIterable, Identifiable, Sendable {
    case all, bilibili, recording, connectionTests
    public var id: String { rawValue }
    public var label: String {
        switch self {
        case .all: return "全部"
        case .bilibili: return "B站视频"
        case .recording: return "录音整理"
        case .connectionTests: return "连接测试"
        }
    }
    public func includes(_ event: UsageEvent) -> Bool {
        switch self {
        case .all: return true
        case .connectionTests: return event.purpose == "connection-test"
        case .bilibili, .recording: return event.resolvedSourceKind == rawValue
        }
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
        var saved = event
        if saved.sourceKind == nil { saved.sourceKind = saved.resolvedSourceKind }
        try AppStorage.write(saved, to: root.appendingPathComponent("usage/events/\(event.id).json"))
    }
    public static func load(root: URL = AppStorage.root) -> UsageSnapshot {
        var snapshot = UsageSnapshot(), seen = Set<String>()
        var titles: [String: String] = [:]
        let files = (try? FileManager.default.contentsOfDirectory(at: root.appendingPathComponent("usage/events"), includingPropertiesForKeys: nil)) ?? []
        for file in files where file.pathExtension == "json" {
            do {
                let event = try AppStorage.read(UsageEvent.self, from: file)
                guard event.schemaVersion == 1, event.timestamp.isFinite, seen.insert(event.id).inserted else { continue }
                snapshot.events.append(event)
            } catch { snapshot.unreadableRecords += 1 }
        }
        // Retain legacy usage independently of disposable task directories.
        let retained = (try? FileManager.default.contentsOfDirectory(at: root.appendingPathComponent("usage/task-history"), includingPropertiesForKeys: nil)) ?? []
        for file in retained where file.pathExtension == "json" && UUID(uuidString: file.deletingPathExtension().lastPathComponent) != nil {
            do {
                let events = try AppStorage.read([UsageEvent].self, from: file)
                guard events.allSatisfy({ $0.taskID == file.deletingPathExtension().lastPathComponent && $0.schemaVersion == 1 && $0.timestamp.isFinite }) else {
                    snapshot.unreadableRecords += 1; continue
                }
                if let title = events.compactMap(\.taskTitle).first { titles[file.deletingPathExtension().lastPathComponent] = title }
                snapshot.events += events.filter { seen.insert($0.id).inserted }
            } catch { snapshot.unreadableRecords += 1 }
        }
        for workspace in WorkspaceKind.allCases {
            let jobs = (try? FileManager.default.contentsOfDirectory(at: root.appendingPathComponent(workspace.tasksDirectoryName), includingPropertiesForKeys: nil)) ?? []
            for folder in jobs where UUID(uuidString: folder.lastPathComponent) != nil {
                if let job = try? AppStorage.read(ProcessingJob.self, from: folder.appendingPathComponent("job.json")) { titles[folder.lastPathComponent] = job.title }
                let legacy = legacyHistory(in: folder, workspace: workspace)
                snapshot.unreadableRecords += legacy.unreadableRecords
                snapshot.events += legacy.events.filter { seen.insert($0.id).inserted }
            }
        }
        for index in snapshot.events.indices {
            if let taskID = snapshot.events[index].taskID { snapshot.events[index].taskTitle = titles[taskID] ?? snapshot.events[index].taskTitle }
        }
        snapshot.events.sort { $0.timestamp == $1.timestamp ? $0.id < $1.id : $0.timestamp > $1.timestamp }
        return snapshot
    }

    static func preserveTaskHistory(taskID: String, root: URL, workspace: WorkspaceKind = .bilibili) throws {
        let folder = root.appendingPathComponent(workspace.tasksDirectoryName).appendingPathComponent(taskID)
        let legacy = legacyHistory(in: folder, workspace: workspace)
        guard legacy.unreadableRecords == 0 else {
            throw VideoError.toolFailed("任务的旧版用量记录无法完整读取，暂未删除。可先归档任务，保留原始记录。")
        }
        let files = (try? FileManager.default.contentsOfDirectory(at: root.appendingPathComponent("usage/events"), includingPropertiesForKeys: nil)) ?? []
        let modern = files.filter { $0.pathExtension == "json" }.compactMap { try? AppStorage.read(UsageEvent.self, from: $0) }
            .filter { $0.taskID == taskID && $0.schemaVersion == 1 && $0.timestamp.isFinite }
        guard !legacy.events.isEmpty || !modern.isEmpty else { return }
        let path = root.appendingPathComponent("usage/task-history/\(taskID).json")
        var saved = FileManager.default.fileExists(atPath: path.path) ? try AppStorage.read([UsageEvent].self, from: path) : []
        guard saved.allSatisfy({ $0.taskID == taskID && $0.schemaVersion == 1 && $0.timestamp.isFinite }) else {
            throw VideoError.toolFailed("历史用量副本无法读取，暂未删除任务。")
        }
        let job = try AppStorage.read(ProcessingJob.self, from: folder.appendingPathComponent("job.json"))
        var seen = Set(saved.map(\.id))
        for event in modern + legacy.events where seen.insert(event.id).inserted { saved.append(event) }
        for index in saved.indices {
            saved[index].taskTitle = job.title
            if saved[index].sourceKind == nil { saved[index].sourceKind = saved[index].resolvedSourceKind }
        }
        try AppStorage.write(saved, to: path)
    }

    private static func legacyHistory(in folder: URL, workspace: WorkspaceKind) -> UsageSnapshot {
        var snapshot = UsageSnapshot()
        let path = folder.appendingPathComponent("checkpoints/summary/usage.jsonl")
        guard FileManager.default.fileExists(atPath: path.path) else { return snapshot }
        do {
            let data = try Data(contentsOf: path)
            guard data.count <= 50 * 1024 * 1024, let lines = String(data: data, encoding: .utf8) else {
                snapshot.unreadableRecords += 1; return snapshot
            }
            for (index, line) in lines.split(separator: "\n").enumerated() {
                guard let raw = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                      let timestamp = raw["time"] as? Double, timestamp.isFinite else { snapshot.unreadableRecords += 1; continue }
                let hash = SHA256.hash(data: Data("\(folder.lastPathComponent):\(index):\(line)".utf8)).map { String(format: "%02x", $0) }.joined()
                let id = raw["eventID"] as? String ?? "legacy-\(hash)"
                var event = UsageEvent(id: id, timestamp: timestamp, purpose: "summary")
                event.taskID = folder.lastPathComponent; event.status = "response"
                event.usage = TokenUsage(response: raw["usage"] as? [String: Any] ?? [:])
                event.finishReason = raw["finish_reason"] as? String
                event.sourceKind = raw["sourceKind"] as? String ?? workspace.rawValue
                event.reasoningEffort = raw["reasoningEffort"] as? String
                event.phase = raw["phase"] as? String
                // Never infer a legacy request's model or price from current settings.
                event.providerID = raw["providerID"] as? String; event.providerName = raw["providerName"] as? String
                event.modelConfigID = raw["modelConfigID"] as? String; event.modelID = raw["modelID"] as? String
                event.modelName = raw["modelName"] as? String
                if let pricing = raw["pricing"], let data = try? JSONSerialization.data(withJSONObject: pricing) {
                    event.pricing = try? JSONDecoder().decode(TokenPricing.self, from: data)
                }
                snapshot.events.append(event)
            }
        } catch { snapshot.unreadableRecords += 1 }
        return snapshot
    }

    public static func csv(_ events: [UsageEvent], preferences: AppPreferences, currentPrices: Bool) -> String {
        func field(_ value: String) -> String {
            let protected = ["=", "+", "-", "@", "\t", "\r"].contains(where: value.hasPrefix) ? "'" + value : value
            return "\"" + protected.replacingOccurrences(of: "\"", with: "\"\"") + "\""
        }
        let formatter = ISO8601DateFormatter()
        var rows = [["时间", "任务", "服务商", "模型ID", "输入Token", "输出Token", "缓存输入Token", "推理Token（含于输出）", "总Token", "币种", "估算费用", "状态", "计价方式", "总结阶段", "用量来源", "请求思考强度"]]
        rows += events.map { event in
            let price = event.price(in: preferences, current: currentPrices)
            return [formatter.string(from: Date(timeIntervalSince1970: event.timestamp)), event.displayTask, event.displayProvider,
                    event.modelID ?? "未记录", event.usage.inputTokens.map(String.init) ?? "", event.usage.outputTokens.map(String.init) ?? "",
                    event.usage.cachedInputTokens.map(String.init) ?? "", event.usage.reasoningTokens.map(String.init) ?? "",
                    event.usage.totalTokens.map(String.init) ?? "", price?.currency ?? "",
                    event.cost(in: preferences, current: currentPrices).map { String(format: "%.8f", locale: Locale(identifier: "en_US_POSIX"), $0) } ?? "",
                    event.statusLabel, currentPrices ? "当前价格预估" : "调用时价格快照", event.phaseLabel,
                    event.sourceLabel, event.requestedReasoningLabel]
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
