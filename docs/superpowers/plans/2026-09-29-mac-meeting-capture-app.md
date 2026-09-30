# Mac Meeting-Capture App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A macOS menu-bar app that records allowlisted meeting apps (remote audio via a Core Audio process tap + the owner's mic) as stereo Opus files and uploads them to the Second Brain receiver over protocol v2.

**Architecture:** A Swift package with two targets. `CaptureCore` is a library of testable logic: config, meeting detection state machine, stereo assembly, resampling, crash-safe PCM spool, Opus encoding, the v2 wire protocol, the upload client and the retrying upload queue. `SecondBrainCapture` is the executable: Core Audio process enumeration and taps, AVAudioEngine mic capture, the capture controller and the menu bar. Audio is spooled as raw 16 kHz stereo PCM (fsync every 5 s, so a crash loses at most 5 s), encoded to Opus CAF when the meeting ends, and uploaded with short per-operation connections.

**Tech Stack:** Swift 6.3 toolchain (Swift 5 language mode), SwiftPM, XCTest, Core Audio process taps (macOS 14.4+), AVFoundation, CryptoKit, AppKit, ServiceManagement. The receiver side (`omi_local` protocol v2) already exists on `feature/wifi-local-upload`.

Spec: `docs/superpowers/specs/2026-09-29-meeting-capture-design.md` §1 and the client side of §2. This is plan 3 of 3; plan 1 (receiver v2) is merged. Plan 2 (pipeline dedupe) is independent of this one.

---

## Conventions

- Package root: `mac/SecondBrainCapture` (called **PKG**). All `swift` commands run from PKG.
- Unit tests: `cd mac/SecondBrainCapture && swift test` (XCTest; Xcode is installed at `/Applications/Xcode.app`).
- The upload integration test needs a Python that can import `omi_local`:
  `SBC_RECEIVER_PYTHON=$PWD/../../omi/firmware/scripts/omi-local/.venv/bin/python swift test`
  Without the variable it is skipped.
- Protocol facts (from `omi/firmware/scripts/omi-local/omi_local/upload_protocol.py`): frames are `[type:u8][len:u32 BE][payload]`; v2 HELLO is `"OMIL" 02 client_id:6 nonce:16`; tags are HMAC-SHA256(secret, label ‖ client_nonce ‖ server_nonce) with labels `omi-local-srv` / `omi-local-cli`; FILE_BEGIN metadata must contain `start_ms <= end_ms` (ints) and `app`; FILE_START offset == length means already committed (still send FILE_END); any REJECT closes the connection; REJECT codes 1 auth, 2 protocol, 3 busy; idle connections close after 60 s.
- Commit messages start with `mac:`.

## File structure

| File | Responsibility |
|---|---|
| `PKG/Package.swift` | Targets: `CaptureCore`, `SecondBrainCapture`, `CaptureCoreTests` |
| `PKG/Sources/CaptureCore/Config.swift` | `config.json` load/save with defaults |
| `PKG/Sources/CaptureCore/AppMatcher.swift` | Bundle ID → allowlisted app family |
| `PKG/Sources/CaptureCore/MeetingDetector.swift` | Start/stop/skip/pause state machine |
| `PKG/Sources/CaptureCore/StereoAssembler.swift` | Align mic (L) and remote (R) sample streams → interleaved Int16 |
| `PKG/Sources/CaptureCore/Resampler.swift` | Any format → 16 kHz mono Float32 |
| `PKG/Sources/CaptureCore/CaptureRecord.swift` | Per-capture sidecar model |
| `PKG/Sources/CaptureCore/Spool.swift` | Spool directory: records, file paths, cleanup |
| `PKG/Sources/CaptureCore/PCMWriter.swift` | Append-only PCM with periodic fsync; crash truncation |
| `PKG/Sources/CaptureCore/CaptureEncoder.swift` | PCM → stereo Opus CAF |
| `PKG/Sources/CaptureCore/Wire.swift` | Protocol v2 codec + hex helpers |
| `PKG/Sources/CaptureCore/ClientIdentity.swift` | Client id from hardware UUID; secret loading |
| `PKG/Sources/CaptureCore/UploadClient.swift` | Socket connection, handshake, open/cancel/upload |
| `PKG/Sources/CaptureCore/UploadQueue.swift` | Delivery order, backoff, failure accounting |
| `PKG/Sources/SecondBrainCapture/AudioProcesses.swift` | Core Audio process/device queries |
| `PKG/Sources/SecondBrainCapture/TapSource.swift` | Process tap + private aggregate device |
| `PKG/Sources/SecondBrainCapture/MicSource.swift` | AVAudioEngine mic input on a chosen device |
| `PKG/Sources/SecondBrainCapture/Probe.swift` | `--probe` diagnostics (processes, tap permission) |
| `PKG/Sources/SecondBrainCapture/CaptureSession.swift` | One capture: sources → resamplers → assembler → PCM |
| `PKG/Sources/SecondBrainCapture/CaptureController.swift` | Detector loop, recovery, finalize, uploads |
| `PKG/Sources/SecondBrainCapture/StatusMenu.swift` | Menu-bar item and actions |
| `PKG/Sources/SecondBrainCapture/main.swift` | App entry, permissions, login item |
| `PKG/Resources/Info.plist` | Bundle metadata and usage strings |
| `PKG/scripts/build-app.sh` | Build, bundle and ad-hoc sign the `.app` |
| `PKG/scripts/test_receiver.py` | Loopback v2 receiver for the integration test |
| `PKG/README.md` | Build, install, configure, verify |
| `PKG/Tests/CaptureCoreTests/*.swift` | Unit and integration tests |

---

### Task 1: Package scaffold, Config and AppMatcher

**Files:**
- Create: `PKG/Package.swift`, `PKG/.gitignore`, `PKG/Sources/CaptureCore/Config.swift`, `PKG/Sources/CaptureCore/AppMatcher.swift`, `PKG/Sources/SecondBrainCapture/main.swift`
- Test: `PKG/Tests/CaptureCoreTests/ConfigTests.swift`

- [ ] **Step 1: Create the package files**

`PKG/Package.swift`:

```swift
// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "SecondBrainCapture",
    platforms: [.macOS("14.4")],
    targets: [
        .target(name: "CaptureCore"),
        .executableTarget(name: "SecondBrainCapture", dependencies: ["CaptureCore"]),
        .testTarget(name: "CaptureCoreTests", dependencies: ["CaptureCore"]),
    ]
)
```

`PKG/.gitignore`:

```
.build/
build/
```

`PKG/Sources/SecondBrainCapture/main.swift` (temporary; replaced in Task 10):

```swift
print("SecondBrainCapture")
```

- [ ] **Step 2: Write the failing tests**

`PKG/Tests/CaptureCoreTests/ConfigTests.swift`:

```swift
import XCTest
@testable import CaptureCore

final class ConfigTests: XCTestCase {
    var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    func testLoadCreatesDefaultsWhenMissing() throws {
        let url = dir.appendingPathComponent("sub/config.json")
        let config = try Config.load(from: url)
        XCTAssertEqual(config, Config())
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
        XCTAssertFalse(config.isConfigured)
    }

    func testPartialFileKeepsDefaultsForMissingKeys() throws {
        let url = dir.appendingPathComponent("config.json")
        try Data(#"{"host": "192.168.1.85"}"#.utf8).write(to: url)
        let config = try Config.load(from: url)
        XCTAssertEqual(config.host, "192.168.1.85")
        XCTAssertEqual(config.port, 7331)
        XCTAssertEqual(config.allowlist, Config.defaultAllowlist)
        XCTAssertTrue(config.isConfigured)
    }

    func testSaveRoundTrips() throws {
        let url = dir.appendingPathComponent("config.json")
        let config = Config(host: "h", port: 9, allowlist: ["a"], minCaptureSeconds: 5)
        try config.save(to: url)
        XCTAssertEqual(try Config.load(from: url), config)
    }

    func testSecretPathExpandsTilde() {
        XCTAssertEqual(Config().expandedSecretPath, NSHomeDirectory() + "/.omi-local/upload-secret.hex")
    }

    func testAppMatcherMatchesAppAndHelpersOnly() {
        let matcher = AppMatcher(allowlist: ["us.zoom.xos", "com.amazon.Amazon-Chime"])
        XCTAssertEqual(matcher.family(of: "us.zoom.xos"), "us.zoom.xos")
        XCTAssertEqual(matcher.family(of: "us.zoom.xos.ZoomAudioHelper"), "us.zoom.xos")
        XCTAssertNil(matcher.family(of: "us.zoom.xosx"))
        XCTAssertNil(matcher.family(of: "com.apple.Music"))
    }
}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd mac/SecondBrainCapture && swift test`
Expected: compile errors `cannot find 'Config' in scope` / `cannot find 'AppMatcher' in scope`.

- [ ] **Step 4: Implement**

`PKG/Sources/CaptureCore/Config.swift`:

```swift
import Foundation

/// User configuration in `~/Library/Application Support/SecondBrainCapture/config.json`.
/// Missing keys take defaults, so the file can stay minimal (`{"host": "..."}`).
public struct Config: Codable, Equatable {
    public var host: String
    public var port: UInt16
    public var allowlist: [String]
    public var secretPath: String
    public var minCaptureSeconds: Double
    public var releaseGraceSeconds: Double
    public var spoolWarnBytes: Int64

    public static let defaultAllowlist = [
        "com.amazon.Amazon-Chime", "us.zoom.xos", "com.microsoft.teams2",
        "com.tinyspeck.slackmacgap", "Cisco-Systems.Spark",
    ]

    public init(host: String = "", port: UInt16 = 7331, allowlist: [String] = Config.defaultAllowlist,
                secretPath: String = "~/.omi-local/upload-secret.hex", minCaptureSeconds: Double = 60,
                releaseGraceSeconds: Double = 20, spoolWarnBytes: Int64 = 5_000_000_000) {
        self.host = host
        self.port = port
        self.allowlist = allowlist
        self.secretPath = secretPath
        self.minCaptureSeconds = minCaptureSeconds
        self.releaseGraceSeconds = releaseGraceSeconds
        self.spoolWarnBytes = spoolWarnBytes
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = Config()
        host = try c.decodeIfPresent(String.self, forKey: .host) ?? d.host
        port = try c.decodeIfPresent(UInt16.self, forKey: .port) ?? d.port
        allowlist = try c.decodeIfPresent([String].self, forKey: .allowlist) ?? d.allowlist
        secretPath = try c.decodeIfPresent(String.self, forKey: .secretPath) ?? d.secretPath
        minCaptureSeconds = try c.decodeIfPresent(Double.self, forKey: .minCaptureSeconds) ?? d.minCaptureSeconds
        releaseGraceSeconds = try c.decodeIfPresent(Double.self, forKey: .releaseGraceSeconds) ?? d.releaseGraceSeconds
        spoolWarnBytes = try c.decodeIfPresent(Int64.self, forKey: .spoolWarnBytes) ?? d.spoolWarnBytes
    }

    public static var directory: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SecondBrainCapture", isDirectory: true)
    }

    /// Load `url`, creating it with defaults if it does not exist.
    public static func load(from url: URL) throws -> Config {
        guard FileManager.default.fileExists(atPath: url.path) else {
            let config = Config()
            try config.save(to: url)
            return config
        }
        return try JSONDecoder().decode(Config.self, from: Data(contentsOf: url))
    }

    public func save(to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(self).write(to: url, options: .atomic)
    }

    public var isConfigured: Bool { !host.isEmpty }
    public var expandedSecretPath: String { (secretPath as NSString).expandingTildeInPath }
}
```

