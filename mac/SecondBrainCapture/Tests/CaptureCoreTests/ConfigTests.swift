import XCTest
@testable import CaptureCore

final class ConfigTests: XCTestCase {
    var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    func testLoadCreatesDefaultsWhenMissing() throws {
        let url = dir.appendingPathComponent("sub/config.json")
        let config = try Config.load(from: url)
        XCTAssertEqual(config, Config())
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
        XCTAssertFalse(config.isConfigured)
    }

    func testPartialFileKeepsDefaultsForMissingKeys() throws {
        let url = dir.appendingPathComponent("config.json")
        try Data(#"{"host": "192.168.1.85"}"#.utf8).write(to: url)
        let config = try Config.load(from: url)
        XCTAssertEqual(config.host, "192.168.1.85")
        XCTAssertEqual(config.port, 7331)
        XCTAssertEqual(config.allowlist, Config.defaultAllowlist)
        XCTAssertTrue(config.isConfigured)
    }

    func testSaveRoundTrips() throws {
        let url = dir.appendingPathComponent("config.json")
        let config = Config(host: "h", port: 9, allowlist: ["a"], minCaptureSeconds: 5)
        try config.save(to: url)
        XCTAssertEqual(try Config.load(from: url), config)
    }

    func testSecretPathExpandsTilde() {
        XCTAssertEqual(Config().expandedSecretPath, NSHomeDirectory() + "/.omi-local/upload-secret.hex")
    }

    func testAppMatcherMatchesAppAndHelpersOnly() {
        let matcher = AppMatcher(allowlist: ["us.zoom.xos", "com.amazon.Amazon-Chime"])
        XCTAssertEqual(matcher.family(of: "us.zoom.xos"), "us.zoom.xos")
        XCTAssertEqual(matcher.family(of: "us.zoom.xos.ZoomAudioHelper"), "us.zoom.xos")
        XCTAssertNil(matcher.family(of: "us.zoom.xosx"))
        XCTAssertNil(matcher.family(of: "com.apple.Music"))
    }
}
