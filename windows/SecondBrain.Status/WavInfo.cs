using System.Buffers.Binary;

namespace SecondBrain.Status;

public static class WavInfo
{
    public static TimeSpan? Duration(ReadOnlySpan<byte> wav)
    {
        if (wav.Length < 12 || !wav[..4].SequenceEqual("RIFF"u8) || !wav.Slice(8, 4).SequenceEqual("WAVE"u8)) return null;
        int byteRate = 0;
        long data = -1;
        int offset = 12;
        while (offset + 8 <= wav.Length)
        {
            var id = wav.Slice(offset, 4);
            uint size = BinaryPrimitives.ReadUInt32LittleEndian(wav.Slice(offset + 4, 4));
            int body = offset + 8;
            if (id.SequenceEqual("fmt "u8) && body + 12 <= wav.Length)
                byteRate = BinaryPrimitives.ReadInt32LittleEndian(wav.Slice(body + 8, 4));
            else if (id.SequenceEqual("data"u8))
            {
                data = Math.Min(size, (long)(wav.Length - body));
                break;
            }
            long next = (long)body + size + (size & 1);  // chunks are word-aligned
            if (next > wav.Length) break;
            offset = (int)next;
        }
        return byteRate > 0 && data >= 0 ? TimeSpan.FromSeconds((double)data / byteRate) : null;
    }
}
