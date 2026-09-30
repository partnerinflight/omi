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

    init(record: CaptureRecord, spool: Spool) throws {
        self.record = record
        writer = try PCMWriter(url: spool.pcmURL(record.captureID))
    }

    func start(processes: [AudioObjectID], inputDevice: AudioDeviceID?) throws {
        try tap.start(processes: processes, queue: queue) { [weak self] buffer in
            guard let self else { return }
            if self.tapResampler == nil { self.tapResampler = Resampler(from: buffer.format) }
            self.assembler.appendRight(self.tapResampler?.convert(buffer) ?? [])
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
    }

    /// Stop both sources, write what remains and close the file. Returns frames written.
    func finish() throws -> Int64 {
        mic.stop()
        tap.stop()
        return try queue.sync {
            try writer.write(assembler.flush(), now: ProcessInfo.processInfo.systemUptime)
            try writer.close()
            if let writeError { throw writeError }
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
