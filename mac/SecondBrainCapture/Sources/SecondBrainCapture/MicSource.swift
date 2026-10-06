import AVFoundation
import CoreAudio

/// Captures one input device (default input if nil) with AVAudioEngine.
final class MicSource {
    let engine = AVAudioEngine()

    /// `onBuffer` runs on the engine's audio thread.
    func start(device: AudioDeviceID?, onBuffer: @escaping (AVAudioPCMBuffer) -> Void) throws {
        let input = engine.inputNode
        // Core Audio renumbers device objects as devices come and go, so the id from the
        // process snapshot can already be stale. Recording the default input beats not
        // recording: it is the same physical microphone in all but unusual setups.
        if var device {
            let status = input.audioUnit.map {
                AudioUnitSetProperty($0, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                                     &device, UInt32(MemoryLayout<AudioDeviceID>.size))
            } ?? OSStatus(-1)
            if status != noErr {
                log.notice("mic device \(device) unavailable (OSStatus \(status)); using the default input")
            }
        }
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw CoreAudioError(what: "mic input format", status: -1)
        }
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
            onBuffer(buffer)
        }
        engine.prepare()
        try engine.start()
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }
}
