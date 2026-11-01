import AVFoundation
import ArgmaxCore
import Darwin
import Foundation
import SpeakerKit

private enum Engine {
    static let revision = "9f99b5cb6a907730543823ad271b82532782a899"
    static let modelRevision = "556fc52a13327837688f02289457cded017802e9"
    static let repository = "argmaxinc/speakerkit-coreml"
    static let model = "pyannote-community-1-coreml"
    static let computePolicy = "segmenter:CPU;embedder:CPU+NeuralEngine;clustering:CPU"

    static var modelBytes: Int {
        if #available(macOS 15, *) { return 11_243_231 }
        return 15_468_390
    }
}

private struct HelperError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

private struct Options {
    var input: URL
    var output: URL
    var cache: URL
    var speakers: Int

    init(_ arguments: [String]) throws {
        var values: [String: String] = [:]
        var index = 0
        while index < arguments.count {
            let key = arguments[index] == "--model-cache" ? "--cache-dir" : arguments[index]
            guard ["--input", "--output", "--cache-dir", "--speakers"].contains(key),
                  index + 1 < arguments.count, values[key] == nil else {
                throw HelperError(message: "参数无效或重复：\(arguments[index])；使用 --help 查看用法。")
            }
            values[key] = arguments[index + 1]
            index += 2
        }
        func path(_ key: String) throws -> URL {
            guard let value = values[key], value.hasPrefix("/") else {
                throw HelperError(message: "\(key) 必须为绝对路径。")
            }
            return URL(fileURLWithPath: value).standardizedFileURL
        }
        input = try path("--input")
        output = try path("--output")
        cache = try path("--cache-dir")
        guard input.resolvingSymlinksInPath() != output.resolvingSymlinksInPath() else {
            throw HelperError(message: "输出文件不能覆盖录音原文件。")
        }
        guard let count = Int(values["--speakers"] ?? "2"), (1...8).contains(count) else {
            throw HelperError(message: "说话人数须为 1～8 的整数。")
        }
        speakers = count
    }
}

// Keep stdout a machine-readable protocol even when Core ML or a dependency logs.
private final class Events: @unchecked Sendable {
    private let output: FileHandle
    private let lock = NSLock()
    private var lastProgress: [String: Double] = [:]
    init() {
        output = FileHandle(fileDescriptor: dup(STDOUT_FILENO), closeOnDealloc: true)
        dup2(STDERR_FILENO, STDOUT_FILENO)
    }

    func emit(_ value: [String: Any]) {
        lock.lock()
        defer { lock.unlock() }
        write(value)
    }

    private func write(_ value: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes]) else { return }
        output.write(data)
        output.write(Data([10]))
    }

    func progress(_ stage: String, _ fraction: Double?, _ message: String) {
        lock.lock()
        defer { lock.unlock() }
        let normalized = fraction.map { min(1, max(0, $0.isFinite ? $0 : 0)) }
        if let normalized, let previous = lastProgress[stage], normalized < 1, normalized < previous + 0.01 { return }
        if let normalized { lastProgress[stage] = normalized }
        write(["kind": "progress", "stage": stage, "fraction": normalized as Any? ?? NSNull(), "message": message])
    }
}

private struct Turn: Codable {
    var start: Double
    var end: Double
    var speaker: String
}

private struct Result: Encodable {
    let version = 1
    let engine = "SpeakerKit"
    let engineRevision = Engine.revision
    let model = Engine.model
    let modelRevision = Engine.modelRevision
    let modelRepository = Engine.repository
    let computePolicy = Engine.computePolicy
    let modelFileBytes = Engine.modelBytes
    let requestedSpeakers: Int
    let detectedSpeakers: Int
    let duration: Double
    let turns: [Turn]
    let timings: [String: Double]
}

@main
private enum RecordingDiarize {
    static func main() async {
        // A cancelled parent terminates the process group. Keep default handlers:
        // the OS releases Core ML memory immediately, without waiting for inference.
        signal(SIGTERM, SIG_DFL)
        signal(SIGINT, SIG_DFL)
        let events = Events()
        let arguments = Array(CommandLine.arguments.dropFirst())
        if arguments == ["--help"] {
            events.emit(["usage": "recording-diarize --input /audio.wav --output /turns.json --cache-dir /model-cache --speakers 2", "input": "16 kHz mono PCM WAV", "version": 1, "modelFileBytes": Engine.modelBytes])
            return
        }
        if arguments == ["--version"] || arguments == ["--info"] {
            events.emit(["version": 1, "engine": "SpeakerKit", "engineRevision": Engine.revision, "modelRevision": Engine.modelRevision, "modelRepository": Engine.repository, "modelFileBytes": Engine.modelBytes, "computePolicy": Engine.computePolicy])
            return
        }
        do {
            try await run(Options(arguments), events: events)
        } catch {
            events.emit(["kind": "error", "message": error.localizedDescription])
            exit(1)
        }
    }

