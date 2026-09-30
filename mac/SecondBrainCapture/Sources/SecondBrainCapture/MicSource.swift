import AVFoundation
import CoreAudio

/// Captures one input device (default input if nil) with AVAudioEngine.
final class MicSource {
    private let engine = AVAudioEngine()

    /// `onBuffer` runs on the engine's audio thread.
    func start(device: AudioDeviceID?, onBuffer: @escaping (AVAudioPCMBuffer) -> Void) throws {
        let input = engine.inputNode
        if var device {
            guard let unit = input.audioUnit else { throw CoreAudioError(what: "mic audio unit", status: -1) }
            try check("select input device", AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice,
                                                                  kAudioUnitScope_Global, 0, &device,
                                                                  UInt32(MemoryLayout<AudioDeviceID>.size)))
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
