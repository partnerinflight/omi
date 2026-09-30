import Foundation

/// Delivers spooled work to the receiver, oldest first: CAPTURE_OPEN for captures being
/// recorded, file uploads for complete ones, CAPTURE_CANCEL for skipped ones. Any
/// error backs off (30 s doubling to 15 min); repeated protocol rejections mark a complete
/// capture failed so it cannot block the rest. Call `run` from one background queue.
public final class UploadQueue {
    public static let initialBackoff: Double = 30
    public static let maxBackoff: Double = 900
    public static let maxProtocolFailures = 3

    public private(set) var nextAttempt: Double = 0
    public private(set) var lastError: String?
    private var backoff = UploadQueue.initialBackoff
    private let spool: Spool
    private let transport: UploadTransport

    public init(spool: Spool, transport: UploadTransport) {
        self.spool = spool
        self.transport = transport
    }

    /// One delivery pass. Returns how many records still need delivery.
    @discardableResult
    public func run(now: Double) -> Int {
        guard now >= nextAttempt else { return pending() }
        var passError: String?
        for record in spool.records() where record.state != .failed {
            do {
                try deliver(record)
            } catch let error as UploadError where error.isRetryable {
                lastError = "\(error)"
                nextAttempt = now + backoff
                backoff = min(backoff * 2, Self.maxBackoff)
                return pending()
            } catch {
                // Only complete captures can be failed; an open recording must survive a receiver
                // that is merely too old (or misconfigured) until it is upgraded.
                try? spool.update(record.captureID) {
                    guard $0.state == .complete else { return }
                    $0.uploadFailures += 1
                    if $0.uploadFailures >= Self.maxProtocolFailures { $0.state = .failed }
                }
                passError = "\(error)"
            }
        }
        lastError = passError
        if passError != nil {
            nextAttempt = now + backoff
            backoff = min(backoff * 2, Self.maxBackoff)
        } else {
            backoff = Self.initialBackoff
            nextAttempt = 0
        }
        return pending()
    }

    /// Give captures marked failed another chance (e.g. after the receiver was upgraded).
    /// Only those whose audio is still spooled are revived.
    public func resetFailed() {
        for record in spool.records() where record.state == .failed {
            guard FileManager.default.fileExists(atPath: spool.cafURL(record.captureID).path) else { continue }
            _ = try? spool.update(record.captureID) {
                $0.state = .complete
                $0.uploadFailures = 0
            }
        }
        nextAttempt = 0
        backoff = Self.initialBackoff
    }

    /// Records the receiver still needs something for (an open recording counts once opened).
    public func pending() -> Int {
        spool.records().filter { $0.state != .failed && !($0.state == .recording && $0.opened) }.count
    }

    private func deliver(_ record: CaptureRecord) throws {
        let id = record.captureID
        switch record.state {
        case .recording, .complete:
            var state = record.state
            if !record.opened {
                try transport.openCapture(record)
                // Re-read under the lock: the controller may have changed the record during the network call.
                guard let fresh = try spool.update(id, { $0.opened = true }) else { return }
                state = fresh.state
            }
            if state == .complete {
                let current = spool.load(id) ?? record
                try transport.upload(current, file: spool.cafURL(id))
                spool.delete(id)
            }
        case .cancelled:
            try transport.cancelCapture(id: id)
            spool.delete(id)
        case .failed:
            break
        }
    }
}
