using System;
using System.Text;
using System.Text.Json;

namespace AgentRadar.Windows;

internal static class NativeBridgeHost
{
    public static int Run()
    {
        try
        {
            using var stdin = Console.OpenStandardInput();
            var buffer = new byte[8193];
            var read = 0;
            while (read < buffer.Length)
            {
                var count = stdin.Read(buffer, read, buffer.Length - read);
                if (count <= 0) break;
                read += count;
            }
            if (read > 8192)
            {
                Write("{\"code\":\"request_too_large\",\"attempted\":false}");
                return 0;
            }
            var json = new UTF8Encoding(false).GetString(buffer, 0, read);
            var result = global::AgentRadar.Windows.Services.NativeAutomation.Run(json);
            if (string.IsNullOrWhiteSpace(result))
            {
                Write("{\"code\":\"empty_native_result\",\"attempted\":false}");
                return 0;
            }
            Write(result.Trim());
            return 0;
        }
        catch (Exception) {
            Write("{\"code\":\"native_bridge_fault\",\"attempted\":true}");return 1;
        }
    }

    private static void Write(string json)
    {
        var stdout = Console.OpenStandardOutput();
        var bytes = new UTF8Encoding(false).GetBytes(json + "\n");
        stdout.Write(bytes, 0, bytes.Length);
        stdout.Flush();
    }
}
