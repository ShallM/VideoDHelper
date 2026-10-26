import Foundation
import Darwin

/// Owns only processes launched by this app. Quit waits for checkpoints to flush.
final class ProcessRegistry: @unchecked Sendable {
    static let shared = ProcessRegistry()
    private let lock = NSLock()
    private var entries: [ObjectIdentifier: (Process, Bool)] = [:]
    private var shuttingDown = false

    func launch(_ process: Process, group: Bool) throws {
        lock.lock(); defer { lock.unlock() }
        guard !shuttingDown else { throw CancellationError() }
        try process.run()
        entries[ObjectIdentifier(process)] = (process, group)
    }

    func remove(_ process: Process) {
        lock.lock(); defer { lock.unlock() }
        entries.removeValue(forKey: ObjectIdentifier(process))
    }

    var running: Bool {
        lock.lock(); defer { lock.unlock() }
        return entries.values.contains { $0.0.isRunning }
    }

    private func signalAll(_ signal: Int32) {
        lock.lock(); defer { lock.unlock() }
        shuttingDown = true
        for (process, group) in entries.values where process.isRunning {
            if group { kill(-process.processIdentifier, signal) }
            kill(process.processIdentifier, signal)
        }
    }

    func terminate() async {
        signalAll(SIGTERM)
        for _ in 0..<30 {
            if !running { return }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        signalAll(SIGKILL)
        for _ in 0..<20 {
            if !running { return }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }
}

public enum BackgroundProcesses {
    public static var isRunning: Bool { ProcessRegistry.shared.running }
    public static func terminateForQuit() async { await ProcessRegistry.shared.terminate() }
}
