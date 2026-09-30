import XCTest
@testable import CaptureCore

final class UploadClientTests: XCTestCase {
    func testUnreachableReceiverThrowsUnreachable() {
        let client = UploadClient(host: "127.0.0.1", port: 1, secret: Data(count: 32), clientID: Data(count: 6),
                                  timeoutSeconds: 2)
        XCTAssertThrowsError(try client.cancelCapture(id: "00112233445566778899aabbccddeeff")) { error in
            guard case UploadError.unreachable = error else { return XCTFail("\(error)") }
        }
    }

    func testAgainstRealReceiver() throws {
        guard let python = ProcessInfo.processInfo.environment["SBC_RECEIVER_PYTHON"] else {
            throw XCTSkip("set SBC_RECEIVER_PYTHON to a Python that can import omi_local")
        }
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let secret = Data((0..<32).map { UInt8($0) })
        let portFile = dir.appendingPathComponent("port")
        let script = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("scripts/test_receiver.py")
        let receiver = Process()
        receiver.executableURL = URL(fileURLWithPath: python)
        receiver.arguments = [script.path, dir.path, secret.hex, portFile.path]
        try receiver.run()
        defer {
            receiver.terminate()
            receiver.waitUntilExit()
        }
        var port: UInt16?
        for _ in 0..<200 where port == nil {
            port = (try? String(contentsOf: portFile, encoding: .utf8)).flatMap { UInt16($0) }
            if port == nil { Thread.sleep(forTimeInterval: 0.05) }
        }
        let bound = try XCTUnwrap(port, "receiver did not start")
        let client = UploadClient(host: "127.0.0.1", port: bound, secret: secret, clientID: Data([1, 2, 3, 4, 5, 6]))

        let record = CaptureRecord(captureID: "00112233445566778899aabbccddeeff", app: "us.zoom.xos",
                                   startMs: 1000, endMs: 61000, state: .complete)
        let file = dir.appendingPathComponent("audio.caf")
        let payload = Data((0..<200_000).map { UInt8($0 % 251) })
        try payload.write(to: file)

        try client.openCapture(record)
        try client.upload(record, file: file)
        try client.upload(record, file: file)  // already committed: FILE_START == length, still FILE_BYE

        let meetings = dir.appendingPathComponent("meetings")
        XCTAssertEqual(try Data(contentsOf: meetings.appendingPathComponent("\(record.captureID).caf")), payload)
        let sidecar = try JSONSerialization.jsonObject(
            with: Data(contentsOf: meetings.appendingPathComponent("\(record.captureID).json"))) as? [String: Any]
        XCTAssertEqual(sidecar?["start_ms"] as? Int, 1000)
        XCTAssertEqual(sidecar?["end_ms"] as? Int, 61000)
        XCTAssertEqual(sidecar?["app"] as? String, "us.zoom.xos")

        let cancelID = "ffeeddccbbaa99887766554433221100"
        try client.cancelCapture(id: cancelID)
        let marker = try JSONSerialization.jsonObject(
            with: Data(contentsOf: meetings.appendingPathComponent(".captures/\(cancelID).json"))) as? [String: Any]
        XCTAssertEqual(marker?["state"] as? String, "cancelled")

        let wrongSecret = UploadClient(host: "127.0.0.1", port: bound, secret: Data(repeating: 9, count: 32),
                                       clientID: Data([1, 2, 3, 4, 5, 6]))
        XCTAssertThrowsError(try wrongSecret.openCapture(record)) { XCTAssertEqual($0 as? UploadError, .authFailed) }

        let badMetadata = CaptureRecord(captureID: "0000000000000000000000000000000a", app: "",
                                        startMs: 5, endMs: 1, state: .complete)
        XCTAssertThrowsError(try client.upload(badMetadata, file: file)) {
            XCTAssertEqual($0 as? UploadError, .rejected(Wire.Reject.protocolError.rawValue))
        }
    }
}
