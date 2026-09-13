// Synthetic Windows process for updater coordination tests, not LeyLineBook.
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

class UpdateProcessFixture {
    [STAThread]
    static int Main(string[] args) {
        string root = Environment.GetEnvironmentVariable("LEYLINEBOOK_UPGRADE_FIXTURE_DIR");
        if (String.IsNullOrEmpty(root) || !Directory.Exists(root)) return 99;
        File.WriteAllText(Path.Combine(root, "fixture-pid.txt"), Process.GetCurrentProcess().Id.ToString());
        string mode = Environment.GetEnvironmentVariable("LEYLINEBOOK_UPGRADE_FIXTURE_MODE");
        if (mode == "startup_failure") return 7;
        if (args.Length != 2 || args[0] != "--update-health-file") return 98;
        if (mode == "late") Thread.Sleep(35000);
        if (mode == "healthy" || mode == "late") File.WriteAllText(args[1], "ready\n");
        Stopwatch watch = Stopwatch.StartNew();
        while (!File.Exists(Path.Combine(root, "fixture-stop.txt")) && watch.Elapsed.TotalSeconds < 90) Thread.Sleep(50);
        return 0;
    }
}
