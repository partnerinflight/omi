using System.ComponentModel;
using System.Diagnostics;
using System.Media;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Threading;
using SecondBrain.Status;

namespace SecondBrain.Tray;

public sealed class ClipRow(Clip clip) : INotifyPropertyChanged
{
    bool playing;
    double progress;

    public Clip Clip { get; } = clip;
    public string Range => Clip.Range;
    public string Quality => Clip.Quality;
    public string Text => Clip.Text;
    public string ButtonGlyph => Playing ? "" : "";  // Stop : Play
    public Visibility ProgressVisibility => Playing ? Visibility.Visible : Visibility.Collapsed;

    public bool Playing
    {
        get => playing;
        set { playing = value; Raise(); Raise(nameof(ButtonGlyph)); Raise(nameof(ProgressVisibility)); }
    }

    public double Progress
    {
        get => progress;
        set { progress = value; Raise(); }
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    void Raise([CallerMemberName] string? name = null) => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

// Plays clips from memory so private audio is never copied to a temp file. SoundPlayer cannot
// pause or seek, so this offers play/stop with a timer-driven progress bar.
sealed class ClipPlayer : IDisposable
{
    readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromMilliseconds(100) };
    readonly Stopwatch clock = new();
    SoundPlayer? player;
    MemoryStream? audio;
    TimeSpan length;

    public ClipPlayer() => timer.Tick += (_, _) => Tick();

    public ClipRow? Current { get; private set; }

    public void Play(ClipRow row, string path)
    {
        Stop();
        byte[] bytes = File.ReadAllBytes(path);
        var stream = new MemoryStream(bytes);
        var sound = new SoundPlayer(stream);
        try
        {
            sound.Load();
            sound.Play();
        }
        catch
        {
            sound.Dispose();
            stream.Dispose();
            throw;
        }
        (audio, player, length, Current) = (stream, sound, WavInfo.Duration(bytes) ?? row.Clip.Length, row);
        row.Progress = 0;
        row.Playing = true;
        clock.Restart();
        timer.Start();
    }

    void Tick()
    {
        if (Current is null) return;
        double progress = length <= TimeSpan.Zero ? 1 : clock.Elapsed / length;
        if (progress >= 1) Stop();
        else Current.Progress = progress;
    }

    public void Stop()
    {
        timer.Stop();
        player?.Stop();
        player?.Dispose();
        audio?.Dispose();
        (player, audio) = (null, null);
        if (Current is null) return;
        Current.Playing = false;
        Current.Progress = 0;
        Current = null;
    }

    public void Dispose() => Stop();
}
