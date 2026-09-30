import AVFoundation
import CoreAudio

/// Captures one input device (default input if nil) with AVAudioEngine.
final class MicSource {
    private let engine = AVAudioEngine()

    /// `onBuffer` runs on the engine's audio thread.
    func start(device: AudioDeviceID?, onBuffer: @escaping (AVAudioPCMBuffer) -> Void) throws {
        let input = engine.inputNode
        if var device, let unit = input.audioUnit {
            try check("select input device", AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice,
                                                                  kAudioUnitScope_Global, 0, &device,
                                                                  UInt32(MemoryLayout<AudioDeviceID>.size)))
        }
        input.installTap(onBus: 0, bufferSize: 4096, format: input.outputFormat(forBus: 0)) { buffer, _ in
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
