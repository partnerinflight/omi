import AVFoundation
import CaptureCore
import Foundation
import UserNotifications

/// Owns the detector loop, the active session, crash recovery, encoding and uploads.
/// All state is touched on the main thread; encoding and uploads run on `work`.
final class CaptureController {
    private(set) var config: Config
    let spool: Spool
    private let detector: MeetingDetector
    private var session: CaptureSession?
    private let work = DispatchQueue(label: "capture.work")
    private var uploads: UploadQueue?
    private var timers: [Timer] = []
    private var lastTick = Date().timeIntervalSince1970

    private(set) var captureError: String?
    private(set) var uploadError: String?
    private(set) var pendingUploads = 0
    var micAuthorized = false
    var onChange: (() -> Void)?

    var recordingApp: String? { session?.record.app }
    var isPaused: Bool { detector.pausedUntil > now }
    var spoolOverLimit: Bool { spool.totalBytes() > config.spoolWarnBytes }
    var failedCaptures: Int { spool.records().filter { $0.state == .failed }.count }
    private var now: Double { Date().timeIntervalSince1970 }

    init() throws {
        config = try Config.load(from: Config.directory.appendingPathComponent("config.json"))
        spool = try Spool(root: Config.directory.appendingPathComponent("spool", isDirectory: true))
        detector = MeetingDetector(matcher: AppMatcher(allowlist: config.allowlist),
                                   releaseGraceSeconds: config.releaseGraceSeconds)
        guard config.isConfigured else {
            uploadError = "Set \"host\" in \(Config.directory.path)/config.json"
            return
        }
        do {
            let secret = try ClientIdentity.loadSecret(path: config.expandedSecretPath)
            let clientID = ClientIdentity.clientID(uuid: ClientIdentity.platformUUID() ?? Host.current().name ?? "mac")
            uploads = UploadQueue(spool: spool, transport: UploadClient(host: config.host, port: config.port,
                                                                        secret: secret, clientID: clientID))
        } catch {
            uploadError = "\(error)"
        }
    }

    func start() {
        recoverSpool()
        timers = [
            Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.tick() },
            Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in self?.kickUploads() },
        ]
        kickUploads()
    }

    func skipMeeting() {
        if detector.skip() != nil { end(discard: true) }
        onChange?()
    }

    func pauseForAnHour() {
        if detector.pause(until: now + 3600) != nil { end(discard: false) }
        onChange?()
    }

    func resume() {
        _ = detector.pause(until: 0)
        onChange?()
    }

    private func tick() {
        // The Mac slept (timers did not fire): end the capture so its wall-clock span stays
        // true; the next tick starts a new capture if the call goes on.
        if now - lastTick > 10, session != nil {
            _ = detector.endCapture()
            end(discard: false)
            onChange?()
        }
        lastTick = now
        guard micAuthorized, let event = detector.update(AudioProcesses.snapshot(), now: now) else { return }
        switch event {
        case let .start(app, processes, device):
            begin(app: app, processes: processes, device: device)
        case .stop:
            end(discard: false)
        }
        onChange?()
    }

    private func begin(app: String, processes: [UInt32], device: UInt32?) {
        let record = CaptureRecord(app: app, startMs: Int64(now * 1000))
        do {
            try spool.save(record)
            let capture = try CaptureSession(record: record, spool: spool)
            try capture.start(processes: processes, inputDevice: device)
            session = capture
            captureError = nil
        } catch {
            spool.delete(record.captureID)
            _ = detector.skip()
            captureError = "Could not record \(app): \(error)"
            notify(captureError!)
        }
        kickUploads()
    }

    private func end(discard: Bool) {
        guard let capture = session else { return }
        session = nil
        var record = capture.record
        let frames: Int64
        do {
            frames = try capture.finish()
        } catch {
            // Keep whatever reached the disk rather than discarding the meeting.
            frames = (try? PCMWriter.truncateToWholeFrames(spool.pcmURL(record.captureID))) ?? 0
            captureError = "Recording failed: \(error)"
            notify(captureError!)
        }
        record.endMs = Int64(now * 1000)
        let endMs = record.endMs
        if discard || Double(frames) / 16000 < config.minCaptureSeconds {
            cancel(record.captureID, endMs: endMs)
        } else {
            // Locked read-modify-write: the upload queue may be saving `opened` concurrently.
            _ = try? spool.update(record.captureID) { $0.endMs = endMs }
            work.async { self.finalize(record.captureID) }
        }
        kickUploads()
    }

    private func cancel(_ id: String, endMs: Int64) {
        spool.deleteAudio(id)
        _ = try? spool.update(id) {
            $0.state = .cancelled
            $0.endMs = endMs
        }
    }

    /// On `work`: encode the PCM, then mark the capture complete and try to upload.
    private func finalize(_ id: String) {
        do {
            try CaptureEncoder.encode(pcm: spool.pcmURL(id), to: spool.cafURL(id))
            try? FileManager.default.removeItem(at: spool.pcmURL(id))
            try spool.update(id) { $0.state = .complete }
        } catch {
            DispatchQueue.main.async {
                self.captureError = "Encoding failed: \(error)"
                self.onChange?()
            }
        }
        runUploads()
    }

    /// Finish captures interrupted by a crash, sleep or quit.
    private func recoverSpool() {
        for record in spool.records() where record.state == .recording {  // before uploads start: no race
            let pcm = spool.pcmURL(record.captureID)
            if !FileManager.default.fileExists(atPath: pcm.path),
               FileManager.default.fileExists(atPath: spool.cafURL(record.captureID).path) {
                _ = try? spool.update(record.captureID) { $0.state = .complete }  // encoded, but the state change was lost
                continue
            }
            let frames = (try? PCMWriter.truncateToWholeFrames(pcm)) ?? 0
            let endMs = record.startMs + frames * 1000 / 16000
            if Double(frames) / 16000 < config.minCaptureSeconds {
                cancel(record.captureID, endMs: endMs)
            } else {
                _ = try? spool.update(record.captureID) { $0.endMs = endMs }
                work.async { self.finalize(record.captureID) }
            }
        }
    }

    private func kickUploads() {
        work.async { self.runUploads() }
    }

    /// On `work`.
    private func runUploads() {
        guard let uploads else { return }
        let pending = uploads.run(now: Date().timeIntervalSince1970)
        let error = uploads.lastError
        DispatchQueue.main.async {
            self.pendingUploads = pending
            self.uploadError = error
            self.onChange?()
        }
    }

    private func notify(_ message: String) {
        let content = UNMutableNotificationContent()
        content.title = "SecondBrainCapture"
        content.body = message
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content,
                                                                     trigger: nil))
    }
}
