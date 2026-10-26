import Foundation
import Darwin
import Testing
@testable import VideoCore

struct VideoCoreTests {
    @Test
    func testFormatChoicesContainOnlyActualSafeStreams() throws {
        let fixture = Data(#"{"title":"规格测试","formats":[{"format_id":"30064","ext":"mp4","height":720,"fps":29.993,"vcodec":"avc1.640033","acodec":"none","filesize_approx":20994762},{"format_id":"30280","ext":"m4a","vcodec":"none","acodec":"mp4a.40.2","tbr":74},{"format_id":"evil/best","vcodec":"avc1"},{"format_id":"unknown","vcodec":"none","acodec":"none"}]}"#.utf8)
        let info = try VideoMetadata.decode(fixture, reference: VideoReference.parse("https://b23.tv/Abc123"))
        #expect(info.formats.map(\.id) == ["30064", "30280"])
        #expect(info.formats[0].label.contains("720p · 30 fps · H.264"))
        #expect(info.formats[0].sizeLabel.hasPrefix("约 "))
        #expect(info.formats[1].hasAudio && !info.formats[1].hasVideo)
        #expect(info.formats[1].sizeLabel == "大小待定")
    }

    @Test
    func testTimestampLinksUseCanonicalVideoInsteadOfShortRedirect() throws {
        let fixture = Data(#"{"title":"短链","webpage_url":"https://www.bilibili.com/video/BV1Ywn46EESf/?p=2&tracking=unused"}"#.utf8)
        let info = try VideoMetadata.decode(fixture, reference: VideoReference.parse("https://b23.tv/Abc123"))
        #expect(info.url.host == "b23.tv")
        #expect(info.canonicalURL.absoluteString == "https://www.bilibili.com/video/BV1Ywn46EESf/?p=2")
    }

    @Test
    func testJobWritesArePrivateAndReplaceAtomically() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let path = root.appendingPathComponent("job.json")
        var job = ProcessingJob(title: "科研视频", url: "https://b23.tv/Abc123", source: "本地字幕")
        try AppStorage.write(job, to: path)
        job.state = "completed"
        try AppStorage.write(job, to: path)
        #expect(try AppStorage.read(ProcessingJob.self, from: path).state == "completed")
        #expect((try FileManager.default.attributesOfItem(atPath: path.path)[.posixPermissions] as? Int) == 0o600)
        #expect(try FileManager.default.contentsOfDirectory(atPath: root.path) == ["job.json"])
    }

    @Test
    func testProcessTimeoutAndCancellationReturnPromptly() async throws {
        let task = Task { try await ProcessRunner.run(path: "/bin/sleep", arguments: ["30"], timeoutSeconds: 35) }
        try await Task.sleep(nanoseconds: 150_000_000)
        let start = Date()
        task.cancel()
        do { _ = try await task.value; Issue.record("Cancelled process should throw") }
        catch is CancellationError { }
        #expect(Date().timeIntervalSince(start) < 5)
        await #expect(throws: (any Error).self) {
            _ = try await ProcessRunner.run(path: "/bin/sleep", arguments: ["30"], timeoutSeconds: 0.15)
        }
    }

    @Test
    func testBackgroundProcessKeepsUnicodeOutputIntact() async throws {
        let data = try await ProcessRunner.run(path: "/usr/bin/printf", arguments: ["中文 🧪 attention\n"])
        #expect(String(data: data, encoding: .utf8) == "中文 🧪 attention\n")
    }

