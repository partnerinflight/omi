import Foundation

/// Sidecar for one capture, stored as `<capture_id>.json` in the spool.
public struct CaptureRecord: Codable, Equatable {
    public enum State: String, Codable {
        case recording   // audio is being written to <id>.pcm
        case complete    // encoded to <id>.caf, waiting for upload
        case cancelled   // skipped or too short; only the CANCEL marker remains to send
        case failed      // rejected by the receiver repeatedly; kept for the user
    }

    public var captureID: String
    public var app: String
    public var startMs: Int64
    public var endMs: Int64
    public var state: State
    /// The receiver acknowledged CAPTURE_OPEN.
    public var opened: Bool
    public var uploadFailures: Int

    enum CodingKeys: String, CodingKey {
        case captureID = "capture_id", app, startMs = "start_ms", endMs = "end_ms", state, opened
        case uploadFailures = "upload_failures"
    }

    public init(captureID: String = CaptureRecord.newID(), app: String, startMs: Int64, endMs: Int64? = nil,
                state: State = .recording, opened: Bool = false, uploadFailures: Int = 0) {
        self.captureID = captureID
        self.app = app
        self.startMs = startMs
        self.endMs = endMs ?? startMs
        self.state = state
        self.opened = opened
        self.uploadFailures = uploadFailures
    }

    public static func newID() -> String {
        (0..<16).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined()
    }

    /// FILE_BEGIN metadata; the receiver requires integer start_ms <= end_ms and app.
    public var uploadMetadata: [String: Any] {
        ["app": app, "start_ms": startMs, "end_ms": endMs, "channels": ["L": "mic", "R": "remote"]]
    }
}
