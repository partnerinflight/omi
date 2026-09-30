import XCTest
@testable import CaptureCore

final class MeetingDetectorTests: XCTestCase {
    let zoom = AudioProcess(objectID: 10, bundleID: "us.zoom.xos", isRunningInput: true, inputDevices: [77])
    let zoomIdle = AudioProcess(objectID: 10, bundleID: "us.zoom.xos", isRunningInput: false)
    let zoomHelper = AudioProcess(objectID: 11, bundleID: "us.zoom.xos.helper", isRunningInput: false)
    let music = AudioProcess(objectID: 20, bundleID: "com.apple.Music", isRunningInput: true)

    func make() -> MeetingDetector {
        MeetingDetector(matcher: AppMatcher(allowlist: Config.defaultAllowlist), releaseGraceSeconds: 20)
    }

    func testStartsWhenAllowlistedAppHoldsMicAndTapsItsHelpers() {
        let d = make()
        XCTAssertNil(d.update([music], now: 0))
        XCTAssertEqual(d.update([zoom, zoomHelper, music], now: 1),
                       .start(app: "us.zoom.xos", processes: [10, 11], inputDevice: 77))
        XCTAssertNil(d.update([zoom, zoomHelper], now: 2))
        XCTAssertEqual(d.activeApp, "us.zoom.xos")
    }

    func testStopsOnlyAfterReleaseGrace() {
        let d = make()
        _ = d.update([zoom], now: 0)
        XCTAssertNil(d.update([zoomIdle], now: 10))
        XCTAssertNil(d.update([zoomIdle], now: 29.9))
        XCTAssertEqual(d.update([zoomIdle], now: 30), .stop)
        XCTAssertNil(d.activeApp)
    }

    func testReacquiringMicWithinGraceKeepsCapture() {
        let d = make()
        _ = d.update([zoom], now: 0)
        XCTAssertNil(d.update([zoomIdle], now: 10))
        XCTAssertNil(d.update([zoom], now: 25))
        XCTAssertNil(d.update([zoomIdle], now: 40))
        XCTAssertNil(d.update([zoomIdle], now: 59))
        XCTAssertEqual(d.update([zoomIdle], now: 60), .stop)
    }

    func testSkipStopsAndIgnoresAppUntilItReleasesMic() {
        let d = make()
        _ = d.update([zoom], now: 0)
        XCTAssertEqual(d.skip(), .stop)
        XCTAssertNil(d.update([zoom], now: 5))
        XCTAssertNil(d.update([zoomIdle], now: 6))
        XCTAssertEqual(d.update([zoom], now: 7), .start(app: "us.zoom.xos", processes: [10], inputDevice: 77))
        XCTAssertNil(make().skip())
    }

    func testPauseStopsAndDefersStart() {
        let d = make()
        _ = d.update([zoom], now: 0)
        XCTAssertEqual(d.pause(until: 3600), .stop)
        XCTAssertNil(d.update([zoom], now: 100))
        XCTAssertEqual(d.pausedUntil, 3600)
        XCTAssertNotNil(d.update([zoom], now: 3600))
        XCTAssertNil(make().pause(until: 10))
    }

    func testResumeClearsPause() {
        let d = make()
        _ = d.pause(until: 3600)
        _ = d.pause(until: 0)
        XCTAssertNotNil(d.update([zoom], now: 5))
    }

    func testFirstAllowlistedAppWinsWhenSeveralHoldMic() {
        let chime = AudioProcess(objectID: 30, bundleID: "com.amazon.Amazon-Chime", isRunningInput: true)
        XCTAssertEqual(make().update([zoom, chime], now: 0),
                       .start(app: "com.amazon.Amazon-Chime", processes: [30], inputDevice: nil))
    }
}
