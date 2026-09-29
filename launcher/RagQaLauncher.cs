/* ============================================================================
 *  RAG-QA.exe —— 一体化启动 / 关闭器
 *
 *  一个不到 100KB 的单文件 exe：双击就启动（Ollama + 后端 + 浏览器），
 *  关掉窗口就把后端和 Ollama 一起收掉；托盘常驻，随时打开页面或停止服务。
 *
 *  ## 为什么是「启动器」而不是把所有东西塞进一个 exe
 *
 *  真·单文件 exe 意味着把 CPython + torch + sentence-transformers + faiss +
 *  FastAPI 全打进一个二进制（PyInstaller/onefile 大约 1.5~3GB），而且
 *  torch 的 onefile 打包在 Windows 上极易在启动时解压失败；再算上
 *  Ollama 运行时（1.4GB）和模型权重，也依然得留在外部目录。
 *  换来的只是「一个文件」的观感，代价是启动慢、易碎、无法独立升级。
 *
 *  这里选择薄启动器：**编排逻辑仍然只有一份**（scripts\start.ps1 / stop.ps1，
 *  它们有完整的测试覆盖），exe 只负责「按需拉起、盯状态、优雅收尾」。
 *  体积 40KB 左右，启动瞬时，不依赖 Python，也不需要联网。
 *
 *  ## 为什么用 .NET Framework 4.x + csc.exe 编译
 *
 *  * Windows 10 1903+ / 11 自带 .NET Framework 4.8，**零运行时安装**；
 *    而 .NET 9 的 exe 要求目标机器装对应运行时，self-contained 又要 70MB。
 *  * 本机 `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe` 就能编译，
 *    不需要 .NET SDK、不需要 NuGet、不需要联网（这个项目本身要离线可用）。
 *  * 代价：编译器只支持 **C# 5** 语法，所以这里刻意不用字符串插值、
 *    空条件运算符 `?.`、表达式体成员等新语法。改动本文件时请遵守。
 *
 *  ## 几条踩过坑的工程约束
 *
 *  1. **不重定向子进程的 stdout**。受限环境里管道（named pipe）会被拒绝，
 *     直接导致启动失败。日志改由脚本自己重定向进文件，界面再读文件。
 *  2. **HTTP 探测必须关掉代理**（`req.Proxy = null`）。Windows 上一旦系统
 *     设了代理，`HttpWebRequest`/httpx 之流**连 127.0.0.1 都走代理**，
 *     结果永远是 502 —— 浏览器和 PowerShell 反而正常，极难排查。
 *  3. **停止服务一律交给 stop.ps1**，绝不用 Process.Kill 冒充停止：
 *     后端与 Ollama 是孙子进程，杀父进程会留下吃显存的孤儿；
 *     stop.ps1 里有 PID / 路径 / 端口三重定位逻辑。
 *  4. 本文件必须是 **UTF-8 带 BOM**：csc.exe 靠 BOM 判断源文件编码，
 *     没有 BOM 时会按系统 ANSI(936) 读，中文字符串会变成乱码。
 * ==========================================================================*/

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace RagQaLauncher
{
    // ======================================================================
    // 命令行解析
    // ======================================================================
    internal class Options
    {
        public string Mode = "gui";      // gui | status | start | stop | help
        public int Port = 8000;
        public bool NoBrowser;
        public bool KeepOllama;
        public bool Json;

        public static Options Parse(string[] args)
        {
            Options o = new Options();
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i];
                switch (a)
                {
                    case "--status": o.Mode = "status"; break;
                    case "--start": o.Mode = "start"; break;
                    case "--stop": o.Mode = "stop"; break;
                    case "--json": o.Json = true; break;
                    case "--no-browser": o.NoBrowser = true; break;
                    case "--keep-ollama": o.KeepOllama = true; break;
                    case "-h":
                    case "--help": o.Mode = "help"; break;
                    case "--port":
                        if (i + 1 < args.Length)
                        {
                            int parsed;
                            if (int.TryParse(args[++i], out parsed)) o.Port = parsed;
                        }
                        break;
                }
            }
            return o;
        }
    }

    // ======================================================================
    // 控制台
    // ======================================================================
    internal static class Con
    {
        [DllImport("kernel32.dll")]
        private static extern bool AttachConsole(int dwProcessId);

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetStdHandle(int nStdHandle);

        private const int ATTACH_PARENT_PROCESS = -1;
        private const int STD_OUTPUT_HANDLE = -11;

        /// <summary>
        /// 以 winexe 编译的程序默认没有控制台。--status/--start/--stop 被脚本调用时
        /// 必须把输出交回去，这里同时兜住两种调用方式。
        ///
        /// **判断顺序很关键**：先看标准输出是不是已经被重定向（调用方写了 `> file`，
        /// 或被管道接住），只有在**没有**重定向时才 AttachConsole。
        /// 因为 AttachConsole 会把进程的标准句柄换成目标控制台的句柄 ——
        /// 先附着再输出，重定向到文件的内容会凭空消失（实测踩过：
        /// `RAG-QA.exe --status --json > out.txt` 得到 0 字节，退出码却是 0）。
        /// </summary>
        public static void PrepareOutput()
        {
            IntPtr handle = GetStdHandle(STD_OUTPUT_HANDLE);
            bool redirected = handle != IntPtr.Zero && handle != new IntPtr(-1);
            if (!redirected) AttachConsole(ATTACH_PARENT_PROCESS);

            UTF8Encoding utf8 = new UTF8Encoding(false);

            // 先无条件把 Console.Out/Error 换成 UTF-8 写入器。
            // **不要**把 Console.OutputEncoding 和 SetOut 放进同一段 try：
            // 没有控制台时 OutputEncoding 的 setter 会抛异常，异常一旦发生，
            // 后面的 SetOut 就被跳过，输出于是按系统 OEM 代码页(936)编码 ——
            // 重定向到文件里的中文全变乱码，而且看不出哪里做错了。
            try
            {
                StreamWriter stdout = new StreamWriter(Console.OpenStandardOutput(), utf8);
                stdout.AutoFlush = true;
                Console.SetOut(stdout);
                StreamWriter stderr = new StreamWriter(Console.OpenStandardError(), utf8);
                stderr.AutoFlush = true;
                Console.SetError(stderr);
            }
            catch
            {
                // 既没有控制台也没有重定向（例如资源管理器里带参数运行）：丢弃输出即可
            }

            try { Console.OutputEncoding = utf8; }
            catch { }
        }
    }

    // ======================================================================
    // 路径解析
    // ======================================================================
    internal class AppLayout
    {
        public string Root;
        public string StartScript;
        public string StopScript;
        public string Python;
        public string LogsDir;
        public string BackendLog;
        public string StartLog;
        public string StopLog;
        public string LauncherLog;
        public string StateFile;

        /// <summary>
        /// exe 放在项目根目录，但用户也可能把它丢进子目录或从任何位置调用，
        /// 所以向上找「有 scripts\start.ps1 的那一层」作为项目根。
        /// </summary>
        public static AppLayout Discover()
        {
            AppLayout l = new AppLayout();
            string dir = AppDomain.CurrentDomain.BaseDirectory;
            DirectoryInfo info = new DirectoryInfo(dir);
            string found = null;
            while (info != null)
            {
                if (File.Exists(Path.Combine(info.FullName, "scripts", "start.ps1")))
                {
                    found = info.FullName;
                    break;
                }
                info = info.Parent;
            }
            l.Root = found != null ? found : dir;
            l.StartScript = Path.Combine(l.Root, "scripts", "start.ps1");
            l.StopScript = Path.Combine(l.Root, "scripts", "stop.ps1");
            l.Python = Path.Combine(l.Root, ".venv", "Scripts", "python.exe");

            // 数据目录允许被 RAG_DATA_DIR 覆盖（测试隔离用），与后端保持一致
            string dataDir = Environment.GetEnvironmentVariable("RAG_DATA_DIR");
            if (dataDir == null || dataDir.Trim().Length == 0)
                dataDir = Path.Combine(l.Root, "data");
            else
                dataDir = Path.GetFullPath(dataDir);

            l.LogsDir = Path.Combine(dataDir, "logs");
            l.BackendLog = Path.Combine(l.LogsDir, "backend.log");
            l.StartLog = Path.Combine(l.LogsDir, "start.log");
            l.StopLog = Path.Combine(l.LogsDir, "stop.log");
            l.LauncherLog = Path.Combine(l.LogsDir, "launcher.log");
            l.StateFile = Path.Combine(l.LogsDir, "launcher-state.json");
            return l;
        }

        public bool ReadyToStart()
        {
            return File.Exists(StartScript) && File.Exists(Python);
        }
    }

    // ======================================================================
    // 健康探测
    // ======================================================================
    internal class Health
    {
        public bool Ok;
        public int Port;
        public string Status = "";
        public string Model = "";
        public bool Ollama;
        public int Documents;
        public int Chunks;
        public string Raw = "";

        public string Url
        {
            get { return "http://127.0.0.1:" + Port; }
        }
    }

    internal static class Probe
    {
        /// <summary>本机 HTTP 探测：显式禁用代理（见文件头第 2 条）。</summary>
        public static string HttpGet(string url, int timeoutMs)
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
                req.Proxy = null;
                req.Timeout = timeoutMs;
                req.ReadWriteTimeout = timeoutMs;
                req.Method = "GET";
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    if ((int)resp.StatusCode != 200) return null;
                    using (StreamReader reader = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                        return reader.ReadToEnd();
                }
            }
            catch
            {
                return null;
            }
        }

        public static bool TcpOpen(int port, int timeoutMs)
        {
            try
            {
                using (TcpClient client = new TcpClient())
                {
                    IAsyncResult ar = client.BeginConnect("127.0.0.1", port, null, null);
                    if (!ar.AsyncWaitHandle.WaitOne(timeoutMs)) return false;
                    client.EndConnect(ar);
                    return true;
                }
            }
            catch
            {
                return false;
            }
        }

        public static Health Check(int port, int timeoutMs)
        {
            string json = HttpGet("http://127.0.0.1:" + port + "/api/health", timeoutMs);
            if (json == null) return null;

            Health h = new Health();
            h.Ok = true;
            h.Port = port;
            h.Raw = json;
            try
            {
                JavaScriptSerializer ser = new JavaScriptSerializer();
                Dictionary<string, object> map = (Dictionary<string, object>)ser.DeserializeObject(json);
                h.Status = Text(map, "status");
                h.Model = Text(map, "llm_model");
                h.Documents = Number(map, "document_count");
                h.Chunks = Number(map, "chunk_count");
                h.Ollama = Bool(map, "ollama_reachable");
            }
            catch
            {
                // 结构变了也不影响「服务在跑」这个判断
            }
            return h;
        }

        private static string Text(Dictionary<string, object> map, string key)
        {
            object value;
            return map.TryGetValue(key, out value) && value != null ? value.ToString() : "";
        }

        private static int Number(Dictionary<string, object> map, string key)
        {
            object value;
            if (map.TryGetValue(key, out value) && value != null)
            {
                try { return Convert.ToInt32(value); }
                catch { return 0; }
            }
            return 0;
        }

        private static bool Bool(Dictionary<string, object> map, string key)
        {
            object value;
            if (map.TryGetValue(key, out value) && value != null)
            {
                try { return Convert.ToBoolean(value); }
                catch { return false; }
            }
            return false;
        }

        /// <summary>在候选端口里找正在运行的本项目服务（含状态文件里记过的端口）。</summary>
        public static Health FindRunning(AppLayout layout, int preferredPort)
        {
            List<int> ports = new List<int>();
            ports.Add(preferredPort);
            int remembered = ReadStatePort(layout);
            if (remembered > 0 && remembered != preferredPort) ports.Add(remembered);
            for (int p = 8000; p <= 8019; p++)
                if (!ports.Contains(p)) ports.Add(p);

            foreach (int port in ports)
            {
                Health h = Check(port, 700);
                if (h != null && h.Ok) return h;
            }
            return null;
        }

        public static int ReadStatePort(AppLayout layout)
        {
            try
            {
                if (!File.Exists(layout.StateFile)) return 0;
                string text = File.ReadAllText(layout.StateFile, Encoding.UTF8);
                JavaScriptSerializer ser = new JavaScriptSerializer();
                Dictionary<string, object> map = (Dictionary<string, object>)ser.DeserializeObject(text);
                object port;
                if (map.TryGetValue("port", out port) && port != null) return Convert.ToInt32(port);
            }
            catch { }
            return 0;
        }
    }

    // ======================================================================
    // 启动 / 停止（一律委托给 scripts 下的脚本）
    // ======================================================================
    internal static class Service
    {
        /// <summary>
        /// 拼 PowerShell 字符串字面量。
        ///
        /// **必须用单引号**：这些片段最终会被塞进 `-Command "…"` 里，
        /// 内部再用双引号会和外层引号提前配对，命令被截断成半句 ——
        /// 表现是子进程安静地什么都没干（连重定向的日志文件都不会生成）。
        /// 路径里的单引号按 PowerShell 规则写成两个。
        /// </summary>
        public static string PsQuote(string value)
        {
            return "'" + value.Replace("'", "''") + "'";
        }

        /// <summary>
        /// 把一段 PowerShell 代码包进转录（transcript），输出落进日志文件。
        ///
        /// **为什么不用 `*> 文件`**：start.ps1 是在 `$ErrorActionPreference='Stop'`
        /// 下前台运行 uvicorn 的，而 PS 5.1 会把「被重定向的原生命令 stderr 输出」
        /// 当成 NativeCommandError 终止性错误 —— uvicorn 刚写第一行日志，
        /// 整个脚本就被弹飞，端口根本没监听。实测现象极具误导性：
        /// 日志停在「按 Ctrl+C 停止服务」、进程消失、而退出码是 0。
        /// Start-Transcript 走宿主输出通道，不碰原生命令的流，因此安全。
        ///
        /// **不要给 powershell.exe 传 `-WindowStyle Hidden`**：那样 PowerShell 自己
        /// 就没有控制台了，start.ps1 会卡在启动阶段（连转录文件都不会创建），
        /// 而进程还活着 —— 极难判断。隐藏窗口交给 ProcessStartInfo.WindowStyle
        /// （等价于 `Start-Process -WindowStyle Hidden`），进程照样有隐藏控制台。
        /// </summary>
        private static string WithTranscript(string payload, string logPath)
        {
            return "Start-Transcript -Path " + PsQuote(logPath) + " -Force | Out-Null; " +
                   payload +
                   "; Stop-Transcript | Out-Null";
        }

        /// <summary>
        /// 拉起后端。start.ps1 会自己处理「Ollama 没起来就先起来」「端口被占」
        /// 「等健康检查通过再开浏览器」这些事，前台阻塞运行 = 服务生命周期。
        ///
        /// 输出交给 PowerShell 自己收集，**不要**用 Process 的管道：
        /// 受限环境下 named pipe 会被拒绝，管道一旦失败服务就起不来。
        /// </summary>
        public static Process Start(AppLayout layout, int port, bool noBrowser, bool keepOllama)
        {
            string payload = "& " + PsQuote(layout.StartScript) + " -Port " + port;
            if (noBrowser) payload += " -NoBrowser";
            if (keepOllama) payload += " -KeepOllama";

            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = "powershell.exe";
            psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -Command \"" +
                            WithTranscript(payload, layout.StartLog) + "\"";
            psi.WorkingDirectory = layout.Root;
            psi.UseShellExecute = true;      // 让系统给子进程一个（隐藏的）控制台，避免句柄/管道问题
            psi.WindowStyle = ProcessWindowStyle.Hidden;

            Directory.CreateDirectory(layout.LogsDir);
            Log.Write(layout, "启动服务: port=" + port + (noBrowser ? " -NoBrowser" : ""));
            return Process.Start(psi);
        }

        /// <summary>停止后端与（可选）Ollama，返回是否在超时内收干净。</summary>
        public static bool Stop(AppLayout layout, int port, bool keepOllama, int waitMs)
        {
            string payload = "& " + PsQuote(layout.StopScript) + " -Port " + port;
            if (keepOllama) payload += " -KeepOllama";

            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = "powershell.exe";
            psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -Command \"" +
                            WithTranscript(payload, layout.StopLog) + "\"";
            psi.WorkingDirectory = layout.Root;
            psi.UseShellExecute = true;
            psi.WindowStyle = ProcessWindowStyle.Hidden;

            Log.Write(layout, "停止服务: port=" + port + (keepOllama ? " -KeepOllama" : ""));
            try
            {
                Process p = Process.Start(psi);
                p.WaitForExit(waitMs);
            }
            catch (Exception ex)
            {
                Log.Write(layout, "停止脚本执行失败: " + ex.Message);
                return false;
            }

            // 以端口是否关闭为准（脚本内部也做了同样的事，这里独立验证一遍）
            for (int i = 0; i < 20; i++)
            {
                if (!Probe.TcpOpen(port, 300)) { ClearState(layout); return true; }
                Thread.Sleep(250);
            }

            // stop.ps1 没能收干净？兜底强杀。
            // 受限账户下脚本可能查不到进程（Get-CimInstance / Get-NetTCPConnection
            // 会「拒绝访问」），那种情况下用户点「停止」将毫无反应，
            // 所以这里再用 netstat 找一次监听者，按进程树结束。
            int pid = FindListenerPid(port);
            if (pid > 0)
            {
                Log.Write(layout, "stop.ps1 未收干净，兜底强杀 PID " + pid);
                ForceKillTree(pid);
                for (int i = 0; i < 12; i++)
                {
                    if (!Probe.TcpOpen(port, 300)) break;
                    Thread.Sleep(250);
                }
            }
            ClearState(layout);
            return !Probe.TcpOpen(port, 300);
        }

        /// <summary>
        /// 按端口找监听进程。用 netstat -ano 解析而不是 Get-NetTCPConnection：
        /// 后者在受限账户下会直接抛「拒绝访问」，整条兜底路径形同虚设。
        /// </summary>
        public static int FindListenerPid(int port)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo();
                psi.FileName = "netstat.exe";
                psi.Arguments = "-ano -p TCP";
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;   // 只读不写：受限的是管道写入方向
                using (Process p = Process.Start(psi))
                {
                    string output = p.StandardOutput.ReadToEnd();
                    p.WaitForExit(5000);
                    string needle = ":" + port + " ";
                    string[] lines = output.Replace("\r\n", "\n").Split('\n');
                    foreach (string raw in lines)
                    {
                        string line = raw.Trim();
                        if (line.Length == 0) continue;
                        if (line.IndexOf("LISTENING", StringComparison.OrdinalIgnoreCase) < 0) continue;
                        if (line.IndexOf(needle, StringComparison.Ordinal) < 0) continue;
                        string[] parts = line.Split(new char[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
                        int pid;
                        if (parts.Length >= 5 && int.TryParse(parts[parts.Length - 1], out pid)) return pid;
                    }
                }
            }
            catch { }
            return 0;
        }

        /// <summary>按进程树强杀（连带 uvicorn 之类的子进程）。</summary>
        public static void ForceKillTree(int pid)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo();
                psi.FileName = "taskkill.exe";
                psi.Arguments = "/PID " + pid + " /T /F";
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                Process p = Process.Start(psi);
                p.WaitForExit(8000);
            }
            catch { }
        }

        public static void WriteState(AppLayout layout, int port, int pid)
        {
            try
            {
                Directory.CreateDirectory(layout.LogsDir);
                string json = "{\"port\":" + port + ",\"pid\":" + pid + ",\"started_at\":\"" +
                              DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "\"}";
                File.WriteAllText(layout.StateFile, json, new UTF8Encoding(false));
            }
            catch { }
        }

        public static void ClearState(AppLayout layout)
        {
            try { if (File.Exists(layout.StateFile)) File.Delete(layout.StateFile); }
            catch { }
        }
    }

    // ======================================================================
    // 启动器自己的日志（与后端日志分开，出问题时好对照）
    // ======================================================================
    internal static class Log
    {
        private static readonly object Gate = new object();

        public static void Write(AppLayout layout, string message)
        {
            try
            {
                lock (Gate)
                {
                    Directory.CreateDirectory(layout.LogsDir);
                    string line = DateTime.Now.ToString("HH:mm:ss") + "  " + message + Environment.NewLine;
                    File.AppendAllText(layout.LauncherLog, line, new UTF8Encoding(false));
                }
            }
            catch { }
        }

        /// <summary>读取文件尾部若干行（服务进程同时持有该文件，必须允许共享读）。</summary>
        public static string Tail(string path, int lines, int maxBytes)
        {
            try
            {
                if (!File.Exists(path)) return "";
                FileInfo info = new FileInfo(path);
                if (info.Length == 0) return "";
                long start = Math.Max(0, info.Length - maxBytes);
                byte[] buffer;
                using (FileStream fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                {
                    fs.Seek(start, SeekOrigin.Begin);
                    buffer = new byte[info.Length - start];
                    int read = fs.Read(buffer, 0, buffer.Length);
                    if (read < buffer.Length) Array.Resize(ref buffer, read);
                }
                string text = Encoding.UTF8.GetString(buffer).Replace("\r\n", "\n");
                string[] all = text.Split('\n');
                int skip = Math.Max(0, all.Length - lines);
                StringBuilder sb = new StringBuilder();
                for (int i = skip; i < all.Length; i++)
                {
                    if (all[i].Length == 0) continue;
                    sb.AppendLine(all[i]);
                }
                return sb.ToString();
            }
            catch
            {
                return "";
            }
        }
    }

    // ======================================================================
    // 主程序
    // ======================================================================
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            Options options = Options.Parse(args);

            if (options.Mode != "gui")
            {
                Con.PrepareOutput();
                return Headless(options);
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            // 单实例：第二次双击只是把已有的窗口叫到前台，不再起一个后端
            bool created;
            using (Mutex mutex = new Mutex(true, "RagQaLauncher.SingleInstance", out created))
            {
                if (!created)
                {
                    try
                    {
                        EventWaitHandle handle = EventWaitHandle.OpenExisting("RagQaLauncher.Show");
                        handle.Set();
                    }
                    catch { }
                    return 0;
                }

                using (EventWaitHandle showSignal = new EventWaitHandle(false, EventResetMode.AutoReset, "RagQaLauncher.Show"))
                using (MainForm form = new MainForm(options, showSignal))
                {
                    Application.Run(form);
                }
            }
            return 0;
        }

        // ------------------------------------------------------------------
        private static int Headless(Options options)
        {
            AppLayout layout = AppLayout.Discover();

            if (options.Mode == "help")
            {
                Console.WriteLine("RAG-QA.exe —— 离线 RAG 文档问答 一体化启动器");
                Console.WriteLine();
                Console.WriteLine("  RAG-QA.exe                    打开图形面板（默认）");
                Console.WriteLine("  RAG-QA.exe --start            启动服务（后台，不弹窗口）");
                Console.WriteLine("  RAG-QA.exe --stop             停止服务（含 Ollama）");
                Console.WriteLine("  RAG-QA.exe --status           打印状态与路径自检");
                Console.WriteLine();
                Console.WriteLine("  可选参数：--port N  --no-browser  --keep-ollama  --json");
                return 0;
            }

            if (options.Mode == "status") return Status(layout, options);

            if (options.Mode == "start")
            {
                Health running = Probe.FindRunning(layout, options.Port);
                if (running != null)
                {
                    Console.WriteLine("服务已在运行：" + running.Url);
                    if (!options.NoBrowser) OpenBrowser(running.Url);
                    return 0;
                }
                if (!layout.ReadyToStart())
                {
                    Console.WriteLine("[FAIL] 环境不完整，请先运行 scripts\\prepare.cmd");
                    Console.WriteLine("       缺少：" + MissingText(layout));
                    return 1;
                }

                Console.WriteLine("正在启动（首次冷启动需要十几秒）...");
                Process child = Service.Start(layout, options.Port, true, options.KeepOllama);
                Service.WriteState(layout, options.Port, child != null ? child.Id : 0);

                Health health = WaitForHealth(options.Port, 120000);
                if (health == null)
                {
                    Console.WriteLine("[FAIL] 启动超时，请查看 " + layout.StartLog);
                    return 1;
                }
                Console.WriteLine("已就绪：" + health.Url + "  （模型 " + health.Model + "，知识库 " +
                                  health.Documents + " 个文档 / " + health.Chunks + " 个分块）");
                if (!options.NoBrowser) OpenBrowser(health.Url);
                return 0;
            }

            if (options.Mode == "stop")
            {
                Console.WriteLine("正在停止服务...");
                bool ok = Service.Stop(layout, options.Port, options.KeepOllama, 60000);
                Console.WriteLine(ok ? "[OK] 已停止" : "[WARN] 端口 " + options.Port + " 仍在监听，请查看 " + layout.StopLog);
                string tail = Log.Tail(layout.StopLog, 12, 16 * 1024);
                if (tail.Length > 0) Console.WriteLine(tail.TrimEnd());
                return ok ? 0 : 1;
            }

            return 0;
        }

        // ------------------------------------------------------------------
        private static int Status(AppLayout layout, Options options)
        {
            Health running = Probe.FindRunning(layout, options.Port);
            bool ollama = Probe.TcpOpen(11434, 400);

            if (options.Json)
            {
                StringBuilder sb = new StringBuilder();
                sb.Append("{");
                sb.Append("\"root\":").Append(Json(layout.Root)).Append(",");
                sb.Append("\"start_script\":").Append(Json(File.Exists(layout.StartScript) ? "ok" : "missing")).Append(",");
                sb.Append("\"stop_script\":").Append(Json(File.Exists(layout.StopScript) ? "ok" : "missing")).Append(",");
                sb.Append("\"python\":").Append(Json(File.Exists(layout.Python) ? "ok" : "missing")).Append(",");
                sb.Append("\"logs_dir\":").Append(Json(layout.LogsDir)).Append(",");
                sb.Append("\"ready\":").Append(layout.ReadyToStart() ? "true" : "false").Append(",");
                sb.Append("\"running\":").Append(running != null ? "true" : "false").Append(",");
                sb.Append("\"port\":").Append(running != null ? running.Port : 0).Append(",");
                sb.Append("\"url\":").Append(Json(running != null ? running.Url : "")).Append(",");
                sb.Append("\"model\":").Append(Json(running != null ? running.Model : "")).Append(",");
                sb.Append("\"documents\":").Append(running != null ? running.Documents : 0).Append(",");
                sb.Append("\"chunks\":").Append(running != null ? running.Chunks : 0).Append(",");
                sb.Append("\"ollama\":").Append(ollama ? "true" : "false");
                sb.Append("}");
                // 同时落一份到 data\logs：PowerShell 不等待 GUI 程序，
                // 调用方（脚本、测试、排错）可以稳定地从文件读取结果。
                try
                {
                    Directory.CreateDirectory(layout.LogsDir);
                    File.WriteAllText(Path.Combine(layout.LogsDir, "launcher-status.json"),
                        sb.ToString(), new UTF8Encoding(false));
                }
                catch { }
                Console.WriteLine(sb.ToString());
                return layout.ReadyToStart() ? 0 : 1;
            }

            Console.WriteLine("项目根目录 : " + layout.Root);
            Console.WriteLine("启动脚本   : " + Mark(File.Exists(layout.StartScript)) + " " + layout.StartScript);
            Console.WriteLine("停止脚本   : " + Mark(File.Exists(layout.StopScript)) + " " + layout.StopScript);
            Console.WriteLine("虚拟环境   : " + Mark(File.Exists(layout.Python)) + " " + layout.Python);
            Console.WriteLine("日志目录   : " + layout.LogsDir);
            Console.WriteLine("Ollama     : " + (ollama ? "运行中 (11434)" : "未运行"));
            Console.WriteLine("服务状态   : " + (running != null
                ? "运行中 " + running.Url + " · 模型 " + running.Model + " · " + running.Documents +
                  " 个文档 / " + running.Chunks + " 个分块"
                : "未运行"));
            Console.WriteLine("可否启动   : " + (layout.ReadyToStart() ? "是" : "否，缺少 " + MissingText(layout)));
            return layout.ReadyToStart() ? 0 : 1;
        }

        private static string MissingText(AppLayout layout)
        {
            StringBuilder sb = new StringBuilder();
            if (!File.Exists(layout.Python)) sb.Append(".venv\\Scripts\\python.exe ");
            if (!File.Exists(layout.StartScript)) sb.Append("scripts\\start.ps1 ");
            return sb.ToString().Trim();
        }

        private static string Mark(bool ok)
        {
            return ok ? "[OK]  " : "[缺失]";
        }

        private static string Json(string value)
        {
            if (value == null) return "\"\"";
            return "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
        }

        private static Health WaitForHealth(int port, int timeoutMs)
        {
            Stopwatch watch = Stopwatch.StartNew();
            while (watch.ElapsedMilliseconds < timeoutMs)
            {
                Health health = Probe.Check(port, 1500);
                if (health != null && health.Ok) return health;
                Thread.Sleep(500);
            }
            return null;
        }

        public static void OpenBrowser(string url)
        {
            try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
            catch { }
        }
    }

    // ======================================================================
    // 图形面板
    // ======================================================================
    internal class MainForm : Form
    {
        private readonly Options _options;
        private readonly AppLayout _layout;
        private readonly EventWaitHandle _showSignal;

        private Label _stateLabel;
        private Label _detailLabel;
        private Label _kbLabel;
        private TextBox _logBox;
        private Button _startButton;
        private Button _stopButton;
        private Button _openButton;
        private CheckBox _minimizeToTray;
        private CheckBox _keepOllama;
        private NotifyIcon _tray;
        private System.Windows.Forms.Timer _timer;
        private Health _health;
        private Process _child;
        private bool _busy;
        private bool _exiting;

        public MainForm(Options options, EventWaitHandle showSignal)
        {
            _options = options;
            _layout = AppLayout.Discover();
            _showSignal = showSignal;

            BuildUi();
            Thread watcher = new Thread(WatchShowSignal);
            watcher.IsBackground = true;
            watcher.Start();

            Load += delegate
            {
                Log.Write(_layout, "启动器已打开，项目根目录 " + _layout.Root);
                RefreshState();
            };
        }

        // ------------------------------------------------------------------
        private void BuildUi()
        {
            Text = "离线 RAG 文档问答";
            Font = new Font("Microsoft YaHei UI", 9F);
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(620, 430);
            Icon = LoadIcon();

            Label title = new Label();
            title.Text = "离线 RAG 文档问答";
            title.Font = new Font(Font.FontFamily, 14F, FontStyle.Bold);
            title.AutoSize = true;
            title.Location = new Point(18, 16);
            Controls.Add(title);

            Label subtitle = new Label();
            subtitle.Text = "LangChain · Ollama · FAISS · 全部本地运行";
            subtitle.ForeColor = Color.Gray;
            subtitle.AutoSize = true;
            subtitle.Location = new Point(20, 48);
            Controls.Add(subtitle);

            _stateLabel = new Label();
            _stateLabel.Text = "状态：检测中…";
            _stateLabel.Font = new Font(Font.FontFamily, 11F, FontStyle.Bold);
            _stateLabel.AutoSize = true;
            _stateLabel.Location = new Point(20, 86);
            Controls.Add(_stateLabel);

            _detailLabel = new Label();
            _detailLabel.Text = "";
            _detailLabel.AutoSize = true;
            _detailLabel.ForeColor = Color.DimGray;
            _detailLabel.Location = new Point(22, 114);
            Controls.Add(_detailLabel);

            _kbLabel = new Label();
            _kbLabel.Text = "";
            _kbLabel.AutoSize = true;
            _kbLabel.ForeColor = Color.DimGray;
            _kbLabel.Location = new Point(22, 136);
            Controls.Add(_kbLabel);

            _openButton = MakeButton("打开页面", 20, 168, 110);
            _openButton.Click += delegate { if (_health != null) Program.OpenBrowser(_health.Url); };
            _startButton = MakeButton("启动服务", 140, 168, 110);
            _startButton.Click += delegate { StartService(); };
            _stopButton = MakeButton("停止服务", 260, 168, 110);
            _stopButton.Click += delegate { StopService(true); };
            Button exitButton = MakeButton("退出", 380, 168, 110);
            exitButton.Click += delegate { Close(); };

            _minimizeToTray = new CheckBox();
            _minimizeToTray.Text = "关闭窗口时最小化到托盘（服务继续运行）";
            _minimizeToTray.AutoSize = true;
            _minimizeToTray.Location = new Point(20, 212);
            Controls.Add(_minimizeToTray);

            _keepOllama = new CheckBox();
            _keepOllama.Text = "退出时保留 Ollama（不关掉，模型仍占显存）";
            _keepOllama.AutoSize = true;
            _keepOllama.Location = new Point(20, 236);
            Controls.Add(_keepOllama);

            Label logTitle = new Label();
            logTitle.Text = "运行日志（data\\logs）";
            logTitle.AutoSize = true;
            logTitle.ForeColor = Color.DimGray;
            logTitle.Location = new Point(20, 266);
            Controls.Add(logTitle);

            _logBox = new TextBox();
            _logBox.Multiline = true;
            _logBox.ReadOnly = true;
            _logBox.ScrollBars = ScrollBars.Vertical;
            _logBox.Font = new Font("Consolas", 8.5F);
            _logBox.BackColor = Color.FromArgb(248, 249, 251);
            _logBox.Location = new Point(20, 288);
            _logBox.Size = new Size(580, 122);
            Controls.Add(_logBox);

            _tray = new NotifyIcon();
            _tray.Icon = Icon;
            _tray.Text = "离线 RAG 文档问答";
            _tray.Visible = true;
            ContextMenuStrip menu = new ContextMenuStrip();
            menu.Items.Add("显示面板", null, delegate { RestoreWindow(); });
            menu.Items.Add("打开页面", null, delegate { if (_health != null) Program.OpenBrowser(_health.Url); });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("停止服务并退出", null, delegate { StopService(true); Close(); });
            _tray.ContextMenuStrip = menu;
            _tray.DoubleClick += delegate { RestoreWindow(); };

            _timer = new System.Windows.Forms.Timer();
            _timer.Interval = 2500;
            _timer.Tick += delegate { RefreshState(); };
            _timer.Start();
        }

        private Button MakeButton(string text, int x, int y, int width)
        {
            Button button = new Button();
            button.Text = text;
            button.Location = new Point(x, y);
            button.Size = new Size(width, 30);
            Controls.Add(button);
            return button;
        }

        private static Icon LoadIcon()
        {
            try { return Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
            catch { return SystemIcons.Application; }
        }

        // ------------------------------------------------------------------
        private void WatchShowSignal()
        {
            while (!_exiting)
            {
                try
                {
                    if (_showSignal.WaitOne(500))
                    {
                        try { BeginInvoke(new MethodInvoker(RestoreWindow)); }
                        catch { }
                    }
                }
                catch { return; }
            }
        }

        private void RestoreWindow()
        {
            Show();
            WindowState = FormWindowState.Normal;
            Activate();
        }

        // ------------------------------------------------------------------
        private void RefreshState()
        {
            if (_busy) return;
            _health = Probe.FindRunning(_layout, _options.Port);
            bool ollama = Probe.TcpOpen(11434, 400);

            if (_health != null)
            {
                _stateLabel.Text = "状态：运行中";
                _stateLabel.ForeColor = Color.FromArgb(22, 163, 74);
                _detailLabel.Text = _health.Url + "   ·   模型 " + _health.Model +
                                    "   ·   Ollama " + (ollama ? "运行中" : "未运行");
                _kbLabel.Text = "知识库：" + _health.Documents + " 个文档 / " + _health.Chunks + " 个分块";
            }
            else if (_child != null && !_child.HasExited)
            {
                _stateLabel.Text = "状态：正在启动…";
                _stateLabel.ForeColor = Color.FromArgb(217, 119, 6);
                _detailLabel.Text = "首次冷启动需要十几秒（要载入嵌入模型与 LLM）";
                _kbLabel.Text = "";
            }
            else
            {
                _stateLabel.Text = "状态：未运行";
                _stateLabel.ForeColor = Color.DimGray;
                _detailLabel.Text = _layout.ReadyToStart()
                    ? "点「启动服务」即可，日志会写进 " + _layout.LogsDir
                    : "环境不完整，请先运行 scripts\\prepare.cmd（首次需要联网）";
                _kbLabel.Text = "Ollama：" + (ollama ? "运行中" : "未运行");
            }

            _openButton.Enabled = _health != null;
            _stopButton.Enabled = _health != null;

            string tail = Log.Tail(_layout.BackendLog, 24, 32 * 1024);
            if (tail.Length == 0) tail = Log.Tail(_layout.StartLog, 24, 32 * 1024);
            if (tail.Length == 0) tail = Log.Tail(_layout.LauncherLog, 24, 32 * 1024);
            if (_logBox.Text != tail) _logBox.Text = tail;
        }

        // ------------------------------------------------------------------
        private void StartService()
        {
            if (!_layout.ReadyToStart())
            {
                MessageBox.Show(this, "环境不完整，请先双击 scripts\\prepare.cmd（首次需要联网）。",
                    "无法启动", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            _busy = true;
            try
            {
                _child = Service.Start(_layout, _options.Port, true, _keepOllama.Checked);
                Service.WriteState(_layout, _options.Port, _child != null ? _child.Id : 0);
                RefreshState();

                // 等健康检查通过再开浏览器 —— 不能靠猜时间，
                // 否则用户先看到的是「127.0.0.1 拒绝连接」。
                ThreadPool.QueueUserWorkItem(delegate
                {
                    Health health = null;
                    Stopwatch watch = Stopwatch.StartNew();
                    while (watch.ElapsedMilliseconds < 120000)
                    {
                        health = Probe.Check(_options.Port, 1500);
                        if (health != null && health.Ok) break;
                        Thread.Sleep(500);
                    }
                    Health ready = health;
                    try
                    {
                        BeginInvoke(new MethodInvoker(delegate
                        {
                            _busy = false;
                            RefreshState();
                            if (ready != null && ready.Ok) Program.OpenBrowser(ready.Url);
                        }));
                    }
                    catch { }
                });
            }
            catch (Exception ex)
            {
                _busy = false;
                MessageBox.Show(this, "启动失败：" + ex.Message, "错误",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private void StopService(bool askOllama)
        {
            if (_health == null && (_child == null || _child.HasExited)) { RefreshState(); return; }

            bool keep = askOllama && _keepOllama.Checked;
            _busy = true;
            _stateLabel.Text = "状态：正在停止…";
            _stateLabel.ForeColor = Color.FromArgb(217, 119, 6);
            try
            {
                Service.Stop(_layout, _options.Port, keep, 60000);
            }
            finally
            {
                _busy = false;
                _child = null;
                RefreshState();
            }
        }

        // ------------------------------------------------------------------
        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!_exiting && _minimizeToTray.Checked && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                Hide();
                _tray.ShowBalloonTip(1500, "仍在后台运行", "服务继续运行，双击托盘图标可重新打开面板。", ToolTipIcon.Info);
                return;
            }

            bool running = _health != null || (_child != null && !_child.HasExited);
            if (!_exiting && running)
            {
                string question = _keepOllama.Checked
                    ? "退出会停止后端服务（按当前设置保留 Ollama）。确定退出？"
                    : "退出会停止后端服务，并关闭 Ollama 进程。确定退出？";
                DialogResult result = MessageBox.Show(this, question, "退出确认",
                    MessageBoxButtons.YesNo, MessageBoxIcon.Question);
                if (result != DialogResult.Yes)
                {
                    e.Cancel = true;
                    return;
                }
                StopService(false);
            }

            _exiting = true;
            base.OnFormClosing(e);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                _exiting = true;
                if (_timer != null) { _timer.Stop(); _timer.Dispose(); }
                if (_tray != null) { _tray.Visible = false; _tray.Dispose(); }
            }
            base.Dispose(disposing);
        }
    }
}
