import CryptoKit
import XCTest
@testable import CaptureCore

final class WireTests: XCTestCase {
    let secret = Data((0..<32).map { UInt8($0) })
    let clientNonce = Data((0..<16).map { UInt8(0xA0 + $0) })
    let serverNonce = Data((0..<16).map { UInt8(0xB0 + $0) })
    let captureID = Data((0..<16).map { UInt8($0) })

    func testAuthTagsMatchPythonReceiver() {
        XCTAssertEqual(Wire.authTag(secret: secret, label: Wire.labelServer, clientNonce: clientNonce, serverNonce: serverNonce).hex,
                       "f82dc3e03e829f7ab1d4adbbf870815979bcdc1053ed4764e467412e7f5d3f1f")
        XCTAssertEqual(Wire.authTag(secret: secret, label: Wire.labelClient, clientNonce: clientNonce, serverNonce: serverNonce).hex,
                       "5089e2fe21da82fc22dbeaa1ba0c1b187dd3d2105d9749a614172f6e8bb096e9")
    }

    func testFramesMatchPythonReceiver() {
        XCTAssertEqual(Wire.frame(.hello, Wire.hello(clientID: Data([1, 2, 3, 4, 5, 6]), nonce: clientNonce)).hex,
                       "010000001b4f4d494c02010203040506a0a1a2a3a4a5a6a7a8a9aaabacadaeaf")
        XCTAssertEqual(Wire.frame(.captureOpen, Wire.captureOpen(id: captureID, startMs: 1_790_000_000_123, app: "us.zoom.xos")).hex,
                       "1000000023000102030405060708090a0b0c0d0e0f000001a0c4506c7b75732e7a6f6f6d2e786f73")
        XCTAssertEqual(Wire.frame(.fileData, Wire.fileData(offset: 4096, bytes: Data("abc".utf8))).hex,
                       "130000000b0000000000001000616263")
    }

    func testFileBeginHeadAndMetadata() throws {
        let sha = Data(SHA256.hash(data: Data("hello".utf8)))
        let payload = try Wire.fileBegin(id: captureID, totalLength: 5, sha256: sha,
                                         metadata: ["start_ms": Int64(1), "end_ms": Int64(2), "app": "x"])
        XCTAssertEqual(payload.prefix(56).hex,
                       "000102030405060708090a0b0c0d0e0f0000000000000005"
                       + "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
        let json = try JSONSerialization.jsonObject(with: payload.dropFirst(56)) as? [String: Any]
        XCTAssertEqual(json?["start_ms"] as? Int, 1)
        XCTAssertEqual(json?["end_ms"] as? Int, 2)
        XCTAssertEqual(json?["app"] as? String, "x")
    }

    func testHeaderAndIntegerHelpers() throws {
        let (type, length) = try Wire.parseHeader(Data([0x16, 0, 0, 1, 2]))
        XCTAssertEqual(type, 0x16)
        XCTAssertEqual(length, 258)
        XCTAssertEqual(try Wire.readU64(Wire.u64(0x0102_0304_0506_0708)), 0x0102_0304_0506_0708)
        XCTAssertEqual(try Wire.readU64(Data([9, 0, 0, 0, 0, 0, 0, 0, 5]).dropFirst()), 5)
    }

    func testMalformedInputIsRejected() {
        XCTAssertThrowsError(try Wire.parseHeader(Data([1, 0, 0, 0])))
        XCTAssertThrowsError(try Wire.readU64(Data([1, 2, 3])))
        XCTAssertThrowsError(try Wire.fileBegin(id: captureID, totalLength: 1, sha256: Data(count: 32), metadata: ["x": Double.nan])) {
            XCTAssertTrue($0 is Wire.Malformed)
        }
        XCTAssertNil(Data(hex: "+1"))
        XCTAssertNil(Data(hex: "-1"))
        XCTAssertEqual(Data(hex: "ABcd")?.hex, "abcd")
    }

    func testHexRoundTripAndRejectsBadHex() {
        XCTAssertEqual(Data(hex: "00ff10")?.hex, "00ff10")
        XCTAssertNil(Data(hex: "0g"))
        XCTAssertNil(Data(hex: "abc"))
    }

    func testClientIDIsFirstSixBytesOfUUIDHash() {
        let id = ClientIdentity.clientID(uuid: "ABC")
        XCTAssertEqual(id, Data(SHA256.hash(data: Data("ABC".utf8)).prefix(6)))
        XCTAssertEqual(id.count, 6)
        XCTAssertNotNil(ClientIdentity.platformUUID())
    }

    func testLoadSecretParsesHexFileAndRejectsBadLength() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: url) }
        try (secret.hex + "\n").write(to: url, atomically: true, encoding: .utf8)
        XCTAssertEqual(try ClientIdentity.loadSecret(path: url.path), secret)
        try "abcd".write(to: url, atomically: true, encoding: .utf8)
        XCTAssertThrowsError(try ClientIdentity.loadSecret(path: url.path))
    }
}
