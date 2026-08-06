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
        private static Panel startupPanel;
        private static Panel startupProgressTrack;
        private static Panel startupProgressFill;
        private static Label startupStatus;
        private static bool startupNavigationPending = true;

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
                    window.BackColor = Color.FromArgb(247, 250, 248);
                    window.AutoScaleMode = AutoScaleMode.Dpi;
                    windowedBounds = window.Bounds;

                    browser.CreationProperties = new CoreWebView2CreationProperties
                    {
                        UserDataFolder = profileDirectory,
                        IsInPrivateModeEnabled = false
                    };
                    browser.Dock = DockStyle.Fill;
                    browser.DefaultBackgroundColor = Color.FromArgb(247, 250, 248);
                    window.Controls.Add(browser);
                    CreateStartupPanel();
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

        private static void CreateStartupPanel()
        {
            startupPanel = new Panel
            {
                Dock = DockStyle.Fill,
                BackColor = Color.FromArgb(247, 250, 248)
            };
            Panel card = new Panel
            {
                Size = new Size(500, 230),
                BackColor = Color.White
            };
            card.Paint += (sender, args) =>
            {
                using (Pen pen = new Pen(Color.FromArgb(220, 232, 227)))
                {
                    args.Graphics.DrawRectangle(pen, 0, 0, card.Width - 1, card.Height - 1);
                }
                using (SolidBrush brush = new SolidBrush(Color.FromArgb(53, 105, 92)))
                {
                    args.Graphics.FillRectangle(brush, 0, 0, 7, card.Height);
                }
            };
            Label title = new Label
            {
                AutoSize = true,
                Text = "研申",
                Font = new Font("Microsoft YaHei UI", 25, FontStyle.Bold),
                ForeColor = Color.FromArgb(32, 38, 34),
                Location = new Point(40, 34)
            };
            Label subtitle = new Label
            {
                AutoSize = true,
                Text = "正在打开你的本地申论工作台",
                Font = new Font("Microsoft YaHei UI", 10),
                ForeColor = Color.FromArgb(106, 116, 111),
                Location = new Point(43, 82)
            };
            startupProgressTrack = new Panel
            {
                BackColor = Color.FromArgb(219, 234, 228),
                Location = new Point(44, 130),
                Size = new Size(412, 8)
            };
            startupProgressFill = new Panel
            {
                BackColor = Color.FromArgb(53, 105, 92),
                Dock = DockStyle.Left,
                Width = 0
            };
            startupProgressTrack.Controls.Add(startupProgressFill);
            startupStatus = new Label
            {
                AutoSize = true,
                Text = "正在初始化桌面组件…",
                Font = new Font("Microsoft YaHei UI", 9),
                ForeColor = Color.FromArgb(51, 67, 61),
                Location = new Point(42, 154)
            };
            card.Controls.Add(title);
            card.Controls.Add(subtitle);
            card.Controls.Add(startupProgressTrack);
            card.Controls.Add(startupStatus);
            startupPanel.Controls.Add(card);
            startupPanel.Resize += (sender, args) =>
            {
                card.Left = Math.Max(0, (startupPanel.ClientSize.Width - card.Width) / 2);
                card.Top = Math.Max(0, (startupPanel.ClientSize.Height - card.Height) / 2);
            };
            window.Controls.Add(startupPanel);
            startupPanel.BringToFront();
            card.Left = (startupPanel.ClientSize.Width - card.Width) / 2;
            card.Top = (startupPanel.ClientSize.Height - card.Height) / 2;
            SetStartupProgress(35);
        }

        private static void SetStartupProgress(int value)
        {
            if (startupProgressTrack == null || startupProgressFill == null)
            {
                return;
            }
            int bounded = Math.Max(0, Math.Min(100, value));
            startupProgressFill.Width = (int)Math.Round(startupProgressTrack.ClientSize.Width * bounded / 100.0);
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
                SetStartupProgress(68);
                startupStatus.Text = "桌面组件已就绪，正在载入界面…";
                browser.CoreWebView2.Settings.IsStatusBarEnabled = false;
                browser.CoreWebView2.WebMessageReceived += HandleWebMessage;
                browser.CoreWebView2.NavigationStarting += (navigationSender, navigationArgs) =>
                {
                    if (!startupNavigationPending) return;
                    SetStartupProgress(84);
                    startupStatus.Text = "正在恢复上次的工作位置…";
                };
                browser.CoreWebView2.NavigationCompleted += (navigationSender, navigationArgs) =>
                {
                    if (!startupNavigationPending) return;
                    startupNavigationPending = false;
                    if (navigationArgs.IsSuccess)
                    {
                        SetStartupProgress(100);
                        startupStatus.Text = "准备完成";
                        startupPanel.Hide();
                        browser.Focus();
                        Log("Initial page navigation completed");
                    }
                    else
                    {
                        SetStartupProgress(100);
                        startupStatus.Text = "界面载入失败，请关闭应用后重试";
                        Log("Initial page navigation failed: " + navigationArgs.WebErrorStatus);
                    }
                };
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
