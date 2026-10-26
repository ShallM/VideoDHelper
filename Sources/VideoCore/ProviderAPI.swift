import Foundation

private final class ProviderRedirectBlocker: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

public enum ProviderAPI {
    public static func models(provider: ModelProvider) async throws -> [String] {
        let request = try makeRequest(provider, path: "models")
        let (data, response) = try await perform(request)
        try checkStatus(response.statusCode)
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any], let rows = root["data"] as? [[String: Any]] else {
            throw VideoError.toolFailed("接口没有返回兼容的模型列表，可手动添加模型 ID。")
        }
        return Array(Set(rows.compactMap { $0["id"] as? String }.filter { !$0.isEmpty && $0.count <= 200 })).sorted()
    }

    public static func test(provider: ModelProvider, model: LanguageModel, root: URL = AppStorage.root) async throws -> String {
        var request = try makeRequest(provider, path: "chat/completions")
        request.httpMethod = "POST"
        var body: [String: Any] = ["model": model.modelID, "messages": [["role": "user", "content": "Reply with exactly OK."]], "stream": false, "max_tokens": 512]
        if model.reasoningEffort == "low" { body["reasoning_effort"] = "low" }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        var event = UsageEvent(provider: provider, model: model)
        try UsageRepository.save(event, root: root)
        do {
            let (data, response) = try await perform(request)
            event.httpStatus = response.statusCode; event.durationSeconds = Date().timeIntervalSince1970 - event.timestamp
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            event.usage = TokenUsage(response: object["usage"] as? [String: Any] ?? [:])
            let choice = (object["choices"] as? [[String: Any]])?.first
            let reason = choice?["finish_reason"] as? String
            event.finishReason = ["stop", "length", "content_filter", "tool_calls", "function_call"].contains(reason ?? "") ? reason : nil
            event.status = (200..<300).contains(response.statusCode) ? (choice == nil ? "invalid_response" : "response") : "http_error"
            try UsageRepository.save(event, root: root)
            try checkStatus(response.statusCode)
            guard choice != nil else { throw VideoError.toolFailed("连接已返回，但没有有效的模型响应；用量已记录。") }
            if reason == "length" { return "模型可访问 · 测试输出达到上限，用量已记录。" }
            guard reason == "stop", let message = choice?["message"] as? [String: Any], let content = message["content"] as? String, !content.isEmpty else {
                throw VideoError.toolFailed("模型未正常完成测试，请检查模型兼容性；用量已记录。")
            }
            return String(format: "连接正常 · %.1f 秒 · %@", event.durationSeconds ?? 0,
                          event.usage.totalTokens.map { "\($0) Tokens" } ?? "服务商未返回用量")
        } catch {
            if event.status == "in_flight" {
                event.status = Task.isCancelled ? "cancelled" : "network_error"
                event.durationSeconds = Date().timeIntervalSince1970 - event.timestamp
                try? UsageRepository.save(event, root: root)
            }
            if error is CancellationError || Task.isCancelled { throw CancellationError() }
            if let error = error as? VideoError { throw error }
            throw VideoError.toolFailed("连接超时或响应无效，请检查 API 地址、网络与模型配置。")
        }
    }

    private static func makeRequest(_ provider: ModelProvider, path: String) throws -> URLRequest {
        let url = try APIEndpoint.url(provider.baseURL, path: path)
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 75)
        let key = provider.apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        // Local compatible servers may intentionally require no key.
        if !key.isEmpty { request.setValue("Bearer " + key, forHTTPHeaderField: "Authorization") }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }
    private static func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 75; config.timeoutIntervalForResource = 90
        let session = URLSession(configuration: config, delegate: ProviderRedirectBlocker(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let (bytes, response) = try await session.bytes(for: request)
        guard let response = response as? HTTPURLResponse else { throw VideoError.toolFailed("服务商返回了无效响应。") }
        var data = Data()
        for try await byte in bytes {
            try Task.checkCancellation()
            guard data.count < 2 * 1024 * 1024 else { throw VideoError.toolFailed("服务商响应超过 2 MB，请检查 API 地址。") }
            data.append(byte)
        }
        return (data, response)
    }
    private static func checkStatus(_ code: Int) throws {
        guard (200..<300).contains(code) else {
            let labels = [301: "地址发生重定向，请核对 API 根地址", 302: "地址发生重定向，请核对 API 根地址",
                          307: "地址发生重定向，请核对 API 根地址", 308: "地址发生重定向，请核对 API 根地址",
                          400: "请求参数不兼容，可改为服务商默认推理强度", 401: "API Key 无效或已过期",
                          403: "没有模型权限或额度不足", 404: "API 地址或模型 ID 不存在", 429: "服务商限流，请稍后重试"]
            throw VideoError.toolFailed("\(labels[code] ?? "模型服务暂时不可用")（HTTP \(code)）。")
        }
    }
}
