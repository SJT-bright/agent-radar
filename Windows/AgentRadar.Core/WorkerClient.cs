using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace AgentRadar.Core;

public static class BridgeRequestGuard {
    static readonly HashSet<string> BlockedKeys = new(StringComparer.OrdinalIgnoreCase) {
        "native_helper", "nativeHelper", "helper_exe", "executable", "worker", "command", "args", "arguments",
        "env", "environment", "AGENT_RADAR_NATIVE_HELPER", "AGENT_RADAR_SUPPORT_DIR", "AGENT_RADAR_BRIDGE_SUPERVISOR",
    };
    static readonly string[] EnvironmentPassthrough = [
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL", "PYTHONUTF8", "PYTHONIOENCODING", "SYSTEMDRIVE",
    ];

    public static Dictionary<string, object?> Sanitize(IReadOnlyDictionary<string, object?> request) {
        var clean = new Dictionary<string, object?>(StringComparer.Ordinal);
        foreach (var pair in request) {
            if (BlockedKeys.Contains(pair.Key)) continue;
            if (pair.Value is string or bool or double or int or long) clean[pair.Key] = pair.Value;
        }
        return clean;
    }

    public static byte[]? Encode(IReadOnlyDictionary<string, object?> request, out string? error) {
        var json = JsonSerializer.Serialize(Sanitize(request),new JsonSerializerOptions{Encoder=System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping});
        var bytes = Encoding.UTF8.GetBytes(json);
        if (bytes.Length > 32768) {
            error = "request_too_large";
            return null;
        }
        error = null;
        return bytes;
    }

    public static ProcessStartInfo Create(string workerExe, string nativeHelperExe, string arguments) {
        var info = new ProcessStartInfo(workerExe, arguments) {
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        info.Environment.Clear();
        foreach (var name in EnvironmentPassthrough) {
            var value = Environment.GetEnvironmentVariable(name);
            if (!string.IsNullOrEmpty(value)) info.Environment[name] = value;
        }
        info.Environment["PYTHONUTF8"] = "1";
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        info.Environment["PYTHONDONTWRITEBYTECODE"] = "1";
        info.Environment["PYTHONNOUSERSITE"] = "1";
        if (!string.IsNullOrWhiteSpace(nativeHelperExe)) info.Environment["AGENT_RADAR_NATIVE_HELPER"] = nativeHelperExe;
        var support = Environment.GetEnvironmentVariable("AGENT_RADAR_SUPPORT_DIR");
        if (!string.IsNullOrWhiteSpace(support)) info.Environment["AGENT_RADAR_SUPPORT_DIR"] = support;
        info.Environment.Remove("AGENT_RADAR_BRIDGE_SUPERVISOR");
        return info;
    }
}

public sealed class WorkerClient : IWorkerClient {
    readonly string _workerExe;
    readonly string _nativeHelperExe;
    Process? _watch;
    CancellationTokenSource? _watchCancel;
    int _restarts;
    public event Action<CollectorSnapshot>? Snapshot;
    public event Action<string>? Fault;

    public WorkerClient(string workerExe, string nativeHelperExe) {
        _workerExe = workerExe ?? "";
        _nativeHelperExe = nativeHelperExe ?? "";
    }

    public async Task StartAsync(CancellationToken cancellationToken = default) {
        if (_watch is { HasExited: false }) return;
        if (!CanStart()) {
            Fault?.Invoke("collector_unavailable");
            return;
        }
        _watchCancel = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        await LaunchWatch(_watchCancel.Token);
    }

    public void Stop() {
        _watchCancel?.Cancel();
        Kill(_watch);
        _watch = null;
    }

