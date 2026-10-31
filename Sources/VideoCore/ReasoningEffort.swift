import Foundation

/// Values sent to an OpenAI-compatible Chat Completions endpoint. These are
/// requested settings; a successful response does not attest to server behavior.
public enum ReasoningEffort: String, CaseIterable, Identifiable, Sendable {
    case providerDefault = "default"
    case low, medium, high, max

    public var id: String { rawValue }
    public var label: String {
        switch self {
        case .providerDefault: return "服务商默认"
        case .low: return "低"
        case .medium: return "中"
        case .high: return "高"
        case .max: return "最高"
        }
    }
    public var detail: String {
        switch self {
        case .providerDefault: return "不发送强度参数，由服务商决定；不等于关闭思考。"
        case .low: return "较少推理，优先控制耗时；可作为质量对照的基线。"
        case .medium: return "请求中等推理强度，实际支持情况由服务商决定。"
        case .high: return "请求更多推理，适合复杂观点与条件梳理；耗时和用量可能增加。"
        case .max: return "请求最高推理强度，建议用于难例对照；不保证比高档更准确。"
        }
    }
    public var connectionTestBudget: Int {
        switch self {
        case .providerDefault, .low: return 1_024
        case .medium, .high: return 4_096
        case .max: return 8_192
        }
    }
    public static func isDeepSeekV4(_ modelID: String) -> Bool {
        let value = modelID.lowercased()
        return value.contains("deepseek") && (value.contains("v4") || value.hasSuffix("deepseek-flash"))
    }
    public static func options(for modelID: String) -> [Self] {
        isDeepSeekV4(modelID) ? [.providerDefault, .low, .high, .max] : allCases
    }
    public static func validated(_ rawValue: String, modelID: String) throws -> Self {
        guard let value = Self(rawValue: rawValue) else {
            throw VideoError.toolFailed("思考强度无效，请重新选择服务商默认、低、中、高或最高。")
        }
        guard options(for: modelID).contains(value) else {
            throw VideoError.toolFailed("DeepSeek V4 没有独立的中档；请明确选择低、高、最高或服务商默认。")
        }
        return value
    }
    public static func parameter(_ rawValue: String, modelID: String) throws -> String? {
        let value = try validated(rawValue, modelID: modelID)
        return value == .providerDefault ? nil : value.rawValue
    }
}
