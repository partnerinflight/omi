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