`PKG/Sources/CaptureCore/AppMatcher.swift`:

```swift
/// Maps a process bundle ID to its allowlisted app family: the app itself or one of
/// its helpers (`<family>.<anything>`), whose audio may come from a helper process.
public struct AppMatcher {
    public let allowlist: [String]

    public init(allowlist: [String]) {
        self.allowlist = allowlist
    }

    public func family(of bundleID: String) -> String? {
        allowlist.first { bundleID == $0 || bundleID.hasPrefix($0 + ".") }
    }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd mac/SecondBrainCapture && swift test`
Expected: `Executed 5 tests, with 0 failures`.

- [ ] **Step 6: Commit**

```bash
git add mac/SecondBrainCapture
git commit -m "mac: scaffold capture package with config and app matching"
```

---

### Task 2: MeetingDetector

**Files:**
- Create: `PKG/Sources/CaptureCore/MeetingDetector.swift`
- Test: `PKG/Tests/CaptureCoreTests/MeetingDetectorTests.swift`

- [ ] **Step 1: Write the failing tests**

```swift
import XCTest
@testable import CaptureCore

final class MeetingDetectorTests: XCTestCase {
    let zoom = AudioProcess(objectID: 10, bundleID: "us.zoom.xos", isRunningInput: true, inputDevices: [77])
    let zoomIdle = AudioProcess(objectID: 10, bundleID: "us.zoom.xos", isRunningInput: false)
    let zoomHelper = AudioProcess(objectID: 11, bundleID: "us.zoom.xos.helper", isRunningInput: false)
    let music = AudioProcess(objectID: 20, bundleID: "com.apple.Music", isRunningInput: true)

    func make() -> MeetingDetector {
        MeetingDetector(matcher: AppMatcher(allowlist: Config.defaultAllowlist), releaseGraceSeconds: 20)
    }

    func testStartsWhenAllowlistedAppHoldsMicAndTapsItsHelpers() {
        let d = make()
        XCTAssertNil(d.update([music], now: 0))
        XCTAssertEqual(d.update([zoom, zoomHelper, music], now: 1),
                       .start(app: "us.zoom.xos", processes: [10, 11], inputDevice: 77))
        XCTAssertNil(d.update([zoom, zoomHelper], now: 2))
        XCTAssertEqual(d.activeApp, "us.zoom.xos")
    }

    func testStopsOnlyAfterReleaseGrace() {
        let d = make()
        _ = d.update([zoom], now: 0)
        XCTAssertNil(d.update([zoomIdle], now: 10))
        XCTAssertNil(d.update([zoomIdle], now: 29.9))
        XCTAssertEqual(d.update([zoomIdle], now: 30), .stop)
        XCTAssertNil(d.activeApp)
    }

    func testReacquiringMicWithinGraceKeepsCapture() {
        let d = make()
        _ = d.update([zoom], now: 0)
        XCTAssertNil(d.update([zoomIdle], now: 10))
        XCTAssertNil(d.update([zoom], now: 25))
        XCTAssertNil(d.update([zoomIdle], now: 40))
        XCTAssertNil(d.update([zoomIdle], now: 59))
        XCTAssertEqual(d.update([zoomIdle], now: 60), .stop)
    }

    func testSkipStopsAndIgnoresAppUntilItReleasesMic() {
        let d = make()
        _ = d.update([zoom], now: 0)
        XCTAssertEqual(d.skip(), .stop)
        XCTAssertNil(d.update([zoom], now: 5))
        XCTAssertNil(d.update([zoomIdle], now: 6))
        XCTAssertEqual(d.update([zoom], now: 7), .start(app: "us.zoom.xos", processes: [10], inputDevice: 77))
        XCTAssertNil(make().skip())
    }

    func testPauseStopsAndDefersStart() {
        let d = make()
        _ = d.update([zoom], now: 0)
        XCTAssertEqual(d.pause(until: 3600), .stop)
        XCTAssertNil(d.update([zoom], now: 100))
        XCTAssertEqual(d.pausedUntil, 3600)
        XCTAssertNotNil(d.update([zoom], now: 3600))
        XCTAssertNil(make().pause(until: 10))
    }

    func testResumeClearsPause() {
        let d = make()
        _ = d.pause(until: 3600)
        _ = d.pause(until: 0)
        XCTAssertNotNil(d.update([zoom], now: 5))
    }

    func testFirstAllowlistedAppWinsWhenSeveralHoldMic() {
        let chime = AudioProcess(objectID: 30, bundleID: "com.amazon.Amazon-Chime", isRunningInput: true)
        XCTAssertEqual(make().update([zoom, chime], now: 0),
                       .start(app: "com.amazon.Amazon-Chime", processes: [30], inputDevice: nil))
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac/SecondBrainCapture && swift test --filter MeetingDetectorTests`
Expected: compile error `cannot find 'AudioProcess' in scope`.

- [ ] **Step 3: Implement**

`PKG/Sources/CaptureCore/MeetingDetector.swift`:

```swift
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
        skippedApp = app
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
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac/SecondBrainCapture && swift test`
Expected: `Executed 12 tests, with 0 failures`.

- [ ] **Step 5: Commit**

```bash
git add mac/SecondBrainCapture
git commit -m "mac: detect meetings from allowlisted apps holding the mic"
```

---

### Task 3: StereoAssembler and Resampler

**Files:**
- Create: `PKG/Sources/CaptureCore/StereoAssembler.swift`, `PKG/Sources/CaptureCore/Resampler.swift`
- Test: `PKG/Tests/CaptureCoreTests/AudioPathTests.swift`

- [ ] **Step 1: Write the failing tests**

```swift
import AVFoundation
import XCTest
@testable import CaptureCore

final class AudioPathTests: XCTestCase {
    func testDrainInterleavesMicLeftRemoteRight() {
        let a = StereoAssembler()
        a.appendLeft([1, 0])
        a.appendRight([-1, 0.5])
        XCTAssertEqual(a.drain(), [32767, -32767, 0, 16384])
        XCTAssertEqual(a.drain(), [])
    }

    func testDrainWaitsForLaggingChannelWithinSkew() {
        let a = StereoAssembler()
        a.appendLeft([Float](repeating: 0.1, count: 10))
        a.appendRight([Float](repeating: 0.2, count: 4))
        XCTAssertEqual(a.drain().count, 8)
        a.appendRight([Float](repeating: 0.2, count: 6))
        XCTAssertEqual(a.drain().count, 12)
    }

    func testDrainPadsChannelThatLagsBeyondSkew() {
        let a = StereoAssembler(maxSkewFrames: 5)
        a.appendLeft([Float](repeating: 0.5, count: 10))
        let out = a.drain()
        XCTAssertEqual(out.count, 10)
        XCTAssertEqual(out[1], 0)
    }

    func testFlushPadsShorterChannel() {
        let a = StereoAssembler()
        a.appendLeft([1, 1, 1])
        a.appendRight([1])
        XCTAssertEqual(a.flush(), [32767, 32767, 32767, 0, 32767, 0])
    }

    func testSamplesAreClipped() {
        let a = StereoAssembler()
        a.appendLeft([2])
        a.appendRight([-3])
        XCTAssertEqual(a.drain(), [32767, -32767])
    }

    func testResamplerDownsamples48kStereoTo16kMono() throws {
        let input = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2))
        let resampler = try XCTUnwrap(Resampler(from: input))
        var total = 0
        var peak: Float = 0
        for chunk in 0..<10 {
            let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: input, frameCapacity: 4800))
            buffer.frameLength = 4800
            for channel in 0..<2 {
                for i in 0..<4800 {
                    buffer.floatChannelData![channel][i] = 0.5 * sin(Float(chunk * 4800 + i) * 2 * .pi * 440 / 48000)
                }
            }
            let out = resampler.convert(buffer)
            total += out.count
            peak = max(peak, out.map(abs).max() ?? 0)
        }
        XCTAssertEqual(Double(total), 16000, accuracy: 320)
        XCTAssertGreaterThan(peak, 0.3)
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac/SecondBrainCapture && swift test --filter AudioPathTests`
Expected: compile error `cannot find 'StereoAssembler' in scope`.

- [ ] **Step 3: Implement**

`PKG/Sources/CaptureCore/StereoAssembler.swift`:

```swift
/// Joins two independently clocked 16 kHz mono streams into interleaved stereo Int16:
/// left = owner mic, right = remote (tapped app). If one stream stalls for more than
/// `maxSkewFrames`, it is padded with silence so memory stays bounded.
public final class StereoAssembler {
    private var left: [Float] = []
    private var right: [Float] = []
    private let maxSkew: Int

    public init(maxSkewFrames: Int = 16000) {
        maxSkew = maxSkewFrames
    }

    public func appendLeft(_ samples: [Float]) { left += samples }
    public func appendRight(_ samples: [Float]) { right += samples }

    /// Frames available on both channels.
    public func drain() -> [Int16] {
        if left.count > right.count + maxSkew {
            right += [Float](repeating: 0, count: left.count - right.count - maxSkew)
        }
        if right.count > left.count + maxSkew {
            left += [Float](repeating: 0, count: right.count - left.count - maxSkew)
        }
        return take(min(left.count, right.count))
    }

    /// Everything left at the end of a capture; the shorter channel is padded with silence.
    public func flush() -> [Int16] {
        let n = max(left.count, right.count)
        left += [Float](repeating: 0, count: n - left.count)
        right += [Float](repeating: 0, count: n - right.count)
        return take(n)
    }

    private func take(_ n: Int) -> [Int16] {
        var out = [Int16](repeating: 0, count: 2 * n)
        for i in 0..<n {
            out[2 * i] = Self.pcm(left[i])
            out[2 * i + 1] = Self.pcm(right[i])
        }
        left.removeFirst(n)
        right.removeFirst(n)
        return out
    }

    static func pcm(_ x: Float) -> Int16 {
        Int16((max(-1, min(1, x)) * 32767).rounded())
    }
}
```

`PKG/Sources/CaptureCore/Resampler.swift`:

```swift
import AVFoundation

/// Converts buffers of one fixed input format to 16 kHz mono Float32 (downmixing).
/// Not thread-safe: use one instance per source, from that source's thread.
public final class Resampler {
    public static let outputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000,
                                                   channels: 1, interleaved: false)!
    private let converter: AVAudioConverter

    public init?(from input: AVAudioFormat) {
        guard let converter = AVAudioConverter(from: input, to: Self.outputFormat) else { return nil }
        converter.downmix = true
        self.converter = converter
    }

    public func convert(_ buffer: AVAudioPCMBuffer) -> [Float] {
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 16000 / buffer.format.sampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: Self.outputFormat, frameCapacity: capacity) else { return [] }
        var supplied = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if supplied {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, let samples = out.floatChannelData?[0] else { return [] }
        return Array(UnsafeBufferPointer(start: samples, count: Int(out.frameLength)))
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac/SecondBrainCapture && swift test`
Expected: `Executed 18 tests, with 0 failures`.

- [ ] **Step 5: Commit**

```bash
git add mac/SecondBrainCapture
git commit -m "mac: resample and assemble mic/remote audio into stereo PCM"
```

---

### Task 4: CaptureRecord, Spool and PCMWriter

**Files:**
- Create: `PKG/Sources/CaptureCore/CaptureRecord.swift`, `PKG/Sources/CaptureCore/Spool.swift`, `PKG/Sources/CaptureCore/PCMWriter.swift`
- Test: `PKG/Tests/CaptureCoreTests/SpoolTests.swift`

- [ ] **Step 1: Write the failing tests**

```swift
import XCTest
@testable import CaptureCore

final class SpoolTests: XCTestCase {
    var dir: URL!
    var spool: Spool!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        spool = try Spool(root: dir.appendingPathComponent("spool"))
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    func testSpoolDirectoryIsPrivate() throws {
        let perms = try FileManager.default.attributesOfItem(atPath: spool.root.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(perms?.intValue, 0o700)
    }

    func testRecordsRoundTripOldestFirstAndSkipCorruptFiles() throws {
        let late = CaptureRecord(app: "us.zoom.xos", startMs: 2000)
        let early = CaptureRecord(app: "us.zoom.xos", startMs: 1000, endMs: 1500, state: .complete, opened: true)
        try spool.save(late)
        try spool.save(early)
        try Data("not json".utf8).write(to: spool.root.appendingPathComponent("junk.json"))
        XCTAssertEqual(spool.records(), [early, late])
    }

    func testRecordUsesReceiverFieldNames() throws {
        let record = CaptureRecord(captureID: "00112233445566778899aabbccddeeff", app: "x", startMs: 1, endMs: 2)
        try spool.save(record)
        let json = try JSONSerialization.jsonObject(with: Data(contentsOf: spool.recordURL(record.captureID))) as? [String: Any]
        XCTAssertEqual(json?["capture_id"] as? String, record.captureID)
        XCTAssertEqual(json?["start_ms"] as? Int, 1)
        XCTAssertEqual(record.uploadMetadata["end_ms"] as? Int64, 2)
        XCTAssertEqual(record.uploadMetadata["app"] as? String, "x")
        XCTAssertEqual(record.uploadMetadata["channels"] as? [String: String], ["L": "mic", "R": "remote"])
    }

    func testNewIDIs32Hex() {
        let id = CaptureRecord.newID()
        XCTAssertEqual(id.count, 32)
        XCTAssertTrue(id.allSatisfy(\.isHexDigit))
        XCTAssertNotEqual(id, CaptureRecord.newID())
    }

    func testDeleteAudioKeepsRecordAndDeleteRemovesAll() throws {
        let record = CaptureRecord(app: "x", startMs: 1)
        try spool.save(record)
        try Data([1]).write(to: spool.pcmURL(record.captureID))
        try Data([2]).write(to: spool.cafURL(record.captureID))
        XCTAssertEqual(spool.totalBytes() > 2, true)
        spool.deleteAudio(record.captureID)
        XCTAssertFalse(FileManager.default.fileExists(atPath: spool.pcmURL(record.captureID).path))
        XCTAssertEqual(spool.records().count, 1)
        spool.delete(record.captureID)
        XCTAssertEqual(spool.records().count, 0)
    }

    func testPCMWriterCountsFramesAndResumesAtEnd() throws {
        let url = spool.pcmURL("a")
        let writer = try PCMWriter(url: url)
        try writer.write([1, 2, 3, 4], now: 0)
        try writer.close()
        XCTAssertEqual(writer.framesWritten, 2)
        let again = try PCMWriter(url: url)
        XCTAssertEqual(again.framesWritten, 2)
        try again.write([5, 6], now: 10)
        try again.close()
        XCTAssertEqual(try Data(contentsOf: url).count, 12)
    }

    func testTruncateDropsTornTrailingFrame() throws {
        let url = spool.pcmURL("b")
        try Data(repeating: 7, count: 4 * 3 + 3).write(to: url)
        XCTAssertEqual(try PCMWriter.truncateToWholeFrames(url), 3)
        XCTAssertEqual(try Data(contentsOf: url).count, 12)
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac/SecondBrainCapture && swift test --filter SpoolTests`
Expected: compile error `cannot find 'Spool' in scope`.

- [ ] **Step 3: Implement**

`PKG/Sources/CaptureCore/CaptureRecord.swift`:

```swift
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
```

`PKG/Sources/CaptureCore/Spool.swift`:

```swift
import Foundation

/// The local spool: `<id>.json` records, `<id>.pcm` while recording, `<id>.caf` after encoding.
/// The directory is private to the user (0700); files are deleted only after the receiver commits them.
public final class Spool {
    public let root: URL

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
            .sorted { $0.startMs < $1.startMs }
    }

    public func deleteAudio(_ id: String) {
        for url in [pcmURL(id), cafURL(id)] {
            try? FileManager.default.removeItem(at: url)
        }
    }

    public func delete(_ id: String) {
        deleteAudio(id)
        try? FileManager.default.removeItem(at: recordURL(id))
    }

    public func totalBytes() -> Int64 {
        let files = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: [.fileSizeKey])) ?? []
        return files.reduce(0) { $0 + Int64((try? $1.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
    }
}
```

`PKG/Sources/CaptureCore/PCMWriter.swift`:

```swift
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac/SecondBrainCapture && swift test`
Expected: `Executed 25 tests, with 0 failures`.

- [ ] **Step 5: Commit**

```bash
git add mac/SecondBrainCapture
git commit -m "mac: add crash-safe capture spool and PCM writer"
```

---

### Task 5: CaptureEncoder

A spike on this Mac confirmed that `AVAudioFile` writes stereo 16 kHz Opus in CAF and that the receiver's ffmpeg decodes it (`codec_name=opus`, 2 channels).

**Files:**
- Create: `PKG/Sources/CaptureCore/CaptureEncoder.swift`
- Test: `PKG/Tests/CaptureCoreTests/CaptureEncoderTests.swift`

- [ ] **Step 1: Write the failing test**

```swift
import AVFoundation
import XCTest
@testable import CaptureCore

final class CaptureEncoderTests: XCTestCase {
    func testEncodesPCMToStereoOpusCAF() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let pcm = dir.appendingPathComponent("a.pcm")
        let caf = dir.appendingPathComponent("a.caf")
        var samples = [Int16](repeating: 0, count: 2 * 48000)
        for i in 0..<48000 {
            samples[2 * i] = Int16(10000 * sin(Double(i) * 2 * .pi * 440 / 16000))
            samples[2 * i + 1] = samples[2 * i] / 2
        }
        let writer = try PCMWriter(url: pcm)
        try writer.write(samples, now: 0)
        try writer.close()

        try CaptureEncoder.encode(pcm: pcm, to: caf)

        let file = try AVAudioFile(forReading: caf)
        XCTAssertEqual(file.fileFormat.channelCount, 2)
        XCTAssertEqual(file.fileFormat.sampleRate, 16000)
        XCTAssertEqual(file.fileFormat.streamDescription.pointee.mFormatID, kAudioFormatOpus)
        XCTAssertEqual(Double(file.length), 48000, accuracy: 960)
        let pcmSize = try XCTUnwrap(try FileManager.default.attributesOfItem(atPath: pcm.path)[.size] as? NSNumber).intValue
        let cafSize = try XCTUnwrap(try FileManager.default.attributesOfItem(atPath: caf.path)[.size] as? NSNumber).intValue
        XCTAssertLessThan(cafSize, pcmSize / 5)
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.contains("tmp") }
        XCTAssertEqual(leftovers, [])
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mac/SecondBrainCapture && swift test --filter CaptureEncoderTests`
Expected: compile error `cannot find 'CaptureEncoder' in scope`.

- [ ] **Step 3: Implement**

`PKG/Sources/CaptureCore/CaptureEncoder.swift`:

```swift
import AVFoundation

/// Encodes spooled 16 kHz stereo Int16 PCM into stereo Opus (32 kbit/s) in CAF, which the
/// receiver's ffmpeg decodes. Writes a temporary file and renames it, so a crash during
/// encoding never leaves a truncated `.caf`.
public enum CaptureEncoder {
    public static let pcmFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000,
                                                channels: 2, interleaved: true)!

    public static func encode(pcm: URL, to caf: URL) throws {
        // AVAudioFile picks the container from the extension, so keep ".caf" last.
        let temporary = caf.deletingPathExtension().appendingPathExtension("tmp").appendingPathExtension("caf")
        try? FileManager.default.removeItem(at: temporary)
        try write(pcm: pcm, to: temporary)
        try? FileManager.default.removeItem(at: caf)
        try FileManager.default.moveItem(at: temporary, to: caf)
    }

    /// Separate function so the AVAudioFile is released (and finalized) before the rename.
    private static func write(pcm: URL, to url: URL) throws {
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatOpus, AVSampleRateKey: 16000, AVNumberOfChannelsKey: 2,
            AVEncoderBitRateKey: 32000,
        ]
        let file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: .pcmFormatInt16, interleaved: true)
        let input = try FileHandle(forReadingFrom: pcm)
        defer { try? input.close() }
        let chunkFrames = 16000
        guard let buffer = AVAudioPCMBuffer(pcmFormat: pcmFormat, frameCapacity: AVAudioFrameCount(chunkFrames)) else {
            throw CocoaError(.fileWriteUnknown)
        }
        while let data = try input.read(upToCount: chunkFrames * 4), !data.isEmpty {
            let frames = data.count / 4
            data.withUnsafeBytes { raw in
                buffer.int16ChannelData![0].update(from: raw.bindMemory(to: Int16.self).baseAddress!, count: frames * 2)
            }
            buffer.frameLength = AVAudioFrameCount(frames)
            try file.write(from: buffer)
        }
        if #available(macOS 15.0, *) {
            file.close()
        }
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac/SecondBrainCapture && swift test`
Expected: `Executed 26 tests, with 0 failures`.

- [ ] **Step 5: Commit**

```bash
git add mac/SecondBrainCapture
git commit -m "mac: encode captures to stereo Opus CAF"
```

---

### Task 6: Wire protocol and client identity

**Files:**
- Create: `PKG/Sources/CaptureCore/Wire.swift`, `PKG/Sources/CaptureCore/ClientIdentity.swift`
- Test: `PKG/Tests/CaptureCoreTests/WireTests.swift`