    private static func readAudio(_ url: URL) throws -> [Float] {
        let file = try AVAudioFile(forReading: url, commonFormat: .pcmFormatFloat32, interleaved: false)
        guard file.processingFormat.sampleRate == 16_000, file.processingFormat.channelCount == 1 else {
            throw HelperError(message: "说话人分析需要 16 kHz 单声道 PCM 音频，请先转换录音。")
        }
        guard file.length > 0, file.length <= Int64(UInt32.max),
              let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)) else {
            throw HelperError(message: "音频为空或过长，无法加载。")
        }
        try file.read(into: buffer)
        guard let channel = buffer.floatChannelData?[0], buffer.frameLength > 0 else {
            throw HelperError(message: "录音没有可读取的音频采样。")
        }
        let samples = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
        guard samples.allSatisfy(\.isFinite) else {
            throw HelperError(message: "音频含无效采样，请重新导出录音。")
        }
        return samples
    }

    private static func run(_ options: Options, events: Events) async throws {
        let started = Date()
        events.progress("audio", nil, "读取录音音轨")
        let samples = try readAudio(options.input)
        let duration = Double(samples.count) / 16_000
        try FileManager.default.createDirectory(at: options.cache, withIntermediateDirectories: true)
        let configuration = PyannoteConfig(
            downloadBase: options.cache.path,
            modelRepo: Engine.repository,
            downloadRevision: Engine.modelRevision,
            verbose: false,
            concurrentSegmenterWorkers: 1,
            concurrentEmbedderWorkers: 2
        )
        let diarizer = SpeakerKitDiarizer.pyannote(config: configuration)
        let downloadStarted = Date()
        events.progress("download", nil, String(format: "准备本地说话人模型；首次额外下载 %.2f MB，之后复用缓存", Double(Engine.modelBytes) / 1_000_000))
        try await (diarizer as ModelManager).downloadModels { progress in
            events.progress("download", progress.fractionCompleted, "下载或校验本地说话人模型")
        }
        events.progress("download", 1, "说话人模型已就绪")
        let downloaded = Date()
        events.progress("load", nil, "加载本地模型（CPU 与 Neural Engine 混合计算）")
        try await diarizer.loadModels()
        events.progress("load", 1, "本地模型已加载")
        let loaded = Date()
        events.progress("diarize", 0, "区分说话人")
        let result = try await diarizer.diarize(
            audioArray: samples,
            options: PyannoteDiarizationOptions(numberOfSpeakers: options.speakers, useExclusiveReconciliation: false),
            progressCallback: { progress in
                events.progress("diarize", progress.fractionCompleted, "区分说话人")
            }
        )
        let finished = Date()
        await diarizer.unloadModels()
        var labels: [Int: String] = [:]
        var turns: [Turn] = []
        let ordered = result.segments.sorted {
            if $0.startTime != $1.startTime { return $0.startTime < $1.startTime }
            return ($0.speaker.speakerId ?? -1) < ($1.speaker.speakerId ?? -1)
        }
        for segment in ordered {
            let start = max(0, Double(segment.startTime))
            let end = min(duration, Double(segment.endTime))
            guard start.isFinite, end.isFinite, end > start else { continue }
            let ids = segment.speaker.speakerIds
            if ids.isEmpty {
                turns.append(Turn(start: start, end: end, speaker: "unknown"))
            } else {
                for id in ids.sorted() {
                    if labels[id] == nil { labels[id] = String(UnicodeScalar(65 + labels.count)!) }
                    turns.append(Turn(start: start, end: end, speaker: labels[id]!))
                }
            }
        }
        let output = Result(requestedSpeakers: options.speakers, detectedSpeakers: labels.count, duration: duration, turns: turns,
                            timings: ["downloadSeconds": downloaded.timeIntervalSince(downloadStarted), "loadSeconds": loaded.timeIntervalSince(downloaded),
                                      "diarizationSeconds": finished.timeIntervalSince(loaded), "totalSeconds": Date().timeIntervalSince(started)])
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        let data = try encoder.encode(output)
        try FileManager.default.createDirectory(at: options.output.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: options.output, options: .atomic)
        events.progress("diarize", 1, "说话人分析完成")
        events.emit(["kind": "complete", "output": options.output.path, "turns": turns.count, "speakers": labels.count])
    }
}
