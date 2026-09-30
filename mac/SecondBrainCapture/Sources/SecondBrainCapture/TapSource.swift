import AVFoundation
import CoreAudio

/// Captures the mixed output of specific processes through a Core Audio process tap
/// attached to a private aggregate device. Requires "System Audio Recording Only".
final class TapSource {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private(set) var format: AVAudioFormat?

    /// `onBuffer` runs on `queue`; the buffer is only valid during the call.
    func start(processes: [AudioObjectID], queue: DispatchQueue, onBuffer: @escaping (AVAudioPCMBuffer) -> Void) throws {
        let description = CATapDescription(stereoMixdownOfProcesses: processes)
        description.uuid = UUID()
        description.muteBehavior = .unmuted
        description.isPrivate = true
        description.name = "SecondBrainCapture"
        try check("create process tap", AudioHardwareCreateProcessTap(description, &tapID))

        var addr = AudioProcesses.address(kAudioTapPropertyFormat)
        var stream = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        try check("read tap format", AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &stream))
        guard let format = AVAudioFormat(streamDescription: &stream) else {
            throw CoreAudioError(what: "tap format", status: -1)
        }
        self.format = format

        let outputUID = try AudioProcesses.defaultOutputUID()
        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "SecondBrainCapture tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: description.uuid.uuidString]],
        ]
        try check("create aggregate device", AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID))
        try check("create IO proc", AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, queue) { _, input, _, _, _ in
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: input, deallocator: nil) else { return }
            onBuffer(buffer)
        })
        try check("start aggregate device", AudioDeviceStart(aggregateID, procID))
    }

    func stop() {
        if aggregateID != AudioObjectID(kAudioObjectUnknown) {
            if let procID {
                AudioDeviceStop(aggregateID, procID)
                AudioDeviceDestroyIOProcID(aggregateID, procID)
            }
            AudioHardwareDestroyAggregateDevice(aggregateID)
        }
        if tapID != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyProcessTap(tapID)
        }
        tapID = AudioObjectID(kAudioObjectUnknown)
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        procID = nil
    }

    deinit {
        stop()
    }
}
