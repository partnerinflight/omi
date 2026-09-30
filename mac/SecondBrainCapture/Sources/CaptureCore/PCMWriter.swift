import Foundation

/// Append-only interleaved stereo Int16 PCM (4 bytes per frame). Data is fsynced at most
/// every `syncInterval` seconds, so a crash loses at most that much audio.
public final class PCMWriter {
    public private(set) var framesWritten: Int64 = 0
    private let handle: FileHandle
    private let syncInterval: Double
    private var lastSync: Double = 0

    public init(url: URL, syncInterval: Double = 5) throws {
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        handle = try FileHandle(forWritingTo: url)
        framesWritten = Int64(try handle.seekToEnd()) / 4
        self.syncInterval = syncInterval
    }

    public func write(_ samples: [Int16], now: Double) throws {
        guard !samples.isEmpty else { return }
        try samples.withUnsafeBufferPointer { try handle.write(contentsOf: Data(buffer: $0)) }
        framesWritten += Int64(samples.count / 2)
        if now - lastSync >= syncInterval {
            try handle.synchronize()
            lastSync = now
        }
    }

    public func close() throws {
        try handle.synchronize()
        try handle.close()
    }

    /// Crash recovery: drop a torn trailing partial frame. Returns whole frames kept.
    @discardableResult
    public static func truncateToWholeFrames(_ url: URL) throws -> Int64 {
        let size = (try FileManager.default.attributesOfItem(atPath: url.path)[.size] as? NSNumber)?.int64Value ?? 0
        let whole = size - size % 4
        if whole != size {
            let handle = try FileHandle(forWritingTo: url)
            try handle.truncate(atOffset: UInt64(whole))
            try handle.close()
        }
        return whole / 4
    }
}
