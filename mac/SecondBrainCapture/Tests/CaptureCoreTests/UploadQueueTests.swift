import XCTest
@testable import CaptureCore

final class FakeTransport: UploadTransport {
    var calls: [String] = []
    var error: Error?

    func openCapture(_ record: CaptureRecord) throws { try note("open \(record.captureID.prefix(2))") }
    func cancelCapture(id: String) throws { try note("cancel \(id.prefix(2))") }
    func upload(_ record: CaptureRecord, file: URL) throws { try note("upload \(record.captureID.prefix(2))") }

    private func note(_ call: String) throws {
        calls.append(call)
        if let error { throw error }
    }
}

final class UploadQueueTests: XCTestCase {
    var dir: URL!
    var spool: Spool!
    let transport = FakeTransport()
    var queue: UploadQueue!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        spool = try Spool(root: dir)
        queue = UploadQueue(spool: spool, transport: transport)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    func record(_ prefix: String, _ start: Int64, _ state: CaptureRecord.State, opened: Bool = false) throws -> CaptureRecord {
        let r = CaptureRecord(captureID: String(repeating: prefix, count: 32), app: "us.zoom.xos", startMs: start,
                              endMs: start + 60_000, state: state, opened: opened)
        try spool.save(r)
        return r
    }

    func testRecordingCaptureIsOpenedOnceAndKept() throws {
        let r = try record("a", 1, .recording)
        XCTAssertEqual(queue.run(now: 0), 0)
        XCTAssertEqual(queue.run(now: 1), 0)
        XCTAssertEqual(transport.calls, ["open aa"])
        XCTAssertEqual(spool.records().first?.opened, true)
        XCTAssertEqual(spool.records().first?.captureID, r.captureID)
    }

    func testCompleteCaptureIsOpenedUploadedAndDeleted() throws {
        _ = try record("b", 1, .complete)
        XCTAssertEqual(queue.run(now: 0), 0)
        XCTAssertEqual(transport.calls, ["open bb", "upload bb"])
        XCTAssertEqual(spool.records(), [])
    }

    func testCancelledCaptureSendsCancelAndIsDeleted() throws {
        _ = try record("c", 1, .cancelled)
        queue.run(now: 0)
        XCTAssertEqual(transport.calls, ["cancel cc"])
        XCTAssertEqual(spool.records(), [])
    }

    func testOldestFirst() throws {
        _ = try record("e", 2, .cancelled)
        _ = try record("d", 1, .cancelled)
        queue.run(now: 0)
        XCTAssertEqual(transport.calls, ["cancel dd", "cancel ee"])
    }

    func testUnreachableBacksOffExponentiallyWithCap() throws {
        _ = try record("f", 1, .complete, opened: true)
        transport.error = UploadError.unreachable("down")
        XCTAssertEqual(queue.run(now: 0), 1)
        XCTAssertEqual(queue.nextAttempt, 30)
        XCTAssertEqual(queue.run(now: 10), 1)
        XCTAssertEqual(transport.calls.count, 1)
        queue.run(now: 30)
        XCTAssertEqual(queue.nextAttempt, 90)
        for step in 0..<10 { queue.run(now: 10_000 * Double(step + 1)) }
        XCTAssertEqual(queue.nextAttempt - 100_000, UploadQueue.maxBackoff)
        XCTAssertNotNil(queue.lastError)
        transport.error = nil
        queue.run(now: 200_000)
        XCTAssertEqual(spool.records(), [])
        XCTAssertNil(queue.lastError)
    }

    func testBusyIsRetryableButProtocolRejectsFailTheCaptureAfterThree() throws {
        _ = try record("g", 1, .complete, opened: true)
        transport.error = UploadError.rejected(Wire.Reject.busy.rawValue)
        queue.run(now: 0)
        XCTAssertEqual(spool.records().first?.uploadFailures, 0)
        transport.error = UploadError.rejected(Wire.Reject.protocolError.rawValue)
        for now in [100.0, 200, 300] { queue.run(now: now) }
        XCTAssertEqual(spool.records().first?.state, .failed)
        let calls = transport.calls.count
        XCTAssertEqual(queue.run(now: 400), 0)
        XCTAssertEqual(transport.calls.count, calls)
    }
}
