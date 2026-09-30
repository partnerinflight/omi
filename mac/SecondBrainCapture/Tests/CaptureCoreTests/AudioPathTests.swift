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