The expected bytes below were produced by the Python receiver's `omi_local.upload_protocol` with secret `00 01 … 1f`, client nonce `a0 … af`, server nonce `b0 … bf`, capture id `00 01 … 0f`.

- [ ] **Step 1: Write the failing tests**

```swift
import CryptoKit
import XCTest
@testable import CaptureCore

final class WireTests: XCTestCase {
    let secret = Data((0..<32).map { UInt8($0) })
    let clientNonce = Data((0..<16).map { UInt8(0xA0 + $0) })
    let serverNonce = Data((0..<16).map { UInt8(0xB0 + $0) })
    let captureID = Data((0..<16).map { UInt8($0) })

    func testAuthTagsMatchPythonReceiver() {
        XCTAssertEqual(Wire.authTag(secret: secret, label: Wire.labelServer, clientNonce: clientNonce, serverNonce: serverNonce).hex,
                       "f82dc3e03e829f7ab1d4adbbf870815979bcdc1053ed4764e467412e7f5d3f1f")
        XCTAssertEqual(Wire.authTag(secret: secret, label: Wire.labelClient, clientNonce: clientNonce, serverNonce: serverNonce).hex,
                       "5089e2fe21da82fc22dbeaa1ba0c1b187dd3d2105d9749a614172f6e8bb096e9")
    }

    func testFramesMatchPythonReceiver() {
        XCTAssertEqual(Wire.frame(.hello, Wire.hello(clientID: Data([1, 2, 3, 4, 5, 6]), nonce: clientNonce)).hex,
                       "010000001b4f4d494c02010203040506a0a1a2a3a4a5a6a7a8a9aaabacadaeaf")
        XCTAssertEqual(Wire.frame(.captureOpen, Wire.captureOpen(id: captureID, startMs: 1_790_000_000_123, app: "us.zoom.xos")).hex,
                       "1000000023000102030405060708090a0b0c0d0e0f000001a0c4506c7b75732e7a6f6f6d2e786f73")
        XCTAssertEqual(Wire.frame(.fileData, Wire.fileData(offset: 4096, bytes: Data("abc".utf8))).hex,
                       "130000000b0000000000001000616263")
    }

    func testFileBeginHeadAndMetadata() throws {
        let sha = Data(SHA256.hash(data: Data("hello".utf8)))
        let payload = try Wire.fileBegin(id: captureID, totalLength: 5, sha256: sha,
                                         metadata: ["start_ms": Int64(1), "end_ms": Int64(2), "app": "x"])
        XCTAssertEqual(payload.prefix(56).hex,
                       "000102030405060708090a0b0c0d0e0f0000000000000005"
                       + "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
        let json = try JSONSerialization.jsonObject(with: payload.dropFirst(56)) as? [String: Any]
        XCTAssertEqual(json?["start_ms"] as? Int, 1)
        XCTAssertEqual(json?["end_ms"] as? Int, 2)
        XCTAssertEqual(json?["app"] as? String, "x")
    }

    func testHeaderAndIntegerHelpers() {
        let (type, length) = Wire.parseHeader(Data([0x16, 0, 0, 1, 2]))
        XCTAssertEqual(type, 0x16)
        XCTAssertEqual(length, 258)
        XCTAssertEqual(Wire.readU64(Wire.u64(0x0102_0304_0506_0708)), 0x0102_0304_0506_0708)
        XCTAssertEqual(Wire.readU64(Data([9, 0, 0, 0, 0, 0, 0, 0, 5]).dropFirst()), 5)
    }

    func testHexRoundTripAndRejectsBadHex() {
        XCTAssertEqual(Data(hex: "00ff10")?.hex, "00ff10")
        XCTAssertNil(Data(hex: "0g"))
        XCTAssertNil(Data(hex: "abc"))
    }

    func testClientIDIsFirstSixBytesOfUUIDHash() {
        let id = ClientIdentity.clientID(uuid: "ABC")
        XCTAssertEqual(id, Data(SHA256.hash(data: Data("ABC".utf8)).prefix(6)))
        XCTAssertEqual(id.count, 6)
        XCTAssertNotNil(ClientIdentity.platformUUID())
    }

    func testLoadSecretParsesHexFileAndRejectsBadLength() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: url) }
        try (secret.hex + "\n").write(to: url, atomically: true, encoding: .utf8)
        XCTAssertEqual(try ClientIdentity.loadSecret(path: url.path), secret)
        try "abcd".write(to: url, atomically: true, encoding: .utf8)
        XCTAssertThrowsError(try ClientIdentity.loadSecret(path: url.path))
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac/SecondBrainCapture && swift test --filter WireTests`
Expected: compile error `cannot find 'Wire' in scope`.

- [ ] **Step 3: Implement**

`PKG/Sources/CaptureCore/Wire.swift`:

```swift
import CryptoKit
import Foundation

/// Receiver protocol v2 (see omi_local/upload_protocol.py). Big-endian integers;
/// frames are [type:u8][length:u32][payload].
public enum Wire {
    public enum Msg: UInt8 {
        case hello = 0x01, challenge = 0x02, auth = 0x03, reject = 0x7F
        case captureOpen = 0x10, captureCancel = 0x11, fileBegin = 0x12, fileData = 0x13, fileEnd = 0x14
        case fileStart = 0x15, fileAck = 0x16, fileBye = 0x17, ok = 0x18
    }

    public enum Reject: UInt8 {
        case auth = 1, protocolError = 2, busy = 3
    }

    public static let version: UInt8 = 2
    public static let headerLength = 5
    public static let maxChunk = 64 * 1024
    public static let labelServer = Data("omi-local-srv".utf8)
    public static let labelClient = Data("omi-local-cli".utf8)

    public static func frame(_ type: Msg, _ payload: Data = Data()) -> Data {
        var out = Data([type.rawValue])
        out.append(u32(UInt32(payload.count)))
        out.append(payload)
        return out
    }

    public static func parseHeader(_ header: Data) -> (UInt8, Int) {
        let b = [UInt8](header)
        return (b[0], Int(b[1]) << 24 | Int(b[2]) << 16 | Int(b[3]) << 8 | Int(b[4]))
    }

    public static func hello(clientID: Data, nonce: Data) -> Data {
        Data("OMIL".utf8) + Data([version]) + clientID + nonce
    }

    public static func authTag(secret: Data, label: Data, clientNonce: Data, serverNonce: Data) -> Data {
        Data(HMAC<SHA256>.authenticationCode(for: label + clientNonce + serverNonce, using: SymmetricKey(data: secret)))
    }

    public static func captureOpen(id: Data, startMs: Int64, app: String) -> Data {
        id + u64(UInt64(startMs)) + Data(app.utf8)
    }

    public static func fileBegin(id: Data, totalLength: Int64, sha256: Data, metadata: [String: Any]) throws -> Data {
        id + u64(UInt64(totalLength)) + sha256 + (try JSONSerialization.data(withJSONObject: metadata, options: [.sortedKeys]))
    }

    public static func fileData(offset: Int64, bytes: Data) -> Data {
        u64(UInt64(offset)) + bytes
    }

    public static func u32(_ v: UInt32) -> Data {
        Data([UInt8(v >> 24 & 0xFF), UInt8(v >> 16 & 0xFF), UInt8(v >> 8 & 0xFF), UInt8(v & 0xFF)])
    }

    public static func u64(_ v: UInt64) -> Data {
        Data((0..<8).reversed().map { UInt8(v >> (UInt64($0) * 8) & 0xFF) })
    }

    public static func readU64(_ data: Data) -> UInt64 {
        data.prefix(8).reduce(0) { $0 << 8 | UInt64($1) }
    }
}

extension Data {
    public init?(hex: String) {
        guard hex.count % 2 == 0 else { return nil }
        var bytes = [UInt8]()
        var index = hex.startIndex
        while index < hex.endIndex {
            let next = hex.index(index, offsetBy: 2)
            guard let byte = UInt8(hex[index..<next], radix: 16) else { return nil }
            bytes.append(byte)
            index = next
        }
        self.init(bytes)
    }

    public var hex: String {
        map { String(format: "%02x", $0) }.joined()
    }
}
```

`PKG/Sources/CaptureCore/ClientIdentity.swift`:

```swift
import CryptoKit
import Foundation
import IOKit

public enum ClientIdentity {
    /// The Mac's hardware UUID (stable across reinstalls).
    public static func platformUUID() -> String? {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"))
        guard service != 0 else { return nil }
        defer { IOObjectRelease(service) }
        return IORegistryEntryCreateCFProperty(service, kIOPlatformUUIDKey as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue() as? String
    }

    /// The 6-byte v2 client id: the first bytes of SHA-256(hardware UUID).
    public static func clientID(uuid: String) -> Data {
        Data(SHA256.hash(data: Data(uuid.utf8)).prefix(6))
    }

    public struct SecretError: Error, CustomStringConvertible {
        public let description: String
    }

    /// The shared 32-byte secret stored as 64 hex characters (the receiver's upload-secret.hex).
    public static func loadSecret(path: String) throws -> Data {
        let text = try String(contentsOfFile: path, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        guard let secret = Data(hex: text), secret.count == 32 else {
            throw SecretError(description: "\(path): expected 64 hex characters")
        }
        return secret
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac/SecondBrainCapture && swift test`
Expected: `Executed 33 tests, with 0 failures`.

- [ ] **Step 5: Commit**

```bash
git add mac/SecondBrainCapture
git commit -m "mac: add receiver protocol v2 codec and client identity"
```

---

### Task 7: UploadClient with a real-receiver integration test

**Files:**
- Create: `PKG/Sources/CaptureCore/UploadClient.swift`, `PKG/scripts/test_receiver.py`
- Test: `PKG/Tests/CaptureCoreTests/UploadClientTests.swift`

- [ ] **Step 1: Add the loopback receiver script**

`PKG/scripts/test_receiver.py`:

```python
"""Loopback protocol-v2 receiver for the Swift integration test. Not for production.

usage: test_receiver.py DEST SECRET_HEX PORT_FILE
Writes the bound port to PORT_FILE, then serves until killed.
"""
import asyncio
import sys
from pathlib import Path

from omi_local.file_store import FileStore
from omi_local.server import UploadServer


async def main(dest: Path, secret_hex: str, port_file: Path) -> None:
    server = UploadServer(bytes.fromhex(secret_hex), dest, host="127.0.0.1", port=0,
                          file_store=FileStore(dest / "meetings"))
    await server.start()
    port_file.write_text(str(server.bound_port))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])))
```

- [ ] **Step 2: Write the failing tests**

`PKG/Tests/CaptureCoreTests/UploadClientTests.swift`:

