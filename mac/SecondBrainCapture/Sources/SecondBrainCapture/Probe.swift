import AVFoundation
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
        for (i, arg) in arguments.enumerated() where arg == "--probe-connect" && arguments.count > i + 2 {
            let start = Date()
            let client = UploadClient(host: arguments[i + 1], port: UInt16(arguments[i + 2]) ?? 0, secret: Data(count: 32),
                                      clientID: Data(count: 6), timeoutSeconds: 5)
            let outcome: String
            do { try client.cancelCapture(id: String(repeating: "0", count: 32)); outcome = "connected (unexpected auth success)" }
            catch { outcome = "\(error)" }
            lines.append(String(format: "connect %@:%@ after %.2fs: ", arguments[i + 1], arguments[i + 2], Date().timeIntervalSince(start)) + outcome)
        }
        if let i = arguments.firstIndex(of: "--probe-mic"), arguments.count > i + 1, let seconds = Double(arguments[i + 1]) {
            let device = arguments.count > i + 2 ? UInt32(arguments[i + 2]) : nil
            lines.append(contentsOf: mic(seconds: seconds, device: device, withTap: arguments.contains("--with-tap") ? snapshot : nil))
        }
        let text = lines.joined(separator: "\n") + "\n"
        print(text, terminator: "")
        try? FileManager.default.createDirectory(at: Config.directory, withIntermediateDirectories: true)
        try? text.write(to: Config.directory.appendingPathComponent("probe.txt"), atomically: true, encoding: .utf8)
    }

    /// Diagnostic: run the mic engine alone (or with a Zoom tap) and log every configuration change.
    private static func mic(seconds: Double, device: UInt32?, withTap snapshot: [AudioProcess]?) -> [String] {
        var out: [String] = []
        let start = Date()
        func stamp(_ text: String) { out.append(String(format: "%6.3fs ", Date().timeIntervalSince(start)) + text) }
        let queue = DispatchQueue(label: "probe.mic.tap")
        let tapSource = TapSource()
        if let snapshot {
            let ids = snapshot.filter { AppMatcher(allowlist: ["us.zoom.xos"]).family(of: $0.bundleID) != nil }.map(\.objectID)
            do { try tapSource.start(processes: ids, queue: queue) { _ in }; stamp("tap started on \(ids)") }
            catch { stamp("tap failed: \(error)") }
        }
        let source = MicSource()
        var buffers = 0
        let observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
                                                              object: source.engine, queue: nil) { _ in
            let f = source.engine.inputNode.inputFormat(forBus: 0)
            stamp("CONFIG CHANGE running=\(source.engine.isRunning) hw input now \(f.sampleRate) Hz \(f.channelCount) ch")
        }
        do {
            let before = source.engine.inputNode.inputFormat(forBus: 0)
            stamp("hw input before start \(before.sampleRate) Hz \(before.channelCount) ch, device=\(device.map { "\($0)" } ?? "default")")
            try source.start(device: device) { _ in buffers += 1 }
            let after = source.engine.inputNode.inputFormat(forBus: 0)
            stamp("started; hw input \(after.sampleRate) Hz \(after.channelCount) ch")
        } catch {
            stamp("mic start failed: \(error)")
        }
        Thread.sleep(forTimeInterval: seconds)
        stamp("buffers=\(buffers) running=\(source.engine.isRunning)")
        source.stop()
        tapSource.stop()
        NotificationCenter.default.removeObserver(observer)
        return out
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
