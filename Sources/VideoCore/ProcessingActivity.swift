import Foundation

/// A session-local activity log. Elapsed time is not an estimate of work remaining.
public struct ProcessingActivity: Sendable {
    public enum Outcome: Sendable { case running, cancelling, succeeded, cancelled, failed }
    public struct Step: Identifiable, Sendable {
        public let id: Int
        public let stage: String
        public let startedAt: Date
        public var message: String
    }
    public let startedAt: Date
    public private(set) var endedAt: Date?
    public private(set) var outcome: Outcome = .running
    public private(set) var steps: [Step]
    public private(set) var progress: Double?
    public var current: Step { steps[steps.count - 1] }

    public init(message: String, at date: Date = Date()) {
        startedAt = date
        steps = [Step(id: 0, stage: "prepare", startedAt: date, message: message)]
    }
    public mutating func receive(stage: String, message: String, progress: Double?, at date: Date = Date()) {
        guard outcome == .running else { return }
        // Shared retry/budget messages use "summary"; they do not restart the draft.
        let stage = stage == "summary" && current.stage.hasPrefix("meeting_") ? current.stage : stage
        if stage == current.stage {
            steps[steps.count - 1].message = message
        } else {
            steps.append(Step(id: current.id + 1, stage: stage, startedAt: date, message: message))
            if steps.count > 12 { steps.removeFirst(steps.count - 12) }
        }
        self.progress = progress.flatMap { $0.isFinite ? min(1, max(0, $0)) : nil }
    }
    public mutating func cancel() {
        guard outcome == .running else { return }
        outcome = .cancelling
    }
    public mutating func finish(_ outcome: Outcome, at date: Date = Date()) {
        guard endedAt == nil, outcome != .running, outcome != .cancelling else { return }
        self.outcome = outcome; endedAt = date
    }
    public func elapsed(at date: Date = Date()) -> TimeInterval {
        max(0, (endedAt ?? date).timeIntervalSince(startedAt))
    }
    public static func duration(_ seconds: TimeInterval) -> String {
        let seconds = max(0, Int(seconds.isFinite ? seconds : 0))
        if seconds >= 3600 { return String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60) }
        return String(format: "%02d:%02d", seconds / 60, seconds % 60)
    }
    public static func title(for stage: String, recording: Bool) -> String {
        switch stage {
        case "prepare": return "准备任务"
        case "download": return "获取媒体"
        case "model": return "准备转录模型"
        case "transcribe": return "本地转录"
        case "align": return "对齐文字时间"
        case "diarize": return "区分说话人"
        case "transcribed": return "原稿已就绪"
        case "transcript": return "保存原稿"
        case "summary": return recording ? "理解对话与主题" : "整理台词与章节"
        case "meeting_draft": return "生成纪要草稿"
        case "meeting_evidence": return "独立提取关键证据"
        case "meeting_review": return "核验纪要与依据"
        case "save": return "保存整理结果"
        case "condense": return "整理原话与标点"
        case "review": return "对照原文复核"
        case "plan": return "理解全文结构"
        case "chapter": return "整理主题笔记"
        case "verify": return "检查遗漏与依据"
        case "reread": return "回查原文"
        case "overview": return recording ? "汇总对话总结" : "汇总学习笔记"
        case "cleanup": return "清理媒体"
        case "completed": return "处理完成"
        default: return "正在处理"
        }
    }
}
