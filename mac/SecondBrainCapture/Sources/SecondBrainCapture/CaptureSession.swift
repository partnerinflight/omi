import AVFoundation
import CaptureCore
import CoreAudio

/// One capture: tap (right) + mic (left) → 16 kHz resamplers → stereo assembler → PCM spool file.
/// Assembly and writing happen on `queue`; each resampler is used only by its own source thread.
final class CaptureSession {
    let record: CaptureRecord
    private let queue = DispatchQueue(label: "capture.session")
    private let tap = TapSource()
    private let mic = MicSource()
    private let assembler = StereoAssembler()
    private let writer: PCMWriter
    private var tapResampler: Resampler?
    private var micResampler: Resampler?
    private var writeError: Error?
    private var remotePeak: Float = 0  // on `queue`
    /// Fired on the main queue when the mic engine reconfigures or the default output device changes.
    var onInterrupted: (() -> Void)?
    private var engineObserver: NSObjectProtocol?
    private var outputListener: AudioObjectPropertyListenerBlock?
    private var outputAddress = AudioProcesses.address(kAudioHardwarePropertyDefaultSystemOutputDevice)

    init(record: CaptureRecord, spool: Spool) throws {
        self.record = record
        writer = try PCMWriter(url: spool.pcmURL(record.captureID))
    }

    func start(processes: [AudioObjectID], inputDevice: AudioDeviceID?) throws {
        try tap.start(processes: processes, queue: queue) { [weak self] buffer in
            guard let self else { return }
            if self.tapResampler == nil { self.tapResampler = Resampler(from: buffer.format) }
            let samples = self.tapResampler?.convert(buffer) ?? []
            for sample in samples { self.remotePeak = max(self.remotePeak, abs(sample)) }
            self.assembler.appendRight(samples)
            self.drain()
        }
        do {
            try mic.start(device: inputDevice) { [weak self] buffer in
                guard let self else { return }
                if self.micResampler == nil { self.micResampler = Resampler(from: buffer.format) }
                let samples = self.micResampler?.convert(buffer) ?? []
                self.queue.async {
                    self.assembler.appendLeft(samples)
                    self.drain()
                }
            }
        } catch {
            tap.stop()
            throw error
        }
        engineObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: mic.engine, queue: .main) { [weak self] _ in
            self?.onInterrupted?()
        }
        let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in self?.onInterrupted?() }
        outputListener = listener
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &outputAddress,
                                            DispatchQueue.main, listener)
    }

    /// Whether the app's audio (the tap) has carried any signal so far.
    func remoteAudioSeen() -> Bool { queue.sync { remotePeak > 0 } }

    /// Stop both sources, write what remains and close the file. Returns frames written.
    func finish() throws -> Int64 {
        if let engineObserver {
            NotificationCenter.default.removeObserver(engineObserver)
            self.engineObserver = nil
        }
        if let outputListener {
            AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &outputAddress,
                                                   DispatchQueue.main, outputListener)
            self.outputListener = nil
        }
        onInterrupted = nil
        mic.stop()
        tap.stop()
        return try queue.sync {
            // One close path: the file is closed even if the final write fails.
            var failure = writeError
            do { try writer.write(assembler.flush(), now: ProcessInfo.processInfo.systemUptime) } catch { failure = failure ?? error }
            do { try writer.close() } catch { failure = failure ?? error }
            if let failure { throw failure }
            return writer.framesWritten
        }
    }

    private func drain() {
        do {
            try writer.write(assembler.drain(), now: ProcessInfo.processInfo.systemUptime)
        } catch {
            writeError = error
        }
    }
}
