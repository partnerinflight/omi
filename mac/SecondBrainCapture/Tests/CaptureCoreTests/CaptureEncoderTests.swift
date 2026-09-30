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
