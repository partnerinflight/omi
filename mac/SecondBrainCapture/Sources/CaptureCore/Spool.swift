import Foundation

/// The local spool: `<id>.json` records, `<id>.pcm` while recording, `<id>.caf` after encoding.
/// The directory is private to the user (0700); files are deleted only after the receiver commits them.
public final class Spool {
    public let root: URL
    private let lock = NSLock()

    public init(root: URL) throws {
        self.root = root
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: root.path)
    }

    public func pcmURL(_ id: String) -> URL { root.appendingPathComponent("\(id).pcm") }
    public func cafURL(_ id: String) -> URL { root.appendingPathComponent("\(id).caf") }
    public func recordURL(_ id: String) -> URL { root.appendingPathComponent("\(id).json") }

    public func save(_ record: CaptureRecord) throws {
        lock.lock()
        defer { lock.unlock() }
        try write(record)
    }

    /// The record on disk, or nil if it is missing or unreadable.
    public func load(_ id: String) -> CaptureRecord? {
        lock.lock()
        defer { lock.unlock() }
        return read(id)
    }

    /// Locked read-modify-write of the on-disk record; nil (and nothing written) if the record no longer exists.
    @discardableResult
    public func update(_ id: String, _ mutate: (inout CaptureRecord) -> Void) throws -> CaptureRecord? {
        lock.lock()
        defer { lock.unlock() }
        guard var record = read(id) else { return nil }
        mutate(&record)
        try write(record)
        return record
    }

    private func read(_ id: String) -> CaptureRecord? {
        try? JSONDecoder().decode(CaptureRecord.self, from: Data(contentsOf: recordURL(id)))
    }

    private func write(_ record: CaptureRecord) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let url = recordURL(record.captureID)
        try encoder.encode(record).write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    /// All readable records, oldest first; unreadable files are ignored.
    public func records() -> [CaptureRecord] {
        let files = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []
        return files.filter { $0.pathExtension == "json" }
            .compactMap { try? JSONDecoder().decode(CaptureRecord.self, from: Data(contentsOf: $0)) }
            .sorted { ($0.startMs, $0.captureID) < ($1.startMs, $1.captureID) }
    }

    public func deleteAudio(_ id: String) {
        for url in [pcmURL(id), cafURL(id)] {
            try? FileManager.default.removeItem(at: url)
        }
    }

    public func delete(_ id: String) {
        lock.lock()
        defer { lock.unlock() }
        deleteAudio(id)
        try? FileManager.default.removeItem(at: recordURL(id))
    }

    public func totalBytes() -> Int64 {
        let files = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: [.fileSizeKey])) ?? []
        return files.reduce(0) { $0 + Int64((try? $1.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
    }
}
