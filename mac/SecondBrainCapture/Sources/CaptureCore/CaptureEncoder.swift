import AVFoundation

/// Encodes spooled 16 kHz stereo Int16 PCM into stereo Opus (32 kbit/s) in CAF, which the
/// receiver's ffmpeg decodes. Writes a temporary file and renames it, so a crash during
/// encoding never leaves a truncated `.caf`.
public enum CaptureEncoder {
    public static let pcmFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000,
                                                channels: 2, interleaved: true)!

    public static func encode(pcm: URL, to caf: URL) throws {
        // AVAudioFile picks the container from the extension, so keep ".caf" last.
        let temporary = caf.deletingPathExtension().appendingPathExtension("tmp").appendingPathExtension("caf")
        try? FileManager.default.removeItem(at: temporary)
        try write(pcm: pcm, to: temporary)
        try? FileManager.default.removeItem(at: caf)
        try FileManager.default.moveItem(at: temporary, to: caf)
    }

    /// Separate function so the file is closed (and finalized) before the rename.
    ///
    /// AVAudioFile ignores AVEncoderBitRateKey for Opus (it produces ~670 kbit/s), so this drives
    /// AVAudioConverter, which honors `bitRate`, and writes the packets with the AudioFile API.
    private static func write(pcm: URL, to url: URL) throws {
        var description = AudioStreamBasicDescription(
            mSampleRate: 16000, mFormatID: kAudioFormatOpus, mFormatFlags: 0, mBytesPerPacket: 0,
            mFramesPerPacket: 320, mBytesPerFrame: 0, mChannelsPerFrame: 2, mBitsPerChannel: 0, mReserved: 0)
        guard let layout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_Stereo),
              let opusFormat = AVAudioFormat(streamDescription: &description, channelLayout: layout),
              let converter = AVAudioConverter(from: pcmFormat, to: opusFormat)
        else { throw CocoaError(.fileWriteUnknown) }
        converter.bitRate = 32000

        var fileID: AudioFileID?
        try check(AudioFileCreateWithURL(url as CFURL, kAudioFileCAFType, &description, .eraseFile, &fileID))
        guard let fileID else { throw CocoaError(.fileWriteUnknown) }
        defer { AudioFileClose(fileID) }
        // Default header padding is ~32 KB, which is large for short captures; the packet table is
        // rewritten on close anyway.
        var reserve: Float64 = 0
        try check(AudioFileSetProperty(fileID, kAudioFilePropertyReserveDuration, UInt32(MemoryLayout<Float64>.size), &reserve))
        if let cookie = converter.magicCookie {
            try cookie.withUnsafeBytes {
                try check(AudioFileSetProperty(fileID, kAudioFilePropertyMagicCookieData, UInt32(cookie.count), $0.baseAddress!))
            }
        }
        var layoutTag = layout.layout.pointee
        try check(AudioFileSetProperty(fileID, kAudioFilePropertyChannelLayout,
                                       UInt32(MemoryLayout<AudioChannelLayout>.size), &layoutTag))

        let input = try FileHandle(forReadingFrom: pcm)
        defer { try? input.close() }
        let chunkFrames = 16000
        guard let buffer = AVAudioPCMBuffer(pcmFormat: pcmFormat, frameCapacity: AVAudioFrameCount(chunkFrames)) else {
            throw CocoaError(.fileWriteUnknown)
        }
        var inputDone = false
        var readError: Error?
        var packetsWritten: Int64 = 0
        while true {
            let output = AVAudioCompressedBuffer(format: opusFormat, packetCapacity: 32,
                                                 maximumPacketSize: converter.maximumOutputPacketSize)
            var convertError: NSError?
            let status = converter.convert(to: output, error: &convertError) { _, inputStatus in
                if inputDone { inputStatus.pointee = .endOfStream; return nil }
                do {
                    guard let data = try input.read(upToCount: chunkFrames * 4), data.count >= 4 else {
                        inputDone = true
                        inputStatus.pointee = .endOfStream
                        return nil
                    }
                    let frames = data.count / 4
                    data.withUnsafeBytes { raw in
                        buffer.int16ChannelData![0].update(from: raw.bindMemory(to: Int16.self).baseAddress!, count: frames * 2)
                    }
                    buffer.frameLength = AVAudioFrameCount(frames)
                    inputStatus.pointee = .haveData
                    return buffer
                } catch {
                    readError = error
                    inputDone = true
                    inputStatus.pointee = .endOfStream
                    return nil
                }
            }
            if let readError { throw readError }
            if status == .error { throw convertError ?? CocoaError(.fileWriteUnknown) }
            if output.packetCount > 0 {
                var count = output.packetCount
                try check(AudioFileWritePackets(fileID, false, output.byteLength, output.packetDescriptions,
                                                packetsWritten, &count, output.data))
                packetsWritten += Int64(count)
            }
            if status == .endOfStream { break }
        }
    }

    private static func check(_ status: OSStatus) throws {
        if status != noErr { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }
}
