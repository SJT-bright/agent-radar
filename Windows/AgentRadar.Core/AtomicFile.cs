namespace AgentRadar.Core;

static class AtomicFile {
    public static void Write(string directory, string name, byte[] data) {
        Directory.CreateDirectory(directory);
        var root = Path.GetFullPath(directory);
        var target = Path.GetFullPath(Path.Combine(root, name));
        if (!target.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.Ordinal))
            throw new InvalidOperationException("path_escape");
        if (File.Exists(target) && File.ResolveLinkTarget(target, false) != null)
            throw new InvalidOperationException("symlink_refused");
        var temp = Path.Combine(root, "." + name + "." + Guid.NewGuid().ToString("N") + ".tmp");
        var options = new FileStreamOptions {
            Mode = FileMode.CreateNew,
            Access = FileAccess.Write,
            Share = FileShare.None,
        };
        if (!OperatingSystem.IsWindows()) options.UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite;
        using (var stream = new FileStream(temp, options)) {
            stream.Write(data);
            stream.Flush(true);
        }
        File.Move(temp, target, overwrite: true);
    }
}