    public async Task<BridgeResult> RunBridgeAsync(Dictionary<string, object?> request, CancellationToken cancellationToken = default) {
        var bytes = BridgeRequestGuard.Encode(request, out var error);
        if (bytes == null) return new BridgeResult { Code = error ?? "request_too_large", Attempted = false };
        if (!CanStart()) return new BridgeResult { Code = "bridge_unavailable", Attempted = false };
        Process? process = null;
        try {
            process = Process.Start(BridgeRequestGuard.Create(_workerExe, _nativeHelperExe, "--bridge"));
            if (process == null) return new BridgeResult { Code = "bridge_unavailable", Attempted = false };
            await process.StandardInput.BaseStream.WriteAsync(bytes, cancellationToken);
            process.StandardInput.Close();
            _ = Drain(process.StandardError);
            var read = ReadBounded(process.StandardOutput,8192,cancellationToken);
            var finished = await Task.WhenAny(read, Task.Delay(TimeSpan.FromSeconds(40), cancellationToken));
            if (finished != read) {
                Kill(process);
                return new BridgeResult { Code = "bridge_timeout", Attempted = true };
            }
            process.WaitForExit(2000);
            var output = (await read)?.Trim()??"";
            if (output.Length == 0 || output.Length > 8192 || !output.StartsWith('{') || !output.EndsWith('}'))
                return new BridgeResult { Code = "bridge_error", Attempted = true };
            var result = JsonSerializer.Deserialize<BridgeResult>(output);
            if (result == null || string.IsNullOrWhiteSpace(result.Code)) return new BridgeResult { Code = "bridge_error", Attempted = true };
            if (result.DiagnosticSummary != null) result.DiagnosticSummary = SafeSummary(result.DiagnosticSummary);
            return result;
        } catch (Exception ex) when (ex is IOException or JsonException or InvalidOperationException or OperationCanceledException or System.ComponentModel.Win32Exception) {
            Kill(process);
            return new BridgeResult { Code = ex is OperationCanceledException ? "bridge_timeout" : "bridge_unavailable", Attempted = process!=null };
        }
    }

    public void Dispose() => Stop();

    bool CanStart() => !string.IsNullOrWhiteSpace(_workerExe) && File.Exists(_workerExe);

    async Task LaunchWatch(CancellationToken token) {
        try {
            var process = Process.Start(BridgeRequestGuard.Create(_workerExe, _nativeHelperExe, "--watch"));
            if (process == null) {
                Fault?.Invoke("collector_unavailable");
                return;
            }
            _ = Drain(process.StandardError);
            _watch = process;
            _ = Task.Run(() => ReadWatch(process, token), token);
        } catch (Exception ex) when (ex is IOException or InvalidOperationException or System.ComponentModel.Win32Exception) {
            Fault?.Invoke("collector_unavailable");
            await Task.CompletedTask;
        }
    }

    async Task ReadWatch(Process process, CancellationToken token) {
        try {
            while (!token.IsCancellationRequested) {
                var line = await ReadBounded(process.StandardOutput,1_000_000,token,true);
                if (line == null) break;
                if (line.Length is 0 or > 1_000_000) {
                    if (line.Length > 1_000_000) Fault?.Invoke("snapshot_too_large");
                    continue;
                }
                try {
                    var snapshot = JsonSerializer.Deserialize<CollectorSnapshot>(line);
                    if (snapshot != null) Snapshot?.Invoke(snapshot);
                } catch (JsonException) {
                    Fault?.Invoke("snapshot_unreadable");
                }
            }
            process.WaitForExit(1000);
            if (!token.IsCancellationRequested && process.ExitCode == 75 && _restarts < 3) {
                _restarts++;
                Fault?.Invoke("collector_restart");
                await LaunchWatch(token);
                return;
            }
            if (!token.IsCancellationRequested) Fault?.Invoke("collector_stopped");
        } catch (OperationCanceledException) {
        } catch (Exception ex) when (ex is IOException or InvalidOperationException or System.ComponentModel.Win32Exception) {
            Fault?.Invoke("collector_stopped");
        }
    }

    static void Kill(Process? process) {
        if (process == null) return;
        try {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
        } catch (Exception ex) when (ex is IOException or InvalidOperationException or System.ComponentModel.Win32Exception) {
        }
    }

    static async Task<string?> ReadBounded(StreamReader reader,int limit,CancellationToken token,bool line=false){var result=new StringBuilder();var chars=new char[1];while(await reader.ReadAsync(chars.AsMemory(),token)>0){if(line&&chars[0]=='\n')return result.ToString();if(result.Length>=limit)throw new IOException("output limit");result.Append(chars[0]);}return result.Length==0?null:result.ToString();}
    static async Task Drain(StreamReader reader){try{var buffer=new char[4096];while(await reader.ReadAsync(buffer.AsMemory())>0){}}catch{}}
    static string SafeSummary(string value) {
        var trimmed = value.Trim();
        if (trimmed.Length > 1000 || trimmed.Contains('/') || trimmed.Contains('\\') || trimmed.Contains('@')) return "";
        return trimmed;
    }
}
