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