    @Test
    func testCancellationAlsoStopsWorkerChildren() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let childFile = folder.appendingPathComponent("child.pid")
        let script = "import os, subprocess, sys, time; os.setpgrp(); p=subprocess.Popen(['/bin/sleep','30']); open(sys.argv[1],'w').write(str(p.pid)); time.sleep(30)"
        let task = Task { try await ProcessRunner.run(path: "/usr/bin/python3", arguments: ["-c", script, childFile.path], processGroup: true) }
        defer { task.cancel() }
        for _ in 0..<100 where !FileManager.default.fileExists(atPath: childFile.path) { try await Task.sleep(nanoseconds: 20_000_000) }
        let pid = try #require(Int32(String(contentsOf: childFile, encoding: .utf8)))
        defer { if kill(pid, 0) == 0 { kill(pid, SIGKILL) } }
        task.cancel()
        do { _ = try await task.value; Issue.record("Cancellation should throw") }
        catch is CancellationError { }
        for _ in 0..<100 where kill(pid, 0) == 0 { try await Task.sleep(nanoseconds: 20_000_000) }
        #expect(kill(pid, 0) != 0)
    }

    @Test
    func testInlineBilibiliSubtitleCanBeLoadedWithoutAURL() async throws {
        let fixture = Data(#"{"title":"字幕测试","subtitles":{"danmaku":[{"ext":"xml","url":"https://comment.bilibili.com/1.xml"}],"ai-zh":[{"ext":"srt","data":"1\n00:00:00,000 --> 00:00:02,000\n学习率 3e-4\n"}]}}"#.utf8)
        let reference = try VideoReference.parse("https://b23.tv/Abc123")
        let metadata = try VideoMetadata.decode(fixture, reference: reference)
        #expect(metadata.tracks.count == 1)
        let track = try #require(metadata.tracks.first)
        #expect(track.url == nil)
        let segments = try await BilibiliProbe.load(track, referer: reference.url)
        #expect(segments.map(\.text) == ["学习率 3e-4"])
    }

    @Test
    func testLoginCookieExportOnlyIncludesBilibili() throws {
        let permitted = try #require(HTTPCookie(properties: [.domain: ".bilibili.com", .path: "/", .name: "SESSDATA", .value: "test-session", .secure: "TRUE"]))
        let unrelated = try #require(HTTPCookie(properties: [.domain: ".example.com", .path: "/", .name: "secret", .value: "unrelated-session"]))
        let impersonator = try #require(HTTPCookie(properties: [.domain: ".bilibili.com.example.com", .path: "/", .name: "secret", .value: "wrong-domain"]))
        let text = BilibiliCookies.serialize([permitted, unrelated, impersonator])
        #expect(text.contains("\tSESSDATA\ttest-session"))
        #expect(!text.contains("unrelated-session"))
        #expect(!text.contains("wrong-domain"))
    }

    @Test
    func testSessionSaveUsesPrivatePermissionsAndCanReplace() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("cookies.txt")
        let cookie = try #require(HTTPCookie(properties: [.domain: ".bilibili.com", .path: "/", .name: "SESSDATA", .value: "local-fixture"]))
        try BilibiliCookies.save([cookie], to: file)
        #expect((try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? Int) == 0o600)
        #expect((try FileManager.default.attributesOfItem(atPath: folder.path)[.posixPermissions] as? Int) == 0o700)
        try BilibiliCookies.save([], to: file)
        #expect(!(try String(contentsOf: file, encoding: .utf8)).contains("local-fixture"))
    }

    @Test
    func testMacOSBilibiliShareTextExtractsShortLink() throws {
        let share = "【Codex/Work 继续重置！Kimi K3.1 官方预告，今天要来？-哔哩哔哩】 https://b23.tv/sHmmsMP\u{00A0}"
        #expect(try VideoReference.parse(share).url.absoluteString == "https://b23.tv/sHmmsMP")
        #expect(throws: (any Error).self) { try VideoReference.parse("两个链接 https://b23.tv/Abc123 https://b23.tv/Def456") }
        #expect(throws: (any Error).self) { try VideoReference.parse("分享 https://example.com/private") }
    }

    @Test
    func testVideoURLPreservesPartButDropsTracking() throws {
        let result = try VideoReference.parse("  https://m.bilibili.com/video/BV1xx411c7mD/?p=3&share_source=test#reply  ")
        #expect(result.url.absoluteString == "https://www.bilibili.com/video/BV1xx411c7mD/?p=3")
    }

    @Test
    func testShortURLNormalizesToHTTPS() throws {
        #expect(try VideoReference.parse("http://b23.tv/Abc123?share=1").url.absoluteString == "https://b23.tv/Abc123")
    }

    @Test
    func testRejectsOtherHostsCredentialsAndBadParts() {
        for value in [
            "https://bilibili.com.evil.test/video/BV1xx411c7mD/",
            "https://user:secret@www.bilibili.com/video/BV1xx411c7mD/",
            "file:///etc/passwd", "https://www.bilibili.com:9000/video/BV1xx411c7mD/",
            "https://www.bilibili.com/video/BV1xx411c7mD/?p=0",
            "https://space.bilibili.com/123", "https://b23.tv/"
        ] { #expect(throws: (any Error).self) { try VideoReference.parse(value) } }
    }

    @Test
    func testBilibiliJSONPreservesChineseAndTechnicalTerms() throws {
        let data = Data(#"{"body":[{"from":12.5,"to":18,"content":"Transformer 的 attention，学习率 3e-4。"},{"from":1,"to":2.25,"content":"开场"}]}"#.utf8)
        let segments = try TranscriptParser.parse(data, format: .bilibiliJSON)
        #expect(segments.map(\.id) == [0, 1])
        #expect(segments.map(\.start) == [1, 12.5])
        #expect(segments.last?.text == "Transformer 的 attention，学习率 3e-4。")
    }

    @Test
    func testRejectsEmptyAndInvalidJSONSubtitles() {
        for text in [#"{"body":[]}"#, #"{"body":[{"from":-1,"to":2,"content":"a"}]}"#,
                     #"{"body":[{"from":3,"to":2,"content":"a"}]}"#, #"{"text":"no timing"}"#] {
            #expect(throws: (any Error).self) { try TranscriptParser.parse(Data(text.utf8), format: .bilibiliJSON) }
        }
    }

    @Test
    func testSRTHandlesBOMWindowsNewlinesAndMultilineCode() throws {
        let text = "\u{FEFF}1\r\n00:00:01,250 --> 00:00:05,000\r\n如果 x < y，\r\n返回 True。\r\n\r\n2\r\n00:00:06,000 --> 00:00:07,500\r\n<b>结束</b>\r\n"
        let segments = try TranscriptParser.parse(Data(text.utf8), format: .srt)
        #expect(segments.count == 2)
        #expect(segments[0].start == 1.25)
        #expect(segments[0].text == "如果 x < y，\n返回 True。")
        #expect(segments[1].text == "结束")
    }

    @Test
    func testVTTHandlesNotesCueIDsAndSettings() throws {
        let text = """
        WEBVTT

        NOTE This is commentary, not a cue.
        ignore this

        cue-1
        00:01.200 --> 00:03.400 align:start position:0%
        <v Alice><c.highlight>学习</c> &amp; research

        cue-2
        00:04.000 --> 00:05.000
        a &lt; b
        """
        let segments = try TranscriptParser.parse(Data(text.utf8), format: .vtt)
        #expect(segments.count == 2)
        #expect(segments[0].text == "学习 & research")
        #expect(segments[1].text == "a < b")
        #expect(abs(segments[0].end - 3.4) < 0.001)
    }

    @Test
    func testRejectsMalformedCueInsteadOfSilentlyDroppingIt() {
        let text = "1\n00:00:01,000 --> 00:00:02,000\n正常\n\n2\n没有时间戳的内容"
        #expect(throws: (any Error).self) { try TranscriptParser.parse(Data(text.utf8), format: .srt) }
        #expect(throws: (any Error).self) { try TranscriptParser.parse(Data("00:78.000 --> 00:79.000\n错误".utf8), format: .vtt) }
    }

    @Test
    func testSubtitleRoundTripRetainsTextAndMilliseconds() throws {
        let original = [
            TranscriptSegment(id: 0, start: 0.123, end: 1.999, text: "学习率 1e-5\n不要更改条件"),
            TranscriptSegment(id: 1, start: 3600, end: 3605.678, text: "还有 A < B")
        ]
        let decoded = try TranscriptParser.parse(Data(TranscriptExport.srt(original).utf8), format: .srt)
        #expect(decoded == original)
    }

    @Test
    func testTimestampDoesNotCrashOnNonfiniteOrHugeValues() {
        #expect(TranscriptExport.timestamp(.infinity) == "--:--:--")
        #expect(TranscriptExport.timestamp(.nan) == "--:--:--")
        #expect(TranscriptExport.timestamp(1e100) == "--:--:--")
        #expect(TranscriptExport.timestamp(59.9996, milliseconds: true) == "00:01:00,000")
    }

    @Test
    func testLongChineseAndEmojiChunkingIsLosslessAndBounded() throws {
        let sentence = "注意学习率 3e-4，保留这个条件。👨‍👩‍👧‍👦 编程使用 e\u{301}。\n"
        let text = String(repeating: sentence, count: 80)
        let original = TranscriptSegment(id: 42, start: 19.5, end: 280, text: text)
        let chunks = try TranscriptChunker.chunks([original], maxUTF8Bytes: 120)
        #expect(chunks.count > 1)
        #expect(chunks.allSatisfy { $0.utf8Bytes <= 120 && !$0.fragments.isEmpty })
        let fragments = chunks.flatMap(\.fragments)
        #expect(fragments.map(\.text).joined() == text)
        #expect(fragments.allSatisfy { $0.segmentID == 42 && $0.start == 19.5 && $0.end == 280 })
    }

    @Test
    func testNinetyMinuteTranscriptKeepsEverySourceSegment() throws {
        let original = (0..<1080).map { index in
            TranscriptSegment(id: index, start: Double(index * 5), end: Double(index * 5 + 5),
                              text: "第 \(index) 段：batch size 为 32，验证集指标不等于测试集指标。")
        }
        let chunks = try TranscriptChunker.chunks(original)
        let fragments = chunks.flatMap(\.fragments)
        #expect(fragments.map(\.segmentID) == original.map(\.id))
        #expect(fragments.map(\.text) == original.map(\.text))
        #expect(chunks.map(\.id) == Array(0..<chunks.count))
        #expect(chunks.allSatisfy { $0.utf8Bytes <= 6000 })
    }

    @Test
    func testChunkingDoesNotSplitUnicodeCharacters() throws {
        #expect(throws: (any Error).self) { try TranscriptChunker.chunks([.init(id: 0, start: 0, end: 1, text: "👨‍👩‍👧‍👦")], maxUTF8Bytes: 4) }
        #expect(throws: (any Error).self) { try TranscriptChunker.chunks([], maxUTF8Bytes: 0) }
        #expect(try TranscriptChunker.chunks([]).isEmpty)
    }

    @Test
    func testMetadataPrefersChineseAndFiltersNonSubtitleHosts() throws {
        let fixture = Data(#"{"title":"测试视频","uploader":"作者","duration":90,"subtitles":{"en":[{"ext":"srt","url":"https://aisubtitle.hdslb.com/en.srt"}],"zh-Hans":[{"ext":"json","url":"https://aisubtitle.hdslb.com/zh.json"},{"ext":"json","url":"http://127.0.0.1/private"},{"ext":"json","url":"https://hdslb.com.evil.test/data"}]},"automatic_captions":{"zh":[{"ext":"vtt","url":"https://aisubtitle.hdslb.com/auto.vtt"}]}}"#.utf8)
        let reference = try VideoReference.parse("https://www.bilibili.com/video/BV1xx411c7mD")
        let metadata = try VideoMetadata.decode(fixture, reference: reference)
        #expect(metadata.title == "测试视频")
        #expect(metadata.tracks.count == 3)
        #expect(metadata.tracks[0].language == "zh-Hans")
        #expect(!metadata.tracks[0].isAutomatic)
        #expect(metadata.tracks[1].isAutomatic)
    }

    @Test
    func testPlaylistMustBeExplicitlySelected() throws {
        let reference = try VideoReference.parse("https://b23.tv/Abc123")
        #expect(throws: (any Error).self) { try VideoMetadata.decode(Data(#"{"_type":"playlist","title":"多P视频"}"#.utf8), reference: reference) }
    }
}
