import AVFoundation
import Combine
import Foundation
import VideoCore

@MainActor
enum WorkspaceProcessing { static var owner: WorkspaceKind? }

@MainActor
final class AudioPlayback: ObservableObject {
    @Published private(set) var position: Double = 0
    @Published private(set) var duration: Double = 0
    @Published private(set) var isPlaying = false
    @Published var speed: Float = 1 { didSet { if isPlaying { player.rate = speed } } }
    private let player = AVPlayer()
    private var observer: Any?
    private var ended: NSObjectProtocol?
    private var url: URL?

    init() {
        observer = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.25, preferredTimescale: 600), queue: .main) { [weak self] time in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if time.seconds.isFinite { position = max(0, time.seconds) }
                if let value = player.currentItem?.duration.seconds, value.isFinite { duration = max(0, value) }
                isPlaying = player.rate != 0
            }
        }
        ended = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: nil, queue: .main) { [weak self] note in
            Task { @MainActor [weak self] in
                guard let self, let item = note.object as? AVPlayerItem, item === player.currentItem else { return }
                isPlaying = false
            }
        }
    }
    deinit {
        if let observer { player.removeTimeObserver(observer) }
        if let ended { NotificationCenter.default.removeObserver(ended) }
    }
    var isLoaded: Bool { url != nil }
    func load(_ url: URL, duration: Double = 0) {
        guard self.url != url else { return }
        stop()
        self.url = url; self.duration = duration
        player.replaceCurrentItem(with: AVPlayerItem(url: url))
    }
    func play() {
        guard isLoaded else { return }
        if duration > 0 && position >= duration - 0.1 { seek(to: 0) }
        player.playImmediately(atRate: speed); isPlaying = true
    }
    func pause() { player.pause(); isPlaying = false }
    func toggle() { isPlaying ? pause() : play() }
    func seek(to seconds: Double) {
        guard seconds.isFinite else { return }
        position = min(max(0, seconds), duration > 0 ? duration : seconds)
        player.seek(to: CMTime(seconds: position, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }
    func stop() {
        pause(); player.replaceCurrentItem(with: nil); url = nil; position = 0; duration = 0
    }
}