```swift
import XCTest
@testable import CaptureCore

final class UploadClientTests: XCTestCase {
    func testUnreachableReceiverThrowsUnreachable() {
        let client = UploadClient(host: "127.0.0.1", port: 1, secret: Data(count: 32), clientID: Data(count: 6),
                                  timeoutSeconds: 2)
        XCTAssertThrowsError(try client.cancelCapture(id: "00112233445566778899aabbccddeeff")) { error in
            guard case UploadError.unreachable = error else { return XCTFail("\(error)") }
        }
    }

    func testAgainstRealReceiver() throws {
        guard let python = ProcessInfo.processInfo.environment["SBC_RECEIVER_PYTHON"] else {
            throw XCTSkip("set SBC_RECEIVER_PYTHON to a Python that can import omi_local")
        }
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let secret = Data((0..<32).map { UInt8($0) })
        let portFile = dir.appendingPathComponent("port")
        let script = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("scripts/test_receiver.py")
        let receiver = Process()
        receiver.executableURL = URL(fileURLWithPath: python)
        receiver.arguments = [script.path, dir.path, secret.hex, portFile.path]
        try receiver.run()
        defer {
            receiver.terminate()
            receiver.waitUntilExit()
        }
        var port: UInt16?
        for _ in 0..<200 where port == nil {
            port = (try? String(contentsOf: portFile, encoding: .utf8)).flatMap { UInt16($0) }
            if port == nil { Thread.sleep(forTimeInterval: 0.05) }
        }
        let bound = try XCTUnwrap(port, "receiver did not start")
        let client = UploadClient(host: "127.0.0.1", port: bound, secret: secret, clientID: Data([1, 2, 3, 4, 5, 6]))

        let record = CaptureRecord(captureID: "00112233445566778899aabbccddeeff", app: "us.zoom.xos",
                                   startMs: 1000, endMs: 61000, state: .complete)
        let file = dir.appendingPathComponent("audio.caf")
        let payload = Data((0..<200_000).map { UInt8($0 % 251) })
        try payload.write(to: file)

        try client.openCapture(record)
        try client.upload(record, file: file)
        try client.upload(record, file: file)  // already committed: FILE_START == length, still FILE_BYE

        let meetings = dir.appendingPathComponent("meetings")
        XCTAssertEqual(try Data(contentsOf: meetings.appendingPathComponent("\(record.captureID).caf")), payload)
        let sidecar = try JSONSerialization.jsonObject(
            with: Data(contentsOf: meetings.appendingPathComponent("\(record.captureID).json"))) as? [String: Any]
        XCTAssertEqual(sidecar?["start_ms"] as? Int, 1000)
        XCTAssertEqual(sidecar?["end_ms"] as? Int, 61000)
        XCTAssertEqual(sidecar?["app"] as? String, "us.zoom.xos")

        let cancelID = "ffeeddccbbaa99887766554433221100"
        try client.cancelCapture(id: cancelID)
        let marker = try JSONSerialization.jsonObject(
            with: Data(contentsOf: meetings.appendingPathComponent(".captures/\(cancelID).json"))) as? [String: Any]
        XCTAssertEqual(marker?["state"] as? String, "cancelled")

        let wrongSecret = UploadClient(host: "127.0.0.1", port: bound, secret: Data(repeating: 9, count: 32),
                                       clientID: Data([1, 2, 3, 4, 5, 6]))
        XCTAssertThrowsError(try wrongSecret.openCapture(record)) { XCTAssertEqual($0 as? UploadError, .authFailed) }

        let badMetadata = CaptureRecord(captureID: "0000000000000000000000000000000a", app: "",
                                        startMs: 5, endMs: 1, state: .complete)
        XCTAssertThrowsError(try client.upload(badMetadata, file: file)) {
            XCTAssertEqual($0 as? UploadError, .rejected(Wire.Reject.protocolError.rawValue))
        }
    }
}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd mac/SecondBrainCapture && swift test --filter UploadClientTests`
Expected: compile error `cannot find 'UploadClient' in scope`.

- [ ] **Step 4: Implement**

`PKG/Sources/CaptureCore/UploadClient.swift`:

```swift
import CryptoKit
import Darwin
import Foundation

public enum UploadError: Error, Equatable {
    case unreachable(String)
    case authFailed
    case rejected(UInt8)
    case protocolViolation(String)

    /// Worth retrying later without counting against the capture.
    public var isRetryable: Bool {
        switch self {
        case .unreachable, .authFailed: return true
        case .rejected(let code): return code == Wire.Reject.busy.rawValue
        case .protocolViolation: return false
        }
    }
}

/// What the upload queue needs from the network; faked in tests.
public protocol UploadTransport {
    func openCapture(_ record: CaptureRecord) throws
    func cancelCapture(id: String) throws
    func upload(_ record: CaptureRecord, file: URL) throws
}

/// Protocol-v2 client. Each call opens a short authenticated connection, because the
/// receiver drops idle connections after 60 s and any REJECT closes the connection.
public final class UploadClient: UploadTransport {
    let host: String
    let port: UInt16
    let secret: Data
    let clientID: Data
    let timeout: Int

    public init(host: String, port: UInt16, secret: Data, clientID: Data, timeoutSeconds: Int = 30) {
        self.host = host
        self.port = port
        self.secret = secret
        self.clientID = clientID
        timeout = timeoutSeconds
    }

    public func openCapture(_ record: CaptureRecord) throws {
        let id = try Self.captureID(record.captureID)
        try session { c in
            _ = try c.expect(c.call(.captureOpen, Wire.captureOpen(id: id, startMs: record.startMs, app: record.app)), .ok)
        }
    }

    public func cancelCapture(id: String) throws {
        let captureID = try Self.captureID(id)
        try session { c in _ = try c.expect(c.call(.captureCancel, captureID), .ok) }
    }

    public func upload(_ record: CaptureRecord, file: URL) throws {
        let id = try Self.captureID(record.captureID)
        let (size, digest) = try Self.sizeAndDigest(file)
        let begin = try Wire.fileBegin(id: id, totalLength: size, sha256: digest, metadata: record.uploadMetadata)
        try session { c in
            var offset = Int64(try Wire.readU64(c.expect(c.call(.fileBegin, begin), .fileStart)))
            let handle = try FileHandle(forReadingFrom: file)
            defer { try? handle.close() }
            while offset < size {
                try handle.seek(toOffset: UInt64(offset))
                let chunk = try handle.read(upToCount: Wire.maxChunk) ?? Data()
                guard !chunk.isEmpty else { throw UploadError.protocolViolation("file shrank during upload") }
                let acked = Int64(try Wire.readU64(c.expect(c.call(.fileData, Wire.fileData(offset: offset, bytes: chunk)), .fileAck)))
                guard acked == offset + Int64(chunk.count) else { throw UploadError.protocolViolation("unexpected ACK \(acked)") }
                offset = acked
            }
            guard try c.expect(c.call(.fileEnd), .fileBye) == Data([1]) else {
                throw UploadError.protocolViolation("receiver did not commit")
            }
        }
    }

    static func captureID(_ hex: String) throws -> Data {
        guard let id = Data(hex: hex), id.count == 16 else { throw UploadError.protocolViolation("bad capture id \(hex)") }
        return id
    }

    static func sizeAndDigest(_ url: URL) throws -> (Int64, Data) {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hash = SHA256()
        var size: Int64 = 0
        while let block = try handle.read(upToCount: 1 << 20), !block.isEmpty {
            hash.update(data: block)
            size += Int64(block.count)
        }
        return (size, Data(hash.finalize()))
    }

    private func session(_ body: (Connection) throws -> Void) throws {
        let c = try Connection(host: host, port: port, timeout: timeout)
        defer { c.close() }
        let nonce = Data((0..<16).map { _ in UInt8.random(in: 0...255) })
        let (kind, challenge) = try c.call(.hello, Wire.hello(clientID: clientID, nonce: nonce))
        if kind == Wire.Msg.reject.rawValue { throw UploadError.rejected(challenge.first ?? 0) }
        guard kind == Wire.Msg.challenge.rawValue, challenge.count == 48 else {
            throw UploadError.protocolViolation("expected CHALLENGE")
        }
        let serverNonce = Data(challenge.prefix(16))
        let expected = Wire.authTag(secret: secret, label: Wire.labelServer, clientNonce: nonce, serverNonce: serverNonce)
        guard Data(challenge.suffix(32)) == expected else { throw UploadError.authFailed }
        let tag = Wire.authTag(secret: secret, label: Wire.labelClient, clientNonce: nonce, serverNonce: serverNonce)
        let (reply, payload) = try c.call(.auth, tag)
        if reply == Wire.Msg.reject.rawValue {
            throw payload.first == Wire.Reject.auth.rawValue ? UploadError.authFailed : UploadError.rejected(payload.first ?? 0)
        }
        guard reply == Wire.Msg.ok.rawValue else { throw UploadError.protocolViolation("expected OK") }
        try body(c)
    }
}

/// A blocking TCP connection with connect/read/write timeouts.
final class Connection {
    private let fd: Int32

    init(host: String, port: UInt16, timeout: Int) throws {
        fd = try Self.open(host: host, port: port, timeout: timeout)
    }

    func call(_ type: Wire.Msg, _ payload: Data = Data()) throws -> (UInt8, Data) {
        try send(Wire.frame(type, payload))
        let (kind, length) = try Wire.parseHeader(readExact(Wire.headerLength))
        guard length <= Wire.maxChunk + 64 else { throw UploadError.protocolViolation("reply too large") }
        return (kind, try readExact(length))
    }

    /// The payload of a `type` reply; a REJECT becomes `UploadError.rejected`.
    func expect(_ reply: (UInt8, Data), _ type: Wire.Msg) throws -> Data {
        if reply.0 == Wire.Msg.reject.rawValue { throw UploadError.rejected(reply.1.first ?? 0) }
        guard reply.0 == type.rawValue else { throw UploadError.protocolViolation("expected \(type), got \(reply.0)") }
        return reply.1
    }

    func close() {
        Darwin.close(fd)
    }

    private func send(_ data: Data) throws {
        var sent = 0
        while sent < data.count {
            let n = data.withUnsafeBytes { Darwin.send(fd, $0.baseAddress! + sent, data.count - sent, 0) }
            guard n > 0 else { throw UploadError.unreachable(String(cString: strerror(errno))) }
            sent += n
        }
    }

    private func readExact(_ count: Int) throws -> Data {
        guard count > 0 else { return Data() }
        var out = Data(count: count)
        var got = 0
        while got < count {
            let n = out.withUnsafeMutableBytes { recv(fd, $0.baseAddress! + got, count - got, 0) }
            guard n > 0 else { throw UploadError.unreachable(n == 0 ? "connection closed" : String(cString: strerror(errno))) }
            got += n
        }
        return out
    }

    private static func open(host: String, port: UInt16, timeout: Int) throws -> Int32 {
        var hints = addrinfo()
        hints.ai_family = AF_UNSPEC
        hints.ai_socktype = SOCK_STREAM
        hints.ai_protocol = IPPROTO_TCP
        var result: UnsafeMutablePointer<addrinfo>?
        let rc = getaddrinfo(host, String(port), &hints, &result)
        guard rc == 0, let first = result else { throw UploadError.unreachable(String(cString: gai_strerror(rc))) }
        defer { freeaddrinfo(result) }
        var failure = "no address for \(host)"
        var candidate: UnsafeMutablePointer<addrinfo>? = first
        while let info = candidate {
            let s = socket(info.pointee.ai_family, info.pointee.ai_socktype, info.pointee.ai_protocol)
            if s >= 0 {
                if connect(s, info.pointee.ai_addr, info.pointee.ai_addrlen, timeout) {
                    configure(s, timeout)
                    return s
                }
                failure = String(cString: strerror(errno))
                Darwin.close(s)
            }
            candidate = info.pointee.ai_next
        }
        throw UploadError.unreachable(failure)
    }

    private static func connect(_ s: Int32, _ address: UnsafeMutablePointer<sockaddr>, _ length: socklen_t, _ timeout: Int) -> Bool {
        let flags = fcntl(s, F_GETFL, 0)
        _ = fcntl(s, F_SETFL, flags | O_NONBLOCK)
        defer { _ = fcntl(s, F_SETFL, flags) }
        if Darwin.connect(s, address, length) == 0 { return true }
        guard errno == EINPROGRESS else { return false }
        var poller = pollfd(fd: s, events: Int16(POLLOUT), revents: 0)
        guard poll(&poller, 1, Int32(timeout * 1000)) == 1 else {
            errno = ETIMEDOUT
            return false
        }
        var error: Int32 = 0
        var size = socklen_t(MemoryLayout<Int32>.size)
        getsockopt(s, SOL_SOCKET, SO_ERROR, &error, &size)
        errno = error
        return error == 0
    }

    private static func configure(_ s: Int32, _ timeout: Int) {
        var on: Int32 = 1
        setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &on, socklen_t(MemoryLayout<Int32>.size))
        var limit = timeval(tv_sec: timeout, tv_usec: 0)
        setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &limit, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &limit, socklen_t(MemoryLayout<timeval>.size))
    }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd mac/SecondBrainCapture && SBC_RECEIVER_PYTHON=$PWD/../../omi/firmware/scripts/omi-local/.venv/bin/python swift test`
