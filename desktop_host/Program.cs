using System;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace GongkaoShenlun.DesktopHost
{
    internal static class Program
    {
        private const string AppUserModelId = "GongkaoShenlun.Desktop";

        private static Form window;
        private static WebView2 browser;
        private static string startUrl;
        private static string logFile;
        private static int exitCode;
        private static Rectangle windowedBounds;

        [DllImport("shell32.dll", SetLastError = true)]
        private static extern int SetCurrentProcessExplicitAppUserModelID(
            [MarshalAs(UnmanagedType.LPWStr)] string appId
        );

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool SetProcessDpiAwarenessContext(IntPtr dpiContext);

        [STAThread]
        private static int Main(string[] args)
        {
            if (args.Length < 4)
            {
                MessageBox.Show(
                    "桌面窗口启动参数不完整，请重新下载完整程序。",
                    "研申启动失败",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 2;
            }

            startUrl = args[0];
            string profileDirectory = args[1];
            string iconPath = args[2];
            logFile = args[3];

            try
            {
                Directory.CreateDirectory(profileDirectory);
                SetCurrentProcessExplicitAppUserModelID(AppUserModelId);
                SetProcessDpiAwarenessContext(new IntPtr(-4));
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);

                using (Icon appIcon = LoadAppIcon(iconPath))
                using (window = new Form())
                using (browser = new WebView2())
                {
                    window.Text = "研申";
                    window.Icon = appIcon;
                    window.StartPosition = FormStartPosition.CenterScreen;
                    window.Size = new Size(1220, 820);
                    window.MinimumSize = new Size(900, 620);
                    window.BackColor = Color.FromArgb(244, 241, 232);
                    window.AutoScaleMode = AutoScaleMode.Dpi;
                    windowedBounds = window.Bounds;

                    browser.CreationProperties = new CoreWebView2CreationProperties
                    {
                        UserDataFolder = profileDirectory,
                        IsInPrivateModeEnabled = false
                    };
                    browser.Dock = DockStyle.Fill;
                    browser.DefaultBackgroundColor = Color.FromArgb(244, 241, 232);
                    window.Controls.Add(browser);
                    window.Shown += InitializeBrowser;

                    Log("Native C# WebView2 window shown");
                    Application.Run(window);
                    Log("Native C# WebView2 window closed");
                }

                return exitCode;
            }
            catch (Exception error)
            {
                Log("Desktop host failed: " + error);
                MessageBox.Show(
                    "WebView2 桌面窗口启动失败。\n\n" + error.Message,
                    "研申启动失败",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 1;
            }
        }

        private static Icon LoadAppIcon(string iconPath)
        {
            if (!String.IsNullOrWhiteSpace(iconPath) && File.Exists(iconPath))
            {
                try
                {
                    return new Icon(iconPath);
                }
                catch (Exception error)
                {
                    Log("External desktop icon could not be loaded; using embedded icon: " + error.Message);
                }
            }

            Icon embeddedIcon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            if (embeddedIcon != null)
            {
                Log("Using desktop host embedded icon");
                return embeddedIcon;
            }

            Log("Embedded desktop icon unavailable; using Windows application icon");
            return (Icon)SystemIcons.Application.Clone();
        }

        private static async void InitializeBrowser(object sender, EventArgs eventArgs)
        {
            try
            {
                await browser.EnsureCoreWebView2Async(null);
                browser.CoreWebView2.Settings.IsStatusBarEnabled = false;
                browser.CoreWebView2.WebMessageReceived += HandleWebMessage;
                browser.CoreWebView2.Navigate(startUrl);
                Log("WebView2 initialized and navigating to " + startUrl);
            }
            catch (Exception error)
            {
                exitCode = 1;
                Log("WebView2 initialization failed: " + error);
                MessageBox.Show(
                    "WebView2 初始化失败。请安装或修复 Microsoft WebView2 Runtime 后重试。\n\n" + error.Message,
                    "研申启动失败",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error,
                    MessageBoxDefaultButton.Button1
                );
                window.Close();
            }
        }

        private static void HandleWebMessage(object sender, CoreWebView2WebMessageReceivedEventArgs eventArgs)
        {
            string message;
            try
            {
                message = eventArgs.TryGetWebMessageAsString();
            }
            catch
            {
                return;
            }

            if (message.StartsWith("zoom:", StringComparison.Ordinal))
            {
                double zoom;
                if (
                    Double.TryParse(
                        message.Substring("zoom:".Length),
                        NumberStyles.Float,
                        CultureInfo.InvariantCulture,
                        out zoom
                    )
                )
                {
                    browser.ZoomFactor = Math.Max(0.5, Math.Min(2.0, zoom));
                    Log("Interface zoom changed to " + browser.ZoomFactor.ToString("0.##", CultureInfo.InvariantCulture));
                }
                return;
            }

            if (!message.StartsWith("display:", StringComparison.Ordinal))
            {
                return;
            }

            string profile = message.Substring("display:".Length);
            if (profile == "window")
            {
                if (window.FormBorderStyle != FormBorderStyle.None)
                {
                    return;
                }
                window.WindowState = FormWindowState.Normal;
                window.FormBorderStyle = FormBorderStyle.Sizable;
                if (windowedBounds.Width >= window.MinimumSize.Width && windowedBounds.Height >= window.MinimumSize.Height)
                {
                    window.Bounds = windowedBounds;
                }
                Log("Display profile changed to window");
                return;
            }

            if (profile == "fullscreen")
            {
                if (window.FormBorderStyle != FormBorderStyle.None)
                {
                    windowedBounds = window.WindowState == FormWindowState.Normal
                        ? window.Bounds
                        : window.RestoreBounds;
                }
                window.WindowState = FormWindowState.Normal;
                window.FormBorderStyle = FormBorderStyle.None;
                window.WindowState = FormWindowState.Maximized;
                Log("Display profile changed to automatic fullscreen");
            }
        }

        private static void Log(string message)
        {
            try
            {
                File.AppendAllText(
                    logFile,
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss,fff") + " INFO " + message + Environment.NewLine
                );
            }
            catch
            {
                // Logging must never prevent the desktop window from opening.
            }
        }
    }
}
