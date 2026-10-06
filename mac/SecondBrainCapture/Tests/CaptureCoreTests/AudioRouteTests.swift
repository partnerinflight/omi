import XCTest
@testable import CaptureCore

final class AudioRouteTests: XCTestCase {
    let format = AudioRoute.InputFormat(sampleRate: 48000, channels: 1)

    func testConfigChangeWhileRunningWithSameFormatIsBenign() {
        // AVAudioEngine posts this ~100 ms after selecting the input device, and keeps running.
        XCTAssertFalse(AudioRoute.isDisruptive(engineRunning: true, before: format, after: format))
    }

    func testStoppedEngineIsDisruptive() {
        XCTAssertTrue(AudioRoute.isDisruptive(engineRunning: false, before: format, after: format))
    }

    func testFormatChangeIsDisruptive() {
        XCTAssertTrue(AudioRoute.isDisruptive(engineRunning: true, before: format,
                                              after: .init(sampleRate: 24000, channels: 1)))
        XCTAssertTrue(AudioRoute.isDisruptive(engineRunning: true, before: format,
                                              after: .init(sampleRate: 48000, channels: 2)))
    }
}
