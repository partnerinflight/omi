import Foundation

/// One Core Audio process object as seen at a detector tick.
public struct AudioProcess: Equatable {
    public let objectID: UInt32
    public let bundleID: String
    public let isRunningInput: Bool
    public let inputDevices: [UInt32]

    public init(objectID: UInt32, bundleID: String, isRunningInput: Bool, inputDevices: [UInt32] = []) {
        self.objectID = objectID
        self.bundleID = bundleID
        self.isRunningInput = isRunningInput
        self.inputDevices = inputDevices
    }
}

public enum DetectorEvent: Equatable {
    /// Start capturing `app`: tap `processes` (the app and its helpers) and record
    /// the mic `inputDevice` the app is using (nil = system default input).
    case start(app: String, processes: [UInt32], inputDevice: UInt32?)
    case stop
}

/// Decides when a meeting starts and ends from periodic process snapshots.
/// Pure logic: the caller supplies snapshots and the time.
public final class MeetingDetector {
    public private(set) var activeApp: String?
    public private(set) var pausedUntil: Double = 0
    private var releasedAt: Double?
    private var skippedApp: String?
    private let matcher: AppMatcher
    private let grace: Double

    public init(matcher: AppMatcher, releaseGraceSeconds: Double) {
        self.matcher = matcher
        grace = releaseGraceSeconds
    }

    public func update(_ processes: [AudioProcess], now: Double) -> DetectorEvent? {
        let holding = Set(processes.filter(\.isRunningInput).compactMap { matcher.family(of: $0.bundleID) })
        if let skipped = skippedApp, !holding.contains(skipped) {
            skippedApp = nil
        }
        if let app = activeApp {
            if holding.contains(app) {
                releasedAt = nil
                return nil
            }
            let released = releasedAt ?? now
            releasedAt = released
            guard now - released >= grace else { return nil }
            activeApp = nil
            releasedAt = nil
            return .stop
        }
        guard now >= pausedUntil,
              let app = matcher.allowlist.first(where: { holding.contains($0) && $0 != skippedApp }) else { return nil }
        activeApp = app
        let family = processes.filter { matcher.family(of: $0.bundleID) == app }
        let device = family.first(where: \.isRunningInput)?.inputDevices.first
        return .start(app: app, processes: family.map(\.objectID), inputDevice: device)
    }

    /// "Skip this meeting": stop now and ignore the app until it releases the microphone.
    public func skip() -> DetectorEvent? {
        guard let app = activeApp else { return nil }
        // If the app already released the mic (grace period), there is nothing left to ignore.
        skippedApp = releasedAt == nil ? app : nil
        activeApp = nil
        releasedAt = nil
        return .stop
    }

    /// Stop any capture and start none before `until`; `pause(until: 0)` resumes.
    public func pause(until: Double) -> DetectorEvent? {
        pausedUntil = until
        guard activeApp != nil else { return nil }
        activeApp = nil
        releasedAt = nil
        return .stop
    }

    /// End any active capture without changing pause or skip state (e.g. after a sleep/wake gap);
    /// the next update starts a new capture if the app still holds the mic.
    public func endCapture() -> DetectorEvent? {
        guard activeApp != nil else { return nil }
        activeApp = nil
        releasedAt = nil
        return .stop
    }
}