Expected: `Executed 35 tests, with 0 failures` and `testAgainstRealReceiver` is not skipped (`swift test --filter testAgainstRealReceiver` shows `passed`).

- [ ] **Step 6: Commit**

```bash
git add mac/SecondBrainCapture
git commit -m "mac: add protocol-v2 upload client tested against the receiver"
```

---

### Task 8: UploadQueue

**Files:**
- Create: `PKG/Sources/CaptureCore/UploadQueue.swift`
- Test: `PKG/Tests/CaptureCoreTests/UploadQueueTests.swift`

- [ ] **Step 1: Write the failing tests**

```swift
import XCTest
@testable import CaptureCore

final class FakeTransport: UploadTransport {
    var calls: [String] = []
    var error: Error?

    func openCapture(_ record: CaptureRecord) throws { try note("open \(record.captureID.prefix(2))") }
    func cancelCapture(id: String) throws { try note("cancel \(id.prefix(2))") }
    func upload(_ record: CaptureRecord, file: URL) throws { try note("upload \(record.captureID.prefix(2))") }

    private func note(_ call: String) throws {
        calls.append(call)
        if let error { throw error }
    }
}

final class UploadQueueTests: XCTestCase {
    var dir: URL!
    var spool: Spool!
    let transport = FakeTransport()
    var queue: UploadQueue!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        spool = try Spool(root: dir)
        queue = UploadQueue(spool: spool, transport: transport)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    func record(_ prefix: String, _ start: Int64, _ state: CaptureRecord.State, opened: Bool = false) throws -> CaptureRecord {
        let r = CaptureRecord(captureID: String(repeating: prefix, count: 32), app: "us.zoom.xos", startMs: start,
                              endMs: start + 60_000, state: state, opened: opened)
        try spool.save(r)
        return r
    }

    func testRecordingCaptureIsOpenedOnceAndKept() throws {
        let r = try record("a", 1, .recording)
        XCTAssertEqual(queue.run(now: 0), 0)
        XCTAssertEqual(queue.run(now: 1), 0)
        XCTAssertEqual(transport.calls, ["open aa"])
        XCTAssertEqual(spool.records().first?.opened, true)
        XCTAssertEqual(spool.records().first?.captureID, r.captureID)
    }

    func testCompleteCaptureIsOpenedUploadedAndDeleted() throws {
        _ = try record("b", 1, .complete)
        XCTAssertEqual(queue.run(now: 0), 0)
        XCTAssertEqual(transport.calls, ["open bb", "upload bb"])
        XCTAssertEqual(spool.records(), [])
    }

    func testCancelledCaptureSendsCancelAndIsDeleted() throws {
        _ = try record("c", 1, .cancelled)
        queue.run(now: 0)
        XCTAssertEqual(transport.calls, ["cancel cc"])
        XCTAssertEqual(spool.records(), [])
    }

    func testOldestFirst() throws {
        _ = try record("e", 2, .cancelled)
        _ = try record("d", 1, .cancelled)
        queue.run(now: 0)
        XCTAssertEqual(transport.calls, ["cancel dd", "cancel ee"])
    }

    func testUnreachableBacksOffExponentiallyWithCap() throws {
        _ = try record("f", 1, .complete, opened: true)
        transport.error = UploadError.unreachable("down")
        XCTAssertEqual(queue.run(now: 0), 1)
        XCTAssertEqual(queue.nextAttempt, 30)
        XCTAssertEqual(queue.run(now: 10), 1)
        XCTAssertEqual(transport.calls.count, 1)
        queue.run(now: 30)
        XCTAssertEqual(queue.nextAttempt, 90)
        for step in 0..<10 { queue.run(now: 10_000 * Double(step + 1)) }
        XCTAssertEqual(queue.nextAttempt - 100_000, UploadQueue.maxBackoff)
        XCTAssertNotNil(queue.lastError)
        transport.error = nil
        queue.run(now: 200_000)
        XCTAssertEqual(spool.records(), [])
        XCTAssertNil(queue.lastError)
    }

    func testBusyIsRetryableButProtocolRejectsFailTheCaptureAfterThree() throws {
        _ = try record("g", 1, .complete, opened: true)
        transport.error = UploadError.rejected(Wire.Reject.busy.rawValue)
        queue.run(now: 0)
        XCTAssertEqual(spool.records().first?.uploadFailures, 0)
        transport.error = UploadError.rejected(Wire.Reject.protocolError.rawValue)
        for now in [100.0, 200, 300] { queue.run(now: now) }
        XCTAssertEqual(spool.records().first?.state, .failed)
        let calls = transport.calls.count
        XCTAssertEqual(queue.run(now: 400), 0)
        XCTAssertEqual(transport.calls.count, calls)
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac/SecondBrainCapture && swift test --filter UploadQueueTests`
Expected: compile error `cannot find 'UploadQueue' in scope`.

- [ ] **Step 3: Implement**

`PKG/Sources/CaptureCore/UploadQueue.swift`:

```swift
import Foundation

/// Delivers spooled work to the receiver, oldest first: CAPTURE_OPEN for captures being
/// recorded, file uploads for complete ones, CAPTURE_CANCEL for skipped ones. Network
/// trouble backs off (30 s doubling to 15 min); repeated protocol rejections mark a
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
                var failed = record
                failed.uploadFailures += 1
                if failed.uploadFailures >= Self.maxProtocolFailures {
                    failed.state = .failed
                }
                try? spool.save(failed)
                passError = "\(error)"
            }
        }
        backoff = Self.initialBackoff
        nextAttempt = 0
        lastError = passError
        return pending()
    }

    /// Records the receiver still needs something for (an open recording counts once opened).
    public func pending() -> Int {
        spool.records().filter { $0.state != .failed && !($0.state == .recording && $0.opened) }.count
    }

    private func deliver(_ record: CaptureRecord) throws {
        var r = record
        switch r.state {
        case .recording, .complete:
            if !r.opened {
                try transport.openCapture(r)
                r.opened = true
                try spool.save(r)
            }
            if r.state == .complete {
                try transport.upload(r, file: spool.cafURL(r.captureID))
                spool.delete(r.captureID)
            }
        case .cancelled:
            try transport.cancelCapture(id: r.captureID)
            spool.delete(r.captureID)
        case .failed:
            break
        }
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac/SecondBrainCapture && swift test`
Expected: `Executed 41 tests, with 0 failures` (1 skipped without `SBC_RECEIVER_PYTHON`).

- [ ] **Step 5: Commit**

```bash
git add mac/SecondBrainCapture
git commit -m "mac: add retrying upload queue"
```

---

### Task 9: Core Audio layer, probe, and app bundle (early permission check)

This task has no unit tests: Core Audio taps need real hardware and user permission. It ends with a manual check on this (managed) Mac, done before building the rest of the app, because device management could block an ad-hoc-signed app from getting System Audio permission.

**Files:**
- Create: `PKG/Sources/SecondBrainCapture/AudioProcesses.swift`, `PKG/Sources/SecondBrainCapture/TapSource.swift`, `PKG/Sources/SecondBrainCapture/MicSource.swift`, `PKG/Sources/SecondBrainCapture/Probe.swift`, `PKG/Resources/Info.plist`, `PKG/scripts/build-app.sh`
- Modify: `PKG/Sources/SecondBrainCapture/main.swift`

- [ ] **Step 1: Core Audio queries**

`PKG/Sources/SecondBrainCapture/AudioProcesses.swift`:

```swift
import CaptureCore
import CoreAudio

struct CoreAudioError: Error, CustomStringConvertible {
    let what: String
    let status: OSStatus
    var description: String { "\(what) failed (OSStatus \(status))" }
}

func check(_ what: String, _ status: OSStatus) throws {
    guard status == noErr else { throw CoreAudioError(what: what, status: status) }
}

/// Read-only Core Audio process and device queries.
enum AudioProcesses {
    static func address(_ selector: AudioObjectPropertySelector,
                        _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    static func objectIDs(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                          _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> [AudioObjectID] {
        var addr = address(selector, scope)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &ids) == noErr else { return [] }
        return ids
    }

    static func uint32(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
        var addr = address(selector)
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr ? value : nil
    }

    static func string(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
        var addr = address(selector)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr, let value else { return nil }
        return value.takeRetainedValue() as String
    }

    /// Every audio process object with a bundle ID.
    static func snapshot() -> [AudioProcess] {
        objectIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList).compactMap { id in
            guard let bundle = string(id, kAudioProcessPropertyBundleID), !bundle.isEmpty else { return nil }
            return AudioProcess(objectID: id, bundleID: bundle,
                                isRunningInput: uint32(id, kAudioProcessPropertyIsRunningInput) == 1,
                                inputDevices: objectIDs(id, kAudioProcessPropertyDevices, kAudioObjectPropertyScopeInput))
        }
    }

    static func defaultOutputUID() throws -> String {
        guard let device = uint32(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultSystemOutputDevice),
              let uid = string(device, kAudioDevicePropertyDeviceUID) else {
            throw CoreAudioError(what: "default output device lookup", status: -1)
        }
        return uid
    }
}
```

- [ ] **Step 2: Process tap source**

