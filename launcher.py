import ctypes
import logging
import os
import subprocess
import threading
import time
import urllib.request
from tkinter import Tk, messagebox

from gongkao.paths import log_path, resource_root, user_data_dir
from gongkao.web.application import create_server

APP_NAME = "研申"
APP_USER_MODEL_ID = "GongkaoShenlun.Desktop"
START_PATH = "/home"
SERVER_READY_TIMEOUT_SECONDS = 20
SERVER_PORT_FILE = "server-port.txt"
WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
WEBVIEW2_CLIENT_KEY = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)
WEBVIEW2_USER_CLIENT_KEY = (
    r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)


def app_icon_path():
    return resource_root() / "assets" / "app-icon.ico"


def desktop_host_path():
    packaged_host = resource_root() / "gongkao_desktop_host.exe"
    if packaged_host.is_file():
        return packaged_host
    return resource_root() / "desktop_host" / "gongkao_desktop_host.exe"


def set_windows_app_user_model_id():
    if os.name != "nt":
        return
    try:
        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_long
        result = setter(APP_USER_MODEL_ID)
        if result != 0:
            logging.warning("Failed to set Windows AppUserModelID: HRESULT 0x%08x", result & 0xFFFFFFFF)
    except (AttributeError, OSError):
        logging.exception("Failed to configure the Windows AppUserModelID")


def webview2_runtime_version():
    if os.name != "nt":
        return ""

    import winreg

    candidates = (
        (winreg.HKEY_LOCAL_MACHINE, WEBVIEW2_CLIENT_KEY),
        (winreg.HKEY_CURRENT_USER, WEBVIEW2_USER_CLIENT_KEY),
    )
    for hive, key_path in candidates:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                version = str(winreg.QueryValueEx(key, "pv")[0]).strip()
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return version
    return ""


def ensure_webview2_runtime():
    version = webview2_runtime_version()
    if version:
        logging.info("Microsoft Edge WebView2 Runtime detected: %s", version)
        return version
    raise RuntimeError(
        "未检测到 Microsoft Edge WebView2 Runtime。\n"
        "本程序不需要 Edge 浏览器，但需要 Windows 的 WebView2 桌面运行组件。\n"
        f"请安装后重新打开：{WEBVIEW2_DOWNLOAD_URL}"
    )


class Launcher:
    def __init__(self):
        self.server = None
        self.server_thread = None
        self.url = ""
        self.start_url = ""

    def configure_logging(self):
        user_data_dir().mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=log_path(),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            encoding="utf-8",
            force=True,
        )

    def preferred_server_port(self):
        try:
            port = int((user_data_dir() / SERVER_PORT_FILE).read_text(encoding="utf-8").strip())
            return port if 1024 <= port <= 65535 else 0
        except (OSError, ValueError):
            return 0

    def remember_server_port(self, port):
        try:
            (user_data_dir() / SERVER_PORT_FILE).write_text(str(port), encoding="utf-8")
        except OSError:
            logging.warning("Could not persist local server port %s", port)

    def start_server(self):
        preferred_port = self.preferred_server_port()
        try:
            self.server = create_server(port=preferred_port)
        except OSError:
            if not preferred_port:
                raise
            logging.warning("Preferred local port %s is unavailable; choosing a new port", preferred_port)
            self.server = create_server(port=0)
        self.remember_server_port(self.server.server_address[1])
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.start_url = f"{self.url}{START_PATH}"
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            name="gongkao-local-server",
            daemon=True,
        )
        self.server_thread.start()
        self.wait_for_server_ready()
        logging.info("Gongkao Shenlun local service started at %s", self.url)

    def wait_for_server_ready(self):
        deadline = time.monotonic() + SERVER_READY_TIMEOUT_SECONDS
        last_error = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.start_url, timeout=0.8) as response:
                    if 200 <= response.status < 500:
                        return
            except OSError as exc:
                last_error = exc
            time.sleep(0.2)
        raise RuntimeError(f"本地服务未能在规定时间内启动：{last_error}")

    def start(self):
        try:
            self.configure_logging()
            set_windows_app_user_model_id()
            ensure_webview2_runtime()
            self.start_server()
            self.run_app_window()
        except Exception as exc:
            logging.exception("Failed to start")
            self.show_error(exc)
        finally:
            self.stop()

    def run_app_window(self):
        profile_dir = user_data_dir() / "webview_profile"
        host_log_path = user_data_dir() / "desktop_host.log"
        profile_dir.mkdir(parents=True, exist_ok=True)
        icon_path = app_icon_path()
        host_path = desktop_host_path()
        if not host_path.is_file():
            raise RuntimeError(f"桌面窗口宿主缺失：{host_path}")

        icon_argument = str(icon_path) if icon_path.is_file() else ""
        if not icon_argument:
            logging.warning(
                "Desktop icon resource is missing at %s; the native host will use its embedded icon",
                icon_path,
            )

        command = [
            str(host_path),
            self.start_url,
            str(profile_dir),
            icon_argument,
            str(host_log_path),
        ]
        logging.info("Opening native C# WebView2 desktop window at %s", self.start_url)
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                "WebView2 桌面窗口启动失败。请安装或修复 WebView2 Runtime 后重试。\n"
                f"下载地址：{WEBVIEW2_DOWNLOAD_URL}\n\n"
                f"窗口宿主退出代码：{result.returncode}\n桌面日志：{host_log_path}"
            )
        logging.info("Native desktop window closed")

    def show_error(self, exc):
        root = Tk()
        root.withdraw()
        messagebox.showerror(
            "启动失败",
            f"{APP_NAME}无法启动。\n\n{exc}\n\n日志：{log_path()}",
            parent=root,
        )
        root.destroy()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=5)
        self.server_thread = None


if __name__ == "__main__":
    Launcher().start()
