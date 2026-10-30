// Test-only executable bridge for Python fake GH peers on Windows. No shell.
using System;
using System.Diagnostics;
using System.Linq;
using System.Text;

class FakeGitHubLauncher {
    static string Quote(string value) {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char letter in value) {
            if (letter == '\\') { slashes++; continue; }
            result.Append('\\', letter == '"' ? slashes * 2 + 1 : slashes);
            result.Append(letter);
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        result.Append('"');
        return result.ToString();
    }

    static int Main(string[] args) {
        var start = new ProcessStartInfo {
            FileName = Environment.GetEnvironmentVariable("ROUTING_FAKE_PYTHON"),
            Arguments = Quote(Environment.GetEnvironmentVariable("ROUTING_FAKE_SCRIPT")) + " "
                + string.Join(" ", args.Select(Quote)),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        using (var process = Process.Start(start)) {
            if (args.Contains("--input")) process.StandardInput.Write(Console.In.ReadToEnd());
            process.StandardInput.Close();
            Console.Out.Write(process.StandardOutput.ReadToEnd());
            Console.Error.Write(process.StandardError.ReadToEnd());
            process.WaitForExit();
            return process.ExitCode;
        }
    }
}
