/// Joins two independently clocked 16 kHz mono streams into interleaved stereo Int16:
/// left = owner mic, right = remote (tapped app). If one stream stalls for more than
/// `maxSkewFrames`, it is padded with silence so memory stays bounded.
public final class StereoAssembler {
    private var left: [Float] = []
    private var right: [Float] = []
    private let maxSkew: Int

    public init(maxSkewFrames: Int = 16000) {
        maxSkew = maxSkewFrames
    }

    public func appendLeft(_ samples: [Float]) { left += samples }
    public func appendRight(_ samples: [Float]) { right += samples }

    /// Frames available on both channels.
    public func drain() -> [Int16] {
        if left.count > right.count + maxSkew {
            right += [Float](repeating: 0, count: left.count - right.count - maxSkew)
        }
        if right.count > left.count + maxSkew {
            left += [Float](repeating: 0, count: right.count - left.count - maxSkew)
        }
        return take(min(left.count, right.count))
    }

    /// Everything left at the end of a capture; the shorter channel is padded with silence.
    public func flush() -> [Int16] {
        let n = max(left.count, right.count)
        left += [Float](repeating: 0, count: n - left.count)
        right += [Float](repeating: 0, count: n - right.count)
        return take(n)
    }

    private func take(_ n: Int) -> [Int16] {
        var out = [Int16](repeating: 0, count: 2 * n)
        for i in 0..<n {
            out[2 * i] = Self.pcm(left[i])
            out[2 * i + 1] = Self.pcm(right[i])
        }
        left.removeFirst(n)
        right.removeFirst(n)
        return out
    }

    static func pcm(_ x: Float) -> Int16 {
        Int16((max(-1, min(1, x)) * 32767).rounded())
    }
}
