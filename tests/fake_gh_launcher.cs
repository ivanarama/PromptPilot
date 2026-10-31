// Test-only executable bridge for Python fake GH peers on Windows. No shell.
using System;
using System.Diagnostics;
using System.Linq;
using System.Text;
using System.Threading.Tasks;

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
            Arguments = "-X utf8 " + Quote(Environment.GetEnvironmentVariable("ROUTING_FAKE_SCRIPT")) + " "
                + string.Join(" ", args.Select(Quote)),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        using (var process = Process.Start(start)) {
            // gh's pipes carry UTF-8 bytes, not console text. The .NET and
            // Python defaults can be different Windows code pages. Decoding
            // here corrupts canonical bodies (and their hashes) before POST.
            var output = process.StandardOutput.BaseStream.CopyToAsync(Console.OpenStandardOutput());
            var error = process.StandardError.BaseStream.CopyToAsync(Console.OpenStandardError());
            if (args.Contains("--input")) Console.OpenStandardInput().CopyTo(process.StandardInput.BaseStream);
            process.StandardInput.Close();
            process.WaitForExit();
            Task.WaitAll(output, error);
            return process.ExitCode;
        }
    }
}
