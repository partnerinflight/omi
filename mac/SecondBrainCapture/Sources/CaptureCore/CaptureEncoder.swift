import AVFoundation

/// Encodes spooled 16 kHz stereo Int16 PCM into stereo Opus (32 kbit/s) in CAF, which the
/// receiver's ffmpeg decodes. Writes a temporary file and renames it, so a crash during
/// encoding never leaves a truncated `.caf`.
public enum CaptureEncoder {
    public static let pcmFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000,
                                                channels: 2, interleaved: true)!

    public static func encode(pcm: URL, to caf: URL) throws {
        let temporary = caf.deletingPathExtension().appendingPathExtension("tmp").appendingPathExtension("caf")
        try? FileManager.default.removeItem(at: temporary)
        do {
            try write(pcm: pcm, to: temporary)
            if FileManager.default.fileExists(atPath: caf.path) {
                _ = try FileManager.default.replaceItemAt(caf, withItemAt: temporary)
            } else {
                try FileManager.default.moveItem(at: temporary, to: caf)
            }
        } catch {
            try? FileManager.default.removeItem(at: temporary)
            throw error
        }
    }

    /// Separate function so the file is closed (and finalized) before the rename.
    ///
    /// AVAudioFile ignores AVEncoderBitRateKey for Opus (it produces ~670 kbit/s), so this drives
    /// AVAudioConverter, which honors `bitRate`, and writes the packets with the AudioFile API.
    private static func write(pcm: URL, to url: URL) throws {
        var description = AudioStreamBasicDescription(
            mSampleRate: 16000, mFormatID: kAudioFormatOpus, mFormatFlags: 0, mBytesPerPacket: 0,
            mFramesPerPacket: 320, mBytesPerFrame: 0, mChannelsPerFrame: 2, mBitsPerChannel: 0, mReserved: 0)
        guard let channelLayout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_Stereo),
              let opusFormat = AVAudioFormat(streamDescription: &description, channelLayout: channelLayout),
              let converter = AVAudioConverter(from: pcmFormat, to: opusFormat)
        else { throw CocoaError(.fileWriteUnknown) }
        converter.bitRate = 32000
        let framesPerPacket = Int64(opusFormat.streamDescription.pointee.mFramesPerPacket)

        var created: AudioFileID?
        try check(AudioFileCreateWithURL(url as CFURL, kAudioFileCAFType, &description, .eraseFile, &created))
        guard let fileID = created else { throw CocoaError(.fileWriteUnknown) }
        var closed = false
        defer { if !closed { AudioFileClose(fileID) } }

        // Default header padding is ~32 KB, which is large for short captures; the packet table is
        // rewritten on close anyway.
        var reserve: Float64 = 0
        try check(AudioFileSetProperty(fileID, kAudioFilePropertyReserveDuration, UInt32(MemoryLayout<Float64>.size), &reserve))
        if let cookie = converter.magicCookie {
            try cookie.withUnsafeBytes {
                try check(AudioFileSetProperty(fileID, kAudioFilePropertyMagicCookieData, UInt32(cookie.count), $0.baseAddress!))
            }
        }
        var layout = channelLayout.layout.pointee
        try check(AudioFileSetProperty(fileID, kAudioFilePropertyChannelLayout,
                                       UInt32(MemoryLayout<AudioChannelLayout>.size), &layout))

        let input = try FileHandle(forReadingFrom: pcm)
        defer { try? input.close() }
        let chunkFrames = 16000
        guard let buffer = AVAudioPCMBuffer(pcmFormat: pcmFormat, frameCapacity: AVAudioFrameCount(chunkFrames))
        else { throw CocoaError(.fileWriteUnknown) }
        let output = AVAudioCompressedBuffer(format: opusFormat, packetCapacity: 32,
                                             maximumPacketSize: converter.maximumOutputPacketSize)
        var inputDone = false
        var readError: Error?
        var totalFrames: Int64 = 0
        var packetsWritten: Int64 = 0
        while true {
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
                    totalFrames += Int64(frames)
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

        // Declare the encoder delay and end padding so readers trim to the real duration.
        let leading = Int64(max(0, converter.primeInfo.leadingFrames))
        var info = AudioFilePacketTableInfo(
            mNumberValidFrames: totalFrames,
            mPrimingFrames: Int32(leading),
            mRemainderFrames: Int32(max(0, packetsWritten * framesPerPacket - totalFrames - leading)))
        try check(AudioFileSetProperty(fileID, kAudioFilePropertyPacketTableInfo,
                                       UInt32(MemoryLayout<AudioFilePacketTableInfo>.size), &info))
        closed = true
        try check(AudioFileClose(fileID))
    }

    private static func check(_ status: OSStatus) throws {
        if status != noErr { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }
}
