using System;
using System.Runtime.InteropServices;

namespace AgentRadar.Windows.Ui;

internal static class ReminderSound
{
    private const uint Alias = 0x00010000;
    private const uint Async = 0x0001;
    private const uint NoDefault = 0x0002;

    [DllImport("winmm.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool PlaySound(string? name, IntPtr module, uint flags);

    public static bool TryPlay()
    {
        try { return PlaySound("SystemAsterisk", IntPtr.Zero, Alias | Async | NoDefault); }
        catch (DllNotFoundException) { return false; }
        catch (EntryPointNotFoundException) { return false; }
    }
}