`PKG/Sources/SecondBrainCapture/TapSource.swift`:

```swift
import AVFoundation
import CoreAudio

/// Captures the mixed output of specific processes through a Core Audio process tap
/// attached to a private aggregate device. Requires "System Audio Recording Only".
final class TapSource {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private(set) var format: AVAudioFormat?

    /// `onBuffer` runs on `queue`; the buffer is only valid during the call.
    func start(processes: [AudioObjectID], queue: DispatchQueue, onBuffer: @escaping (AVAudioPCMBuffer) -> Void) throws {
        let description = CATapDescription(stereoMixdownOfProcesses: processes)
        description.uuid = UUID()
        description.muteBehavior = .unmuted
        description.isPrivate = true
        description.name = "SecondBrainCapture"
        try check("create process tap", AudioHardwareCreateProcessTap(description, &tapID))

        var addr = AudioProcesses.address(kAudioTapPropertyFormat)
        var stream = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        try check("read tap format", AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &stream))
        guard let format = AVAudioFormat(streamDescription: &stream) else {
            throw CoreAudioError(what: "tap format", status: -1)
        }
        self.format = format

        let outputUID = try AudioProcesses.defaultOutputUID()
        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "SecondBrainCapture tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: description.uuid.uuidString]],
        ]
        try check("create aggregate device", AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID))
        try check("create IO proc", AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, queue) { _, input, _, _, _ in
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: input, deallocator: nil) else { return }
            onBuffer(buffer)
        })
        try check("start aggregate device", AudioDeviceStart(aggregateID, procID))
    }

    func stop() {
        if aggregateID != AudioObjectID(kAudioObjectUnknown) {
            if let procID {
                AudioDeviceStop(aggregateID, procID)
                AudioDeviceDestroyIOProcID(aggregateID, procID)
            }
            AudioHardwareDestroyAggregateDevice(aggregateID)
        }
        if tapID != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyProcessTap(tapID)
        }
        tapID = AudioObjectID(kAudioObjectUnknown)
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        procID = nil
    }

    deinit {
        stop()
    }
}
```

- [ ] **Step 3: Mic source**

`PKG/Sources/SecondBrainCapture/MicSource.swift`:

```swift
import AVFoundation
import CoreAudio

/// Captures one input device (default input if nil) with AVAudioEngine.
final class MicSource {
    private let engine = AVAudioEngine()

    /// `onBuffer` runs on the engine's audio thread.
    func start(device: AudioDeviceID?, onBuffer: @escaping (AVAudioPCMBuffer) -> Void) throws {
        let input = engine.inputNode
        if var device, let unit = input.audioUnit {
            try check("select input device", AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice,
                                                                  kAudioUnitScope_Global, 0, &device,
                                                                  UInt32(MemoryLayout<AudioDeviceID>.size)))
        }
        input.installTap(onBus: 0, bufferSize: 4096, format: input.outputFormat(forBus: 0)) { buffer, _ in
            onBuffer(buffer)
        }
        engine.prepare()
        try engine.start()
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }
}
```

- [ ] **Step 4: Probe and temporary entry point**

`PKG/Sources/SecondBrainCapture/Probe.swift`:

```swift
import CaptureCore
import Foundation

/// `--probe` lists audio processes; `--probe-tap <bundle-id> <seconds>` taps that app and
/// reports what arrived. Output also goes to probe.txt in the app's support directory,
/// because a launched `.app` has no terminal.
enum Probe {
    static func run(_ arguments: [String]) {
        var lines: [String] = []
        let snapshot = AudioProcesses.snapshot()
        for p in snapshot {
            lines.append("\(p.isRunningInput ? "MIC" : "   ") \(p.objectID) \(p.bundleID) inputs=\(p.inputDevices)")
        }
        if let i = arguments.firstIndex(of: "--probe-tap"), arguments.count > i + 2, let seconds = Double(arguments[i + 2]) {
            lines.append(tap(family: arguments[i + 1], seconds: seconds, snapshot: snapshot))
        }
        let text = lines.joined(separator: "\n") + "\n"
        print(text, terminator: "")
        try? FileManager.default.createDirectory(at: Config.directory, withIntermediateDirectories: true)
        try? text.write(to: Config.directory.appendingPathComponent("probe.txt"), atomically: true, encoding: .utf8)
    }

    private static func tap(family: String, seconds: Double, snapshot: [AudioProcess]) -> String {
        let ids = snapshot.filter { AppMatcher(allowlist: [family]).family(of: $0.bundleID) != nil }.map(\.objectID)
        guard !ids.isEmpty else { return "no audio process for \(family)" }
        let queue = DispatchQueue(label: "probe.tap")
        let source = TapSource()
        var frames = 0
        var peak: Float = 0
        do {
            try source.start(processes: ids, queue: queue) { buffer in
                frames += Int(buffer.frameLength)
                if let data = buffer.floatChannelData?[0] {
                    for k in 0..<Int(buffer.frameLength) * buffer.stride {
                        peak = max(peak, abs(data[k]))
                    }
                }
            }
        } catch {
            return "tap failed: \(error)"
        }
        Thread.sleep(forTimeInterval: seconds)
        source.stop()
        return queue.sync { "tap \(family): frames=\(frames) peak=\(peak) format=\(source.format.map { "\($0)" } ?? "-")" }
    }
}
```

Replace `PKG/Sources/SecondBrainCapture/main.swift` with:

```swift
import Foundation

if CommandLine.arguments.contains("--probe") {
    Probe.run(CommandLine.arguments)
    exit(0)
}
print("SecondBrainCapture: run with --probe [--probe-tap <bundle-id> <seconds>]")
```

- [ ] **Step 5: Bundle metadata and build script**

`PKG/Resources/Info.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.partnerinflight.SecondBrainCapture</string>
    <key>CFBundleName</key>
    <string>SecondBrainCapture</string>
    <key>CFBundleExecutable</key>
    <string>SecondBrainCapture</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.4</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Records your side of allowlisted meetings for your Second Brain.</string>
    <key>NSAudioCaptureUsageDescription</key>
    <string>Records the other participants in allowlisted meeting apps for your Second Brain.</string>
</dict>
</plist>
```

`PKG/scripts/build-app.sh`:

```bash
#!/usr/bin/env bash
# Build SecondBrainCapture.app (ad-hoc signed) into mac/SecondBrainCapture/build/.
set -euo pipefail
PKG="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PKG"
swift build -c release
BIN="$(swift build -c release --show-bin-path)/SecondBrainCapture"
APP="$PKG/build/SecondBrainCapture.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN" "$APP/Contents/MacOS/SecondBrainCapture"
cp "$PKG/Resources/Info.plist" "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
codesign --verify "$APP"
echo "$APP"
```

Run: `chmod +x mac/SecondBrainCapture/scripts/build-app.sh`

- [ ] **Step 6: Build and verify**

Run: `cd mac/SecondBrainCapture && swift build && swift test && scripts/build-app.sh`
Expected: build succeeds, tests pass, last line is the `.app` path.

- [ ] **Step 7: Manual permission check on this Mac (human step)**

Ask the user to do this and report the output; do not proceed to Task 10 until they have:

1. Start a Zoom/Chime test call (or any allowlisted app playing audio and using the mic).
2. `open -W mac/SecondBrainCapture/build/SecondBrainCapture.app --args --probe --probe-tap us.zoom.xos 10`
   (use the bundle ID of the app in the call). Approve the System Audio Recording prompt if shown.
3. `cat ~/Library/Application\ Support/SecondBrainCapture/probe.txt`

Expected: the meeting app appears with `MIC` while its mic is live, and the tap line shows `frames>0` and `peak>0` while the other side is talking. `tap failed` (e.g. OSStatus from `create process tap`) or `peak=0.0` with audible remote audio means permission was denied or blocked by device management — stop and report to the user; the rest of the app cannot work until that is resolved.

- [ ] **Step 8: Commit**

```bash
git add mac/SecondBrainCapture
git commit -m "mac: add Core Audio tap/mic sources, probe and app bundle"
```

---

### Task 10: Capture session, controller, menu bar and app entry

**Files:**
- Create: `PKG/Sources/SecondBrainCapture/CaptureSession.swift`, `PKG/Sources/SecondBrainCapture/CaptureController.swift`, `PKG/Sources/SecondBrainCapture/StatusMenu.swift`
- Modify: `PKG/Sources/SecondBrainCapture/main.swift`

- [ ] **Step 1: Capture session**

`PKG/Sources/SecondBrainCapture/CaptureSession.swift`:

```swift
import AVFoundation
import CaptureCore
import CoreAudio

/// One capture: tap (right) + mic (left) → 16 kHz resamplers → stereo assembler → PCM spool file.
/// Assembly and writing happen on `queue`; each resampler is used only by its own source thread.
final class CaptureSession {
    let record: CaptureRecord
    private let queue = DispatchQueue(label: "capture.session")
    private let tap = TapSource()
    private let mic = MicSource()
    private let assembler = StereoAssembler()
    private let writer: PCMWriter
    private var tapResampler: Resampler?
    private var micResampler: Resampler?
    private var writeError: Error?

    init(record: CaptureRecord, spool: Spool) throws {
        self.record = record
        writer = try PCMWriter(url: spool.pcmURL(record.captureID))
    }

    func start(processes: [AudioObjectID], inputDevice: AudioDeviceID?) throws {
        try tap.start(processes: processes, queue: queue) { [weak self] buffer in
            guard let self else { return }
            if self.tapResampler == nil { self.tapResampler = Resampler(from: buffer.format) }
            self.assembler.appendRight(self.tapResampler?.convert(buffer) ?? [])
            self.drain()
        }
        do {
            try mic.start(device: inputDevice) { [weak self] buffer in
                guard let self else { return }
                if self.micResampler == nil { self.micResampler = Resampler(from: buffer.format) }
                let samples = self.micResampler?.convert(buffer) ?? []
                self.queue.async {
                    self.assembler.appendLeft(samples)
                    self.drain()
                }
            }
        } catch {
            tap.stop()
            throw error
        }
    }

    /// Stop both sources, write what remains and close the file. Returns frames written.
    func finish() throws -> Int64 {
        mic.stop()
        tap.stop()
        return try queue.sync {
            try writer.write(assembler.flush(), now: ProcessInfo.processInfo.systemUptime)
            try writer.close()
            if let writeError { throw writeError }
            return writer.framesWritten
        }
    }

    private func drain() {
        do {
            try writer.write(assembler.drain(), now: ProcessInfo.processInfo.systemUptime)
        } catch {
            writeError = error
        }
    }
}
```

- [ ] **Step 2: Controller**

`PKG/Sources/SecondBrainCapture/CaptureController.swift`:

