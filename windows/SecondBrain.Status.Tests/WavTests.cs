using System.Text;

namespace SecondBrain.Status.Tests;

static class WavTests
{
    // RIFF/WAVE with fmt (16 kHz mono 16-bit unless overridden), an optional odd-sized LIST chunk, and data.
    static byte[] Wav(int declaredData, int actualData, int byteRate = 32000, bool list = false)
    {
        using var stream = new MemoryStream();
        using var w = new BinaryWriter(stream);
        w.Write(Encoding.ASCII.GetBytes("RIFF")); w.Write(0); w.Write(Encoding.ASCII.GetBytes("WAVE"));
        w.Write(Encoding.ASCII.GetBytes("fmt ")); w.Write(16);
        w.Write((short)1); w.Write((short)1); w.Write(16000); w.Write(byteRate); w.Write((short)2); w.Write((short)16);
        if (list) { w.Write(Encoding.ASCII.GetBytes("LIST")); w.Write(3); w.Write(new byte[] { 1, 2, 3, 0 }); }
        w.Write(Encoding.ASCII.GetBytes("data")); w.Write(declaredData); w.Write(new byte[actualData]);
        return stream.ToArray();
    }

    public static void Run()
    {
        Check.Equal<TimeSpan?>(TimeSpan.FromSeconds(1.5), WavInfo.Duration(Wav(48000, 48000)), "plain PCM duration");
        Check.Equal<TimeSpan?>(TimeSpan.FromSeconds(1.5), WavInfo.Duration(Wav(48000, 48000, list: true)), "skips a padded odd-sized chunk");
        Check.Equal<TimeSpan?>(TimeSpan.FromSeconds(0.5), WavInfo.Duration(Wav(48000, 16000)), "truncated data uses the bytes present");
        Check.Equal<TimeSpan?>(null, WavInfo.Duration(Wav(48000, 48000, byteRate: 0)), "zero byte rate");
        Check.Equal<TimeSpan?>(null, WavInfo.Duration(Encoding.ASCII.GetBytes("not a wav file at all")), "not RIFF");
        Check.Equal<TimeSpan?>(null, WavInfo.Duration(Wav(48000, 48000)[..10]), "header cut short");
    }
}
