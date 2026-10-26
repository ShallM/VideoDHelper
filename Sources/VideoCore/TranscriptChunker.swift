import Foundation

public struct TranscriptFragment: Equatable, Sendable {
    public let segmentID: Int
    public let start: Double
    public let end: Double
    public let text: String
}

public struct TranscriptChunk: Identifiable, Equatable, Sendable {
    public let id: Int
    public let fragments: [TranscriptFragment]
    public var utf8Bytes: Int { fragments.reduce(0) { $0 + $1.text.utf8.count } }
}

/// Lossless text partitioning for the prototype. This byte budget is NOT a model
/// context limit: the future API adapter must budget the complete serialized
/// request with its tokenizer, instructions and reserved output space.
public enum TranscriptChunker {
    public static func chunks(_ segments: [TranscriptSegment], maxUTF8Bytes: Int = 6000) throws -> [TranscriptChunk] {
        guard maxUTF8Bytes > 0 else { throw VideoError.invalidBudget }
        var chunks: [TranscriptChunk] = []
        var current: [TranscriptFragment] = []
        var size = 0
        for segment in segments {
            for piece in try split(segment.text, budget: maxUTF8Bytes) {
                let bytes = piece.utf8.count
                if size + bytes > maxUTF8Bytes && !current.isEmpty {
                    chunks.append(TranscriptChunk(id: chunks.count, fragments: current))
                    current = []; size = 0
                }
                current.append(TranscriptFragment(segmentID: segment.id, start: segment.start, end: segment.end, text: piece))
                size += bytes
            }
        }
        if !current.isEmpty { chunks.append(TranscriptChunk(id: chunks.count, fragments: current)) }
        return chunks
    }

    private static func split(_ text: String, budget: Int) throws -> [String] {
        if text.isEmpty { return [] }
        if text.utf8.count <= budget { return [text] }
        var result: [String] = []
        var remaining = text[...]
        let boundaries = Set<Character>("。！？；.!?;\n")
        while !remaining.isEmpty {
            var size = 0
            var end = remaining.startIndex
            var boundary: String.Index?
            while end < remaining.endIndex {
                let character = remaining[end]
                let bytes = String(character).utf8.count
                guard bytes <= budget else { throw VideoError.invalidBudget }
                if size + bytes > budget { break }
                size += bytes
                end = remaining.index(after: end)
                if boundaries.contains(character) { boundary = end }
            }
            let cut = end == remaining.endIndex ? end : boundary ?? end
            result.append(String(remaining[..<cut]))
            remaining = remaining[cut...]
        }
        return result
    }
}
