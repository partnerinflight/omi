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
