/// Whether an AVAudioEngine configuration-change notification means the mic was really
/// disrupted. The engine posts one ~100 ms after an input device is selected even though it
/// keeps running with the same format; only a stopped engine or a new format ends a capture.
public enum AudioRoute {
    public struct InputFormat: Equatable {
        public let sampleRate: Double
        public let channels: UInt32

        public init(sampleRate: Double, channels: UInt32) {
            self.sampleRate = sampleRate
            self.channels = channels
        }
    }

    public static func isDisruptive(engineRunning: Bool, before: InputFormat, after: InputFormat) -> Bool {
        !engineRunning || before != after
    }
}
