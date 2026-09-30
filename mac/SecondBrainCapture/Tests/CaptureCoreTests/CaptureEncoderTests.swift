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
        XCTAssertLessThan(cafSize, pcmSize / 10)
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.contains("tmp") }
        XCTAssertEqual(leftovers, [])
    }

    // MARK: helpers

    private func makeDir() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        return dir
    }

    private func writeTone(frames: Int, to url: URL) throws {
        var samples = [Int16](repeating: 0, count: 2 * frames)
        for i in 0..<frames {
            samples[2 * i] = Int16(10000 * sin(Double(i) * 2 * .pi * 440 / 16000))
            samples[2 * i + 1] = samples[2 * i]
        }
        let writer = try PCMWriter(url: url)
        try writer.write(samples, now: 0)
        try writer.close()
    }

    /// RMS of channel 0 over `count` frames starting at `start`.
    private func rms(_ file: AVAudioFile, start: AVAudioFramePosition, count: AVAudioFrameCount) throws -> Double {
        file.framePosition = start
        let buf = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: count))
        try file.read(into: buf, frameCount: count)
        XCTAssertEqual(buf.frameLength, count)
        let ch = try XCTUnwrap(buf.floatChannelData)[0]
        var sum = 0.0
        for i in 0..<Int(buf.frameLength) { sum += Double(ch[i]) * Double(ch[i]) }
        return (sum / Double(max(1, buf.frameLength))).squareRoot()
    }

    // MARK: tests

    func testOddLengthKeepsTail() throws {
        let dir = try makeDir()
        let pcm = dir.appendingPathComponent("a.pcm"), caf = dir.appendingPathComponent("a.caf")
        try writeTone(frames: 48123, to: pcm)
        try CaptureEncoder.encode(pcm: pcm, to: caf)
        let file = try AVAudioFile(forReading: caf)
        XCTAssertEqual(Double(file.length), 48123, accuracy: 40)
        XCTAssertGreaterThan(try rms(file, start: file.length - 1600, count: 1600), 0.01)
    }

    func testLongCaptureKeepsDuration() throws {
        let dir = try makeDir()
        let pcm = dir.appendingPathComponent("a.pcm"), caf = dir.appendingPathComponent("a.caf")
        let frames = 35 * 16000 + 77
        try writeTone(frames: frames, to: pcm)
        try CaptureEncoder.encode(pcm: pcm, to: caf)
        let file = try AVAudioFile(forReading: caf)
        XCTAssertEqual(Double(file.length), Double(frames), accuracy: 320)
        for start in [AVAudioFramePosition(1000), file.length / 2, file.length - 2000] {
            XCTAssertGreaterThan(try rms(file, start: start, count: 1600), 0.01)
        }
    }

    func testEmptyAndTinyInputsEncode() throws {
        let dir = try makeDir()
        let empty = dir.appendingPathComponent("e.pcm")
        FileManager.default.createFile(atPath: empty.path, contents: Data())
        XCTAssertNoThrow(try CaptureEncoder.encode(pcm: empty, to: dir.appendingPathComponent("e.caf")))
        let tiny = dir.appendingPathComponent("t.pcm")
        try writeTone(frames: 100, to: tiny)
        XCTAssertNoThrow(try CaptureEncoder.encode(pcm: tiny, to: dir.appendingPathComponent("t.caf")))
    }

    func testMissingInputThrowsAndLeavesExistingCAFUntouched() throws {
        let dir = try makeDir()
        let caf = dir.appendingPathComponent("a.caf")
        let original = Data("previous good capture".utf8)
        try original.write(to: caf)
        XCTAssertThrowsError(try CaptureEncoder.encode(pcm: dir.appendingPathComponent("missing.pcm"), to: caf))
        XCTAssertEqual(try Data(contentsOf: caf), original)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.contains("tmp") }, [])
    }
}
