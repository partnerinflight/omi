using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Configuration;

var builder = Host.CreateApplicationBuilder(args);
builder.Services.AddWindowsService(o => o.ServiceName = builder.Configuration["service-name"] ?? "SecondBrain");
builder.Services.Configure<HostOptions>(o => o.ShutdownTimeout = TimeSpan.FromSeconds(45));
builder.Services.AddHostedService<PipelineWorker>();
await builder.Build().RunAsync();

sealed class PipelineWorker(IConfiguration config, ILogger<PipelineWorker> logger) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        using var job = new ProcessJob();
        using var process = new Process();
        try
        {
            string python = config["python"] ?? throw new InvalidOperationException("--python is required");
            string settings = config["config"] ?? throw new InvalidOperationException("--config is required");
            if (!Path.IsPathFullyQualified(python) || !Path.IsPathFullyQualified(settings))
                throw new InvalidOperationException("Service paths must be absolute");
            process.StartInfo = new(python) {
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true,
                WorkingDirectory = AppContext.BaseDirectory
            };
            foreach (var arg in new[] { "-u", "-m", "second_brain.cli", "run", "--config", settings, "--stdin-control" })
                process.StartInfo.ArgumentList.Add(arg);
            process.StartInfo.Environment["PYTHONUTF8"] = "1";
            if (!process.Start()) throw new InvalidOperationException("Python worker did not start");
            job.Assign(process);
            logger.LogInformation("Second Brain worker started");
            try
            {
                await process.WaitForExitAsync(stoppingToken);
                throw new InvalidOperationException($"Worker exited unexpectedly ({process.ExitCode})");
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                try { await process.StandardInput.WriteLineAsync("stop"); } catch (IOException) { }
                using var grace = new CancellationTokenSource(TimeSpan.FromSeconds(35));
                try { await process.WaitForExitAsync(grace.Token); }
                catch (OperationCanceledException) { process.Kill(entireProcessTree: true); }
            }
        }
        catch (Exception error) when (!stoppingToken.IsCancellationRequested)
        {
            logger.LogError(error, "Worker failed; requesting Service Control Manager recovery");
            // Job disposal kills Python and all model subprocesses before SCM restarts us.
            job.Dispose();
            try { if (!process.HasExited) process.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
            Environment.Exit(1);
        }
        finally
        {
            try { if (process.Id > 0 && !process.HasExited) process.Kill(entireProcessTree: true); }
            catch (InvalidOperationException) { }
        }
    }
}

// Closing the supervisor, even forcibly, must not leave model workers behind.
sealed class ProcessJob : IDisposable
{
    private IntPtr handle;
    public ProcessJob()
    {
        handle = CreateJobObject(IntPtr.Zero, null);
        if (handle == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
        var limits = new ExtendedLimits();
        limits.Basic.LimitFlags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        int size = Marshal.SizeOf<ExtendedLimits>();
        IntPtr memory = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(limits, memory, false);
            if (!SetInformationJobObject(handle, 9, memory, (uint)size))
                throw new System.ComponentModel.Win32Exception();
        }
        finally { Marshal.FreeHGlobal(memory); }
    }
    public void Assign(Process process)
    {
        if (!AssignProcessToJobObject(handle, process.Handle)) throw new System.ComponentModel.Win32Exception();
    }
    public void Dispose() { if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; } }
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits {
        public long PerProcessUserTime, PerJobUserTime; public uint LimitFlags;
        public UIntPtr MinWorkingSet, MaxWorkingSet; public uint ActiveProcessLimit;
        public UIntPtr Affinity; public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
        public BasicLimits Basic; public IoCounters Io;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemory, PeakJobMemory;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateJobObject(IntPtr attributes, string? name);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetInformationJobObject(IntPtr job, int type, IntPtr info, uint length);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
}
