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
        XCTAssertEqual(out.count, 20)
        for i in 0..<10 {
            XCTAssertEqual(out[2 * i], 16384)
            XCTAssertEqual(out[2 * i + 1], 0)
        }
        a.appendLeft([Float](repeating: 1, count: 3))
        a.appendRight([Float](repeating: -1, count: 3))
        XCTAssertEqual(a.drain(), [Int16]([32767, -32767, 32767, -32767, 32767, -32767]))
    }

    func testNaNBecomesSilence() {
        let a = StereoAssembler()
        a.appendLeft([.nan])
        a.appendRight([.nan])
        XCTAssertEqual(a.drain(), [0, 0])
    }

    func testResamplerFollowsFormatChange() throws {
        let f1 = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2))
        let f2 = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 44100, channels: 1))
        let resampler = try XCTUnwrap(Resampler(from: f1))
        let b1 = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: f1, frameCapacity: 4800))
        b1.frameLength = 4800
        _ = resampler.convert(b1)
        let b2 = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: f2, frameCapacity: 4410))
        b2.frameLength = 4410
        for i in 0..<4410 { b2.floatChannelData![0][i] = 0.5 * sin(Float(i) * 2 * .pi * 440 / 44100) }
        let out = resampler.convert(b2)
        // A fresh converter swallows ~120 frames of resampler priming, hence the wider tolerance.
        XCTAssertEqual(Double(out.count), 1600, accuracy: 150)
        XCTAssertGreaterThan(out.map(abs).max() ?? 0, 0.1)
    }

    func testResamplerDownmixesSixChannelsIncludingLast() throws {
        let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 6)
            ?? AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, interleaved: false,
                             channelLayout: AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | 6)!)
        let input = try XCTUnwrap(format)
        let resampler = try XCTUnwrap(Resampler(from: input))
        var peak: Float = 0
        for chunk in 0..<5 {
            let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: input, frameCapacity: 4800))
            buffer.frameLength = 4800
            for c in 0..<6 { for i in 0..<4800 { buffer.floatChannelData![c][i] = 0 } }
            for i in 0..<4800 {
                buffer.floatChannelData![5][i] = 0.5 * sin(Float(chunk * 4800 + i) * 2 * .pi * 440 / 48000)
            }
            peak = max(peak, resampler.convert(buffer).map(abs).max() ?? 0)
        }
        XCTAssertGreaterThan(peak, 0.05)
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

    func testResamplerDownmixesInterleavedSixChannels() throws {
        let input = try XCTUnwrap(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, interleaved: true,
                                                channelLayout: AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | 6)!))
        let resampler = try XCTUnwrap(Resampler(from: input))
        var peak: Float = 0
        for chunk in 0..<5 {
            let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: input, frameCapacity: 4800))
            buffer.frameLength = 4800
            let data = try XCTUnwrap(buffer.floatChannelData)[0]
            for i in 0..<(4800 * 6) { data[i] = 0 }
            for i in 0..<4800 {
                data[i * 6 + 5] = 0.5 * sin(Float(chunk * 4800 + i) * 2 * .pi * 440 / 48000)
            }
            peak = max(peak, resampler.convert(buffer).map(abs).max() ?? 0)
        }
        XCTAssertGreaterThan(peak, 0.05)
    }

    func testEqualButDistinctFormatsDoNotRebuildConverter() throws {
        let formats = try (0..<2).map { _ in try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2)) }
        let resampler = try XCTUnwrap(Resampler(from: formats[0]))
        var total = 0
        for chunk in 0..<10 {
            let format = formats[chunk % 2]
            let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4800))
            buffer.frameLength = 4800
            for c in 0..<2 {
                for i in 0..<4800 {
                    buffer.floatChannelData![c][i] = 0.5 * sin(Float(chunk * 4800 + i) * 2 * .pi * 440 / 48000)
                }
            }
            total += resampler.convert(buffer).count
        }
        XCTAssertEqual(Double(total), 16000, accuracy: 320)
    }
}
