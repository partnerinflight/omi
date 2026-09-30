import AVFoundation

/// Converts buffers of one fixed input format to 16 kHz mono Float32 (downmixing).
/// Not thread-safe: use one instance per source, from that source's thread.
public final class Resampler {
    public static let outputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000,
                                                   channels: 1, interleaved: false)!
    private var converter: AVAudioConverter
    private var inputFormat: AVAudioFormat

    public init?(from input: AVAudioFormat) {
        guard let converter = Self.makeConverter(for: input) else { return nil }
        self.converter = converter
        inputFormat = input
    }

    /// AVAudioConverter's downmix only folds the first two channels of wider layouts, so inputs
    /// with more than two channels are averaged to mono manually and the converter takes mono.
    private static func makeConverter(for input: AVAudioFormat) -> AVAudioConverter? {
        guard let source = input.channelCount > 2 ? monoFormat(rate: input.sampleRate) : input,
              let converter = AVAudioConverter(from: source, to: outputFormat) else { return nil }
        converter.downmix = true
        return converter
    }

    private static func sameShape(_ a: AVAudioFormat, _ b: AVAudioFormat) -> Bool {
        a.sampleRate == b.sampleRate && a.channelCount == b.channelCount
            && a.isInterleaved == b.isInterleaved && a.commonFormat == b.commonFormat
    }

    private static func monoFormat(rate: Double) -> AVAudioFormat? {
        AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false)
    }

    /// Handles planar and interleaved Float32 only; non-Float32 input with more than two channels
    /// yields nil (so `convert` returns []). Taps and mics deliver Float32.
    private static func averageToMono(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let format = monoFormat(rate: buffer.format.sampleRate),
              let mono = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: buffer.frameLength),
              let src = buffer.floatChannelData, let dst = mono.floatChannelData?[0] else { return nil }
        let frames = Int(buffer.frameLength)
        let channels = Int(buffer.format.channelCount)
        mono.frameLength = buffer.frameLength
        if buffer.format.isInterleaved {
            for i in 0..<frames {
                var sum: Float = 0
                for c in 0..<channels { sum += src[0][i * channels + c] }
                dst[i] = sum / Float(channels)
            }
        } else {
            for i in 0..<frames {
                var sum: Float = 0
                for c in 0..<channels { sum += src[c][i] }
                dst[i] = sum / Float(channels)
            }
        }
        return mono
    }

    public func convert(_ original: AVAudioPCMBuffer) -> [Float] {
        var buffer = original
        if !Self.sameShape(buffer.format, inputFormat) {
            guard let rebuilt = Self.makeConverter(for: buffer.format) else { return [] }
            converter = rebuilt
            inputFormat = buffer.format
        }
        if buffer.format.channelCount > 2 {
            guard let mono = Self.averageToMono(buffer) else { return [] }
            buffer = mono
        }
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
