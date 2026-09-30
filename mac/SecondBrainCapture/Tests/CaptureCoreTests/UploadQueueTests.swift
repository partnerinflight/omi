import XCTest
@testable import CaptureCore

final class FakeTransport: UploadTransport {
    var calls: [String] = []
    var error: Error?
    var uploadError: Error?
    var onOpen: ((CaptureRecord) -> Void)?

    func openCapture(_ record: CaptureRecord) throws {
        try note("open \(record.captureID.prefix(2))")
        onOpen?(record)
    }
    func cancelCapture(id: String) throws { try note("cancel \(id.prefix(2))") }
    func upload(_ record: CaptureRecord, file: URL) throws {
        try note("upload \(record.captureID.prefix(2))")
        if let uploadError { throw uploadError }
    }

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
        for now in [1000.0, 2000, 3000] { queue.run(now: now) }
        XCTAssertEqual(spool.records().first?.state, .failed)
        let calls = transport.calls.count
        XCTAssertEqual(queue.run(now: 4000), 0)
        XCTAssertEqual(transport.calls.count, calls)
    }

    func testCancelDuringOpenIsNotReverted() throws {
        let r = try record("h", 1, .recording)
        transport.onOpen = { [spool] rec in _ = try? spool!.update(rec.captureID) { $0.state = .cancelled } }
        queue.run(now: 0)
        XCTAssertEqual(spool.load(r.captureID)?.state, .cancelled)
        XCTAssertEqual(spool.load(r.captureID)?.opened, true)
        XCTAssertEqual(transport.calls, ["open hh"])
    }

    func testCompleteDuringOpenIsNotReverted() throws {
        let r = try record("i", 1, .recording)
        transport.onOpen = { [spool] rec in
            _ = try? spool!.update(rec.captureID) { $0.state = .complete; $0.endMs = 99_000 }
        }
        queue.run(now: 0)
        if let after = spool.load(r.captureID) {
            XCTAssertEqual(after.state, .complete)
            XCTAssertEqual(after.endMs, 99_000)
        } else {
            XCTAssertEqual(transport.calls, ["open ii", "upload ii"])
        }
    }

    func testFailureAfterOpenKeepsOpenedAndCountsOnce() throws {
        let r = try record("j", 1, .complete)
        transport.uploadError = UploadError.protocolViolation("bad")
        queue.run(now: 0)
        let after = spool.load(r.captureID)
        XCTAssertEqual(after?.opened, true)
        XCTAssertEqual(after?.uploadFailures, 1)
        queue.run(now: 1000)
        XCTAssertEqual(transport.calls.filter { $0.hasPrefix("open") }.count, 1)
    }

    func testRecordingStaysRecordingWhenReceiverRejectsProtocol() throws {
        let r = try record("k", 1, .recording)
        transport.error = UploadError.rejected(Wire.Reject.protocolError.rawValue)
        for pass in 0..<5 { queue.run(now: 100_000 * Double(pass + 1)) }
        XCTAssertEqual(spool.load(r.captureID)?.state, .recording)
        XCTAssertEqual(spool.load(r.captureID)?.uploadFailures, 0)
        XCTAssertEqual(transport.calls.count, 5)
    }

    func testProtocolErrorBacksOff() throws {
        _ = try record("l", 1, .recording)
        transport.error = UploadError.rejected(Wire.Reject.protocolError.rawValue)
        queue.run(now: 0)
        XCTAssertEqual(queue.nextAttempt, 30)
        let calls = transport.calls.count
        queue.run(now: 10)
        XCTAssertEqual(transport.calls.count, calls)
    }

    func testResetFailedRevivesOnlyRecordsWithAudio() throws {
        var a = try record("m", 1, .failed)
        a.uploadFailures = 3
        try spool.save(a)
        try Data([1]).write(to: spool.cafURL(a.captureID))
        var b = try record("n", 2, .failed)
        b.uploadFailures = 3
        try spool.save(b)
        queue.resetFailed()
        XCTAssertEqual(spool.load(a.captureID)?.state, .complete)
        XCTAssertEqual(spool.load(a.captureID)?.uploadFailures, 0)
        XCTAssertEqual(spool.load(b.captureID)?.state, .failed)
    }
}
