import Foundation

/// Built once per transcript revision; citation rendering no longer scans the transcript per ID.
public struct TranscriptTimeIndex: Sendable {
    private let starts: [Int: Double]
    public init(_ segments: [TranscriptSegment] = []) {
        var values: [Int: Double] = [:]
        values.reserveCapacity(segments.count)
        for segment in segments where values[segment.id] == nil {
            values[segment.id] = segment.start
        }
        starts = values
    }
    public func start(for id: Int) -> Double? { starts[id] }
    public func range(for ids: [Int]) -> (first: Double, last: Double)? {
        var first = Double.infinity, last = -Double.infinity
        for id in ids {
            if let time = starts[id], time.isFinite { first = min(first, time); last = max(last, time) }
        }
        return first.isFinite ? (first, last) : nil
    }
}