```swift
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
        if discard || Double(frames) / 16000 < config.minCaptureSeconds {
            cancel(record)
        } else {
            try? spool.save(record)
            work.async { self.finalize(record) }
        }
        kickUploads()
    }

    private func cancel(_ record: CaptureRecord) {
        var cancelled = record
        cancelled.state = .cancelled
        spool.deleteAudio(record.captureID)
        try? spool.save(cancelled)
    }

    /// On `work`: encode the PCM, then mark the capture complete and try to upload.
    private func finalize(_ record: CaptureRecord) {
        var complete = record
        do {
            try CaptureEncoder.encode(pcm: spool.pcmURL(record.captureID), to: spool.cafURL(record.captureID))
            try? FileManager.default.removeItem(at: spool.pcmURL(record.captureID))
            complete.state = .complete
            try spool.save(complete)
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
        for var record in spool.records() where record.state == .recording {
            let pcm = spool.pcmURL(record.captureID)
            if !FileManager.default.fileExists(atPath: pcm.path),
               FileManager.default.fileExists(atPath: spool.cafURL(record.captureID).path) {
                record.state = .complete  // encoded, but the state change was lost
                try? spool.save(record)
                continue
            }
            let frames = (try? PCMWriter.truncateToWholeFrames(pcm)) ?? 0
            record.endMs = record.startMs + frames * 1000 / 16000
            if Double(frames) / 16000 < config.minCaptureSeconds {
                cancel(record)
            } else {
                try? spool.save(record)
                work.async { self.finalize(record) }
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
```

- [ ] **Step 3: Menu bar**

`PKG/Sources/SecondBrainCapture/StatusMenu.swift`:

```swift
import AppKit
import CaptureCore

/// Menu-bar icon: idle, recording (red), uploads pending, or error.
final class StatusMenu: NSObject {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let controller: CaptureController

    init(controller: CaptureController) {
        self.controller = controller
        super.init()
        controller.onChange = { [weak self] in self?.refresh() }
        refresh()
    }

    func refresh() {
        let error = controller.captureError ?? controller.uploadError
        let symbol: String
        let title: String
        if let app = controller.recordingApp {
            symbol = "record.circle.fill"
            title = "Recording \(app)"
        } else if error != nil || !controller.micAuthorized {
            symbol = "exclamationmark.triangle"
            title = "Not recording"
        } else if controller.pendingUploads > 0 {
            symbol = "arrow.up.circle"
            title = "Idle"
        } else {
            symbol = "waveform"
            title = controller.isPaused ? "Paused" : "Idle"
        }
        item.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
        item.button?.contentTintColor = controller.recordingApp == nil ? nil : .systemRed

        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: title, action: nil, keyEquivalent: ""))
        if !controller.micAuthorized {
            menu.addItem(NSMenuItem(title: "Microphone permission missing", action: nil, keyEquivalent: ""))
        }
        if controller.pendingUploads > 0 {
            menu.addItem(NSMenuItem(title: "\(controller.pendingUploads) uploads pending", action: nil, keyEquivalent: ""))
        }
        if controller.failedCaptures > 0 {
            menu.addItem(NSMenuItem(title: "\(controller.failedCaptures) captures failed to upload", action: nil, keyEquivalent: ""))
        }
        if controller.spoolOverLimit {
            menu.addItem(NSMenuItem(title: "Spool is over the size limit", action: nil, keyEquivalent: ""))
        }
        if let error {
            menu.addItem(NSMenuItem(title: String(error.prefix(120)), action: nil, keyEquivalent: ""))
        }
        menu.addItem(.separator())
        if controller.recordingApp != nil {
            add(menu, "Skip this meeting", #selector(skip))
        }
        if controller.isPaused {
            add(menu, "Resume capture", #selector(resume))
        } else {
            add(menu, "Pause for 1 hour", #selector(pause))
        }
        add(menu, "Open spool folder", #selector(openSpool))
        menu.addItem(.separator())
        add(menu, "Quit", #selector(quit))
        item.menu = menu
    }

    private func add(_ menu: NSMenu, _ title: String, _ action: Selector) {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
        entry.target = self
        menu.addItem(entry)
    }

    @objc private func skip() { controller.skipMeeting() }
    @objc private func pause() { controller.pauseForAnHour() }
    @objc private func resume() { controller.resume() }
    @objc private func openSpool() { NSWorkspace.shared.open(controller.spool.root) }
    @objc private func quit() { NSApp.terminate(nil) }
}
```

- [ ] **Step 4: App entry**

Replace `PKG/Sources/SecondBrainCapture/main.swift` with:

```swift
import AppKit
import AVFoundation
import ServiceManagement
import UserNotifications

if CommandLine.arguments.contains("--probe") {
    Probe.run(CommandLine.arguments)
    exit(0)
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: CaptureController?
    private var menu: StatusMenu?

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let controller = try CaptureController()
            self.controller = controller
            menu = StatusMenu(controller: controller)
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { _, _ in }
            try? SMAppService.mainApp.register()  // start at login
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async {
                    controller.micAuthorized = granted
                    self.menu?.refresh()
                    controller.start()
                }
            }
        } catch {
            let alert = NSAlert()
            alert.messageText = "SecondBrainCapture could not start"
            alert.informativeText = "\(error)"
            alert.runModal()
            NSApp.terminate(nil)
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
```

- [ ] **Step 5: Build and test**

Run: `cd mac/SecondBrainCapture && swift build && swift test && scripts/build-app.sh`
Expected: build succeeds with no errors, all unit tests pass, `.app` path printed.

- [ ] **Step 6: Commit**

```bash
git add mac/SecondBrainCapture
git commit -m "mac: record meetings from the menu bar and upload them"
```

---

### Task 11: Test runner, documentation and end-to-end check

**Files:**
- Modify: `scripts/test.py`
- Create: `PKG/README.md`
- Modify: `SYSTEM.md`, `src/second_brain/ARCHITECTURE.md`

- [ ] **Step 1: Run the Swift suite from the shared test entry point on macOS**

In `scripts/test.py`, add `import shutil` to the imports and append after the existing loop:

```python
mac = root / "mac/SecondBrainCapture"
if sys.platform == "darwin" and shutil.which("swift"):
    # The integration test starts a loopback receiver with this interpreter.
    subprocess.run(["swift", "test"], cwd=mac, env={**env, "SBC_RECEIVER_PYTHON": sys.executable}, check=True)
```

Run: `omi/firmware/scripts/omi-local/.venv/bin/python scripts/test.py 2>&1 | grep -E '^Ran|^OK|Executed|FAILED|error:'`
Expected: both Python blocks `OK`, then `Executed N tests, with 0 failures` including the non-skipped integration test.

- [ ] **Step 2: Write `PKG/README.md`**

```markdown
# SecondBrainCapture (macOS)

Menu-bar app that records allowlisted meeting apps while they use the microphone —
your mic on the left channel, the app's audio (other participants) on the right — and
uploads each meeting to the Second Brain receiver (protocol v2) as stereo Opus.
Needs macOS 14.4+ and Xcode (for `swift`).

## Build and install

    cd mac/SecondBrainCapture
    swift test
    scripts/build-app.sh
    ditto build/SecondBrainCapture.app ~/Applications/SecondBrainCapture.app
    open ~/Applications/SecondBrainCapture.app

On first launch approve **Microphone** and, at the first capture, **System Audio
Recording Only**. The app registers itself as a login item (System Settings → General →
Login Items). To update, quit it from the menu, rebuild and repeat the `ditto`/`open`.

## Configure

`~/Library/Application Support/SecondBrainCapture/config.json` is created on first run:

    {"host": "192.168.1.85", "port": 7331}

Other keys (defaults): `allowlist` (Chime, Zoom, Teams, Slack, Webex bundle IDs; browsers
are not included because a tap records every tab), `secretPath`
(`~/.omi-local/upload-secret.hex`), `minCaptureSeconds` (60), `releaseGraceSeconds` (20),
`spoolWarnBytes` (5 GB). Restart the app after editing.

Copy the receiver's pairing secret to this Mac: the same 64-hex-character file the Omi
uses (`upload-secret.hex` from `%USERPROFILE%\.omi-local\` or the service's configured
secret file), saved as `~/.omi-local/upload-secret.hex` with `chmod 600`.

The Windows service must run a build that includes receiver protocol v2
(`windows/install.ps1` from a checkout containing it); older receivers reject the app.

## Behavior

- Starts when an allowlisted app (or its helper process) holds the mic; stops 20 s after
  it lets go. Captures under 60 s are discarded. If the Mac sleeps mid-meeting, the
  capture ends at sleep and a new one starts after wake, so each file's time span is real.
- Spool: `~/Library/Application Support/SecondBrainCapture/spool/` (0700). Audio is written
  as raw PCM with an fsync every 5 s, encoded to Opus when the meeting ends, and deleted only
  after the receiver confirms a verified commit. Captures interrupted by a crash or sleep are
  finished on the next launch.
- Uploads retry with backoff (30 s → 15 min) while the receiver is unreachable.
- Menu: Skip this meeting (discard it and tell the receiver), Pause for 1 hour / Resume,
  Open spool folder.

## Diagnose

    open -W build/SecondBrainCapture.app --args --probe --probe-tap us.zoom.xos 10
    cat ~/Library/Application\ Support/SecondBrainCapture/probe.txt

Lists audio processes (`MIC` = currently using input) and taps the given app for 10 s.
`peak=0.0` while the other side is audibly talking means System Audio permission is missing.
macOS gives no API to check that permission directly, so a silent capture cannot be told
apart from a silent meeting.
```

- [ ] **Step 3: Update `SYSTEM.md` and `ARCHITECTURE.md`**

In `SYSTEM.md`, find the numbered list under "Receiver, queue, and crash boundaries" (item 6 describes protocol v2) and append to item 6:

```markdown
   The client is `mac/SecondBrainCapture` (menu-bar app; see its README). It opens a
   short connection per operation, sends CAPTURE_OPEN when a meeting starts, and uploads
   stereo Opus CAF (L = owner mic, R = meeting app) after the meeting ends.
```

In `src/second_brain/ARCHITECTURE.md`, in the paragraph that begins "Protocol-v2 file clients (the Mac meeting-capture app)", append: `The client lives in \`mac/SecondBrainCapture\`.`

- [ ] **Step 4: End-to-end check with the real receiver (human-assisted)**

With the Windows service running a v2-capable build, config and secret in place, and the app installed:
1. Join a test call in an allowlisted app for at least 90 s, with someone (or a video) talking on the other side and you talking too.
2. Leave the call; within ~30 s the menu shows the upload pending, then idle.
3. On the Windows machine, confirm `incoming\meetings\<id>.caf` + `<id>.json` exist and `ffprobe` reports 2 channels of Opus with the expected duration; `.captures\<id>.json` is `closed`.
4. Start another call and choose "Skip this meeting": nothing is uploaded, and its `.captures` marker becomes `cancelled`.

Record the results (date, app used, durations) in `docs/validation.md` under a new "Mac meeting capture" heading.

- [ ] **Step 5: Commit**

```bash
git add scripts/test.py mac/SecondBrainCapture/README.md SYSTEM.md src/second_brain/ARCHITECTURE.md docs/validation.md
git commit -m "docs: document and test the Mac meeting-capture app"
```
