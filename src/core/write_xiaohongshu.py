# 小红书的自动发稿
from playwright.async_api import async_playwright
import time
import json
import os
import sys
import subprocess
import logging
import asyncio
from glob import glob
from PyQt5.QtWidgets import QInputDialog, QLineEdit
from PyQt5.QtCore import QObject, pyqtSignal, QMetaObject, Qt, QThread, pyqtSlot
from PyQt5.QtWidgets import QApplication
log_path = os.path.expanduser('~/Desktop/xhsai_error.log')
logging.basicConfig(filename=log_path, level=logging.DEBUG)

class VerificationCodeHandler(QObject):
    code_received = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self.code = None
        self.dialog = None
        
    async def get_verification_code(self):
        # 确保在主线程中执行
        if QApplication.instance().thread() != QThread.currentThread():
            # 如果不在主线程，使用moveToThread移动到主线程
            self.moveToThread(QApplication.instance().thread())
            # 使用invokeMethod确保在主线程中执行
            QMetaObject.invokeMethod(self, "_show_dialog", Qt.ConnectionType.BlockingQueuedConnection)
        else:
            # 如果已经在主线程，直接执行
            self._show_dialog()
        
        # 等待代码输入完成
        while self.code is None:
            await asyncio.sleep(0.1)
            
        return self.code
    
    @pyqtSlot()
    def _show_dialog(self):
        code, ok = QInputDialog.getText(
            None,
            "验证码",
            "请输入验证码（如需扫码/滑块等风控验证，可点取消并在浏览器中手动完成登录）:",
            QLineEdit.EchoMode.Normal,
        )
        if ok:
            self.code = code
            self.code_received.emit(code)
        else:
            self.code = ""

class XiaohongshuPoster:
    def __init__(self, user_id: int = None, browser_environment=None):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._auth_issue = False
        self._auth_issue_url = None
        self.verification_handler = VerificationCodeHandler()
        self.loop = None
        self.user_id = user_id
        self.browser_environment = browser_environment
        # 不再在初始化时调用 initialize，而是让调用者显式调用

    @staticmethod
    def _is_truthy(value, *, default: bool = False) -> bool:
        """Parse common truthy/falsey values from env/config."""
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off", ""):
            return False
        return default

    def _reset_auth_issue(self) -> None:
        self._auth_issue = False
        self._auth_issue_url = None

    async def _is_creator_logged_in(self) -> bool:
        """Best-effort login check without navigating away from current page."""
        try:
            if not self.context:
                return False
            req = getattr(self.context, "request", None)
            if req is None:
                return False
            resp = await req.get(
                "https://creator.xiaohongshu.com/api/galaxy/user/info",
                timeout=10_000,
            )
            status = getattr(resp, "status", None)
            try:
                dispose = getattr(resp, "dispose", None)
                if callable(dispose):
                    await dispose()
            except Exception:
                pass
            return int(status or 0) == 200
        except Exception:
            return False

    def _get_user_phone(self) -> str:
        try:
            uid = getattr(self, "user_id", None)
            if not uid:
                return ""
            from src.core.services.user_service import user_service

            user = user_service.get_user_by_id(int(uid))
            return (user.phone or "").strip() if user else ""
        except Exception:
            return ""

    async def _wait_until_creator_logged_in(self, timeout_s: int = 180) -> bool:
        if not self.page:
            return False

        deadline = time.time() + max(5, int(timeout_s or 0))
        last_url = ""
        next_probe_at = 0.0
        while time.time() < deadline:
            now = time.time()
            # Throttle API probing to avoid excessive requests while user is scanning/validating.
            if now >= next_probe_at:
                next_probe_at = now + 3.0
                if await self._is_creator_logged_in():
                    return True
            try:
                last_url = self.page.url or last_url
                if "login" not in (last_url or "") and not self._auth_issue:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1)

        # 最后再主动探测一次创作者首页
        try:
            self._reset_auth_issue()
            await self.page.goto("https://creator.xiaohongshu.com/new/home", wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.5)
            last_url = self.page.url or last_url
            if await self._is_creator_logged_in():
                return True
            if "login" not in (last_url or "") and not self._auth_issue:
                return True
        except Exception:
            pass

        # 仍未登录：保存诊断信息，便于判断是否卡在风控/二维码登录页
        try:
            await self._dump_page_debug(tag="login_timeout", include_cookies=True)
        except Exception:
            pass
        return False

    async def _warmup_xhs_sso(self) -> None:
        """让 SSO 覆盖到 www 域名，避免发布页调用 www.* 接口返回 401 无登录信息。"""
        if not self.page:
            return
        try:
            await self.page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.5)
            try:
                cookies = await self.context.cookies("https://www.xiaohongshu.com")
                names = sorted({(c.get("name") or "").strip() for c in cookies if (c.get("name") or "").strip()})
                print(f"SSO 同步: www cookies={len(cookies)} names={names[:12]}")
            except Exception:
                pass
        except Exception:
            pass
        try:
            await self.page.goto("https://creator.xiaohongshu.com/new/home", wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.0)
        except Exception:
            pass

    def _get_env_value(self, key, default=None):
        env = self.browser_environment
        if env is None:
            return os.getenv(str(key), default)
        if isinstance(env, dict):
            if key in env:
                return env.get(key, default)
            # Allow passing extra knobs via .env without changing DB schema/UI.
            return os.getenv(str(key), default)
        # SQLAlchemy model: direct field, then extra_config, then real OS env.
        try:
            val = getattr(env, key)
            if val is not None:
                return val
        except Exception:
            pass
        try:
            extra = getattr(env, "extra_config", None)
            if isinstance(extra, dict) and key in extra:
                return extra.get(key, default)
        except Exception:
            pass
        return os.getenv(str(key), default)

    def _get_user_storage_dir(self) -> str:
        home_dir = os.path.expanduser('~')
        base_dir = os.path.join(home_dir, '.xhs_system')
        if self.user_id is None:
            return base_dir
        return os.path.join(base_dir, "users", str(self.user_id))

    def _build_playwright_proxy(self):
        if not self.browser_environment:
            return None

        proxy_enabled = bool(self._get_env_value("proxy_enabled", False))
        proxy_type = (self._get_env_value("proxy_type") or "").strip()
        if not proxy_enabled or not proxy_type or proxy_type == "direct":
            return None

        host = self._get_env_value("proxy_host")
        port = self._get_env_value("proxy_port")
        if not host or not port:
            return None

        scheme = proxy_type
        if scheme == "https":
            scheme = "http"

        proxy = {"server": f"{scheme}://{host}:{int(port)}"}
        username = self._get_env_value("proxy_username")
        password = self._get_env_value("proxy_password")
        if username:
            proxy["username"] = str(username)
        if password:
            proxy["password"] = str(password)
        return proxy

    def _build_context_options(self):
        options = {"permissions": ["geolocation"]}

        ua = self._get_env_value("user_agent")
        if ua:
            options["user_agent"] = ua

        try:
            vw = int(self._get_env_value("viewport_width", 0) or 0)
            vh = int(self._get_env_value("viewport_height", 0) or 0)
            if vw > 0 and vh > 0:
                options["viewport"] = {"width": vw, "height": vh}
        except Exception:
            pass

        try:
            sw = int(self._get_env_value("screen_width", 0) or 0)
            sh = int(self._get_env_value("screen_height", 0) or 0)
            if sw > 0 and sh > 0:
                options["screen"] = {"width": sw, "height": sh}
        except Exception:
            pass

        locale = self._get_env_value("locale")
        if locale:
            options["locale"] = locale

        tz = self._get_env_value("timezone")
        if tz:
            options["timezone_id"] = tz

        lat = self._get_env_value("geolocation_latitude")
        lng = self._get_env_value("geolocation_longitude")
        if lat and lng:
            try:
                options["geolocation"] = {"latitude": float(lat), "longitude": float(lng)}
            except Exception:
                pass

        return options

    def _get_debug_dir(self) -> str:
        base = self._get_user_storage_dir()
        debug_dir = os.path.join(base, "debug")
        try:
            os.makedirs(debug_dir, exist_ok=True)
        except Exception:
            pass
        return debug_dir

    async def _dump_page_debug(self, *, tag: str, include_cookies: bool = False) -> None:
        """保存当前页面截图/HTML（以及可选 cookies）用于排查登录/风控问题。"""
        if not self.page:
            return

        ts = time.strftime("%Y%m%d-%H%M%S")
        safe_tag = "".join([c if c.isalnum() or c in ("-", "_") else "_" for c in (tag or "debug")])[:40]
        debug_dir = self._get_debug_dir()
        base = os.path.join(debug_dir, f"{safe_tag}_{ts}")

        info_path = f"{base}.json"
        screenshot_path = f"{base}.png"
        html_path = f"{base}.html"
        cookies_path = f"{base}_cookies.json"

        info = {
            "tag": tag,
            "timestamp": ts,
            "url": getattr(self.page, "url", "") or "",
            "auth_issue": bool(getattr(self, "_auth_issue", False)),
            "auth_issue_url": getattr(self, "_auth_issue_url", None),
        }

        try:
            info["title"] = await self.page.title()
        except Exception:
            pass

        try:
            text_preview = await self.page.evaluate(
                "() => (document.body && (document.body.innerText || document.body.textContent) || '').slice(0, 1200)"
            )
            if text_preview:
                info["body_text_preview"] = str(text_preview)
                keywords = ["访问异常", "环境异常", "风险", "安全", "验证码", "滑块", "请在手机", "请使用手机", "请先登录"]
                hit = [k for k in keywords if k in str(text_preview)]
                if hit:
                    info["possible_risk_keywords"] = hit
        except Exception:
            pass

        try:
            await self.page.screenshot(path=screenshot_path, full_page=True, timeout=20_000)
            info["screenshot"] = screenshot_path
        except Exception as e:
            info["screenshot_error"] = str(e)

        try:
            html = await self.page.content()
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            info["html"] = html_path
        except Exception as e:
            info["html_error"] = str(e)

        if include_cookies and self.context:
            try:
                cookies = await self.context.cookies()
                with open(cookies_path, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, ensure_ascii=False, indent=2)
                info["cookies"] = cookies_path
                try:
                    domains = sorted({(c.get("domain") or "").strip() for c in cookies if (c.get("domain") or "").strip()})
                    info["cookie_domains"] = domains
                except Exception:
                    pass
            except Exception as e:
                info["cookies_error"] = str(e)

        try:
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
            print(f"已保存调试信息: {info_path}")
        except Exception:
            pass

    def _candidate_ms_playwright_dirs(self):
        """返回可能存在 Playwright 浏览器缓存的目录列表（按优先级排序）。"""
        candidates = []

        home_dir = os.path.expanduser("~")

        # 项目自用目录（更不容易被系统清理）
        candidates.append(os.path.join(home_dir, ".xhs_system", "ms-playwright"))

        # Playwright 默认缓存目录
        if sys.platform == "win32":
            local_app_data = os.environ.get("LOCALAPPDATA") or os.path.join(home_dir, "AppData", "Local")
            candidates.append(os.path.join(local_app_data, "ms-playwright"))
        elif sys.platform == "darwin":
            candidates.append(os.path.join(home_dir, "Library", "Caches", "ms-playwright"))
        else:
            candidates.append(os.path.join(home_dir, ".cache", "ms-playwright"))

        # 打包版本：浏览器可能随应用一起带在 ms-playwright
        if getattr(sys, "frozen", False):
            if sys.platform == "win32":
                base_dir = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
                candidates.insert(0, os.path.join(base_dir, "ms-playwright"))
            elif sys.platform == "darwin":
                executable_dir = os.path.dirname(sys.executable)
                # DMG / .app 两种常见结构
                candidates.insert(0, os.path.join(executable_dir, "ms-playwright"))
                candidates.insert(0, os.path.join(executable_dir, "Contents", "MacOS", "ms-playwright"))

        # 去重并过滤不存在的目录
        seen = set()
        result = []
        for path in candidates:
            if not path or path in seen:
                continue
            seen.add(path)
            if os.path.exists(path):
                result.append(path)
        return result

    def _find_chromium_executable_under(self, root_dir: str):
        """在指定 ms-playwright 目录内查找 Chromium 可执行文件。"""
        if not root_dir or not os.path.exists(root_dir):
            return None

        if sys.platform == "win32":
            direct = os.path.join(root_dir, "chrome-win", "chrome.exe")
            if os.path.exists(direct):
                return direct

            candidates = glob(os.path.join(root_dir, "chromium-*", "chrome-win", "chrome.exe"))
            candidates.sort(reverse=True)
            for path in candidates:
                if os.path.exists(path):
                    return path

            for dirpath, _, filenames in os.walk(root_dir):
                if "chrome.exe" in filenames:
                    return os.path.join(dirpath, "chrome.exe")

        elif sys.platform == "darwin":
            candidates = glob(
                os.path.join(
                    root_dir,
                    "chromium-*",
                    "chrome-mac",
                    "Chromium.app",
                    "Contents",
                    "MacOS",
                    "Chromium",
                )
            )
            candidates.sort(reverse=True)
            for path in candidates:
                if os.path.exists(path):
                    return path

            for dirpath, _, filenames in os.walk(root_dir):
                if "Chromium" in filenames and dirpath.endswith(os.path.join("Contents", "MacOS")):
                    return os.path.join(dirpath, "Chromium")

        else:
            candidates = glob(os.path.join(root_dir, "chromium-*", "chrome-linux", "chrome"))
            candidates.sort(reverse=True)
            for path in candidates:
                if os.path.exists(path):
                    return path

            for dirpath, _, filenames in os.walk(root_dir):
                if "chrome" in filenames:
                    return os.path.join(dirpath, "chrome")

        return None

    def _find_playwright_chromium_executable(self):
        for root in self._candidate_ms_playwright_dirs():
            found = self._find_chromium_executable_under(root)
            if found:
                return found
        return None

    def _detect_windows_browser_channel(self):
        """检测系统安装的浏览器通道（避免 Playwright 缓存被清理导致无法启动）。"""
        if sys.platform != "win32":
            return None

        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local_app_data = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")

        chrome_paths = [
            os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
        ]
        if any(os.path.exists(p) for p in chrome_paths):
            return "chrome"

        edge_paths = [
            os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(local_app_data, "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        if any(os.path.exists(p) for p in edge_paths):
            return "msedge"

        return None

    def _is_missing_executable_error(self, err) -> bool:
        if not err:
            return False
        msg = str(err)
        keywords = [
            "Executable doesn't exist",
            "executable doesn't exist",
            "chromium",
            "browserType.launch",
        ]
        if "Executable doesn't exist" in msg or "executable doesn't exist" in msg:
            return True
        # 一些本地化/兼容错误文案
        if ("找不到" in msg or "不存在" in msg) and "Executable" in msg:
            return True
        # 兜底：出现 chromium 且无法找到可执行文件时也尝试修复
        return "chromium" in msg and ("not found" in msg.lower() or "不存在" in msg or "找不到" in msg)

    def _get_playwright_browsers_path(self) -> str:
        return os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH",
            os.path.join(os.path.expanduser("~"), ".xhs_system", "ms-playwright"),
        )

    async def _auto_install_playwright_chromium(self) -> bool:
        """检测到 Playwright 浏览器缺失时尝试自动安装（打包版不执行）。"""
        if getattr(sys, "frozen", False):
            return False

        browsers_path = self._get_playwright_browsers_path()
        try:
            os.makedirs(browsers_path, exist_ok=True)
        except Exception:
            pass

        env = os.environ.copy()
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", browsers_path)
        if sys.platform == "win32":
            env.setdefault("PLAYWRIGHT_DOWNLOAD_HOST", "https://npmmirror.com/mirrors/playwright")

        cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
        print("🔧 检测到浏览器缺失，尝试自动安装 Playwright Chromium（可能需要几分钟）...")

        def _run():
            return subprocess.run(cmd, capture_output=True, text=True, env=env)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        try:
            if loop:
                result = await loop.run_in_executor(None, _run)
            else:
                result = _run()
        except Exception as e:
            print(f"❌ 自动安装失败: {e}")
            return False

        if result.returncode == 0:
            print("✅ Playwright Chromium 自动安装完成")
            return True

        stderr = (result.stderr or "").strip()
        if stderr:
            print(f"❌ 自动安装失败: {stderr[:800]}")
        return False

    async def initialize(self):
        """初始化浏览器"""
        if self.playwright is not None:
            return
            
        try:
            print("开始初始化Playwright...")
            self.playwright = await async_playwright().start()

            # 指纹提示：系统为 macOS 但环境配置为 Win32/Windows 时，容易触发风控（UA/Client-Hints/platform 不一致）
            try:
                ua_hint = str(self._get_env_value("user_agent") or "")
                platform_hint = str(self._get_env_value("platform") or "")
                if sys.platform == "darwin" and ("Windows NT" in ua_hint or platform_hint == "Win32"):
                    print("⚠️ 检测到当前默认浏览器环境为 Windows 指纹，但你在 macOS 上运行；建议在【浏览器环境】切换到 Mac Chrome 直连（platform=MacIntel, UA=Macintosh）后再登录。")
                if sys.platform == "win32" and ("Macintosh" in ua_hint or platform_hint == "MacIntel"):
                    print("⚠️ 检测到当前默认浏览器环境为 Mac 指纹，但你在 Windows 上运行；建议切换到 Windows Chrome 指纹后再登录。")
            except Exception:
                pass

            # 获取用户数据目录（多用户隔离 token/cookies/storage_state）
            app_dir = self._get_user_storage_dir()
            os.makedirs(app_dir, exist_ok=True)

            # 设置 token/cookies/storage_state 文件路径
            self.token_file = os.path.join(app_dir, "xiaohongshu_token.json")
            self.cookies_file = os.path.join(app_dir, "xiaohongshu_cookies.json")
            self.storage_state_file = os.path.join(app_dir, "xiaohongshu_storage_state.json")
            self.token = self._load_token()

            # 启动参数：默认尽量接近真实浏览器，避免过多“反常”flags 触发风控/登录异常
            args_mode = str(self._get_env_value("browser_args_mode", "") or "").strip().lower()
            minimal_args = ["--start-maximized"]
            if sys.platform.startswith("linux"):
                minimal_args = [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--start-maximized",
                ]

            compat_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-infobars",
                "--start-maximized",
                "--ignore-certificate-errors",
                "--ignore-ssl-errors",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--memory-pressure-off",
                "--max_old_space_size=4096",
            ]

            chosen_args = compat_args if args_mode in ("compat", "legacy") else minimal_args
            print(f"浏览器启动参数模式: {'compat' if chosen_args is compat_args else 'minimal'}")

            # 推荐：使用 persistent context 保存完整浏览器 Profile（cookies + localStorage + IndexedDB...）
            # 这样只需登录一次，后续可自动复用登录态，减少“每次都要登录”的痛点。
            use_persistent_context = self._is_truthy(
                self._get_env_value("XHS_USE_PERSISTENT_CONTEXT", None),
                default=True,
            )

            # Persist for login()/debug decisions
            self._use_persistent_context = use_persistent_context

            chrome_user_data_dir = str(
                self._get_env_value(
                    "XHS_CHROME_USER_DATA_DIR",
                    os.path.join(app_dir, "chrome_user_data"),
                )
                or ""
            ).strip()
            self._chrome_user_data_dir = chrome_user_data_dir
            self._managed_chrome_user_data_dir = os.path.join(app_dir, "chrome_user_data")

            chrome_profile_directory = str(self._get_env_value("XHS_CHROME_PROFILE_DIRECTORY", "") or "").strip()
            if chrome_profile_directory and not any(a.startswith("--profile-directory=") for a in chosen_args):
                chosen_args = list(chosen_args) + [f"--profile-directory={chrome_profile_directory}"]

            launch_args = {
                'headless': False,
                # 部分机器/环境启动较慢，适当拉长超时避免“偶发启动失败”
                'timeout': 60_000,
                'args': chosen_args,
            }

            proxy = self._build_playwright_proxy()
            if proxy:
                launch_args["proxy"] = proxy

            executable_path = None
            channel = None

            # macOS：优先使用系统 Chrome（更稳定），否则尝试 Playwright 缓存
            if sys.platform == "darwin":
                system_chrome_paths = [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium",
                ]
                for chrome_path in system_chrome_paths:
                    if os.path.exists(chrome_path):
                        executable_path = chrome_path
                        print(f"使用系统Chrome: {chrome_path}")
                        break

            # 优先尝试 Playwright 已下载/随包附带的 Chromium
            if not executable_path:
                executable_path = self._find_playwright_chromium_executable()
                if executable_path:
                    print(f"使用Playwright Chromium: {executable_path}")

            # Windows：如果 Playwright 缓存缺失，退回使用系统 Chrome/Edge 通道
            if sys.platform == "win32" and not executable_path:
                channel = self._detect_windows_browser_channel()
                if channel:
                    print(f"使用系统浏览器通道: {channel}")

            launch_attempts = []
            if executable_path:
                try:
                    os.chmod(executable_path, 0o755)
                except Exception:
                    pass
                args_with_path = dict(launch_args)
                args_with_path["executable_path"] = executable_path
                launch_attempts.append(args_with_path)

            if channel:
                args_with_channel = dict(launch_args)
                args_with_channel["channel"] = channel
                launch_attempts.append(args_with_channel)

            # 最后尝试 Playwright 默认路径
            launch_attempts.append(dict(launch_args))

            # 创建新的上下文（应用指纹/地理位置等）
            context_options = self._build_context_options()

            # persistent context: 用完整 Profile 目录保存登录态（更稳），默认开启；失败则自动回退到普通模式。
            if use_persistent_context and chrome_user_data_dir:
                try:
                    os.makedirs(chrome_user_data_dir, exist_ok=True)
                except Exception:
                    pass

                persistent_error = None
                for attempt in launch_attempts:
                    try:
                        merged = dict(attempt)
                        merged.update(context_options)
                        self.context = await self.playwright.chromium.launch_persistent_context(
                            chrome_user_data_dir,
                            **merged,
                        )
                        self.browser = getattr(self.context, "browser", None)
                        pages = getattr(self.context, "pages", None) or []
                        self.page = pages[0] if pages else await self.context.new_page()
                        print(f"使用 persistent profile: {chrome_user_data_dir}")
                        break
                    except Exception as e:
                        persistent_error = e
                        continue

                if not self.context:
                    print(f"persistent profile 启动失败，回退到普通模式: {persistent_error}")

            last_error = None
            if not self.context:
                for attempt in launch_attempts:
                    try:
                        self.browser = await self.playwright.chromium.launch(**attempt)
                        break
                    except Exception as e:
                        last_error = e
                        continue

            # 对 persistent context 来说，Playwright 可能不会暴露 Browser 对象（context 仍可正常使用）。
            # 仅当 browser 和 context 都为空时，才视为启动失败。
            if not self.browser and not self.context:
                # 自愈：Playwright 浏览器缺失时尝试自动安装再重试一次（开发/源码运行场景）
                if self._is_missing_executable_error(last_error) and await self._auto_install_playwright_chromium():
                    executable_path = self._find_playwright_chromium_executable()
                    launch_attempts_retry = []

                    if executable_path:
                        try:
                            os.chmod(executable_path, 0o755)
                        except Exception:
                            pass
                        args_with_path = dict(launch_args)
                        args_with_path["executable_path"] = executable_path
                        launch_attempts_retry.append(args_with_path)

                    if channel:
                        args_with_channel = dict(launch_args)
                        args_with_channel["channel"] = channel
                        launch_attempts_retry.append(args_with_channel)

                    launch_attempts_retry.append(dict(launch_args))

                    last_error = None
                    for attempt in launch_attempts_retry:
                        try:
                            self.browser = await self.playwright.chromium.launch(**attempt)
                            break
                        except Exception as e:
                            last_error = e

                if not self.browser and not self.context:
                    raise last_error

            # 如果已通过 persistent context 初始化，则无需再 new_context
            if self.context and self.page:
                loaded_storage_state = False
            else:
                loaded_storage_state = False
                try:
                    if os.path.exists(self.storage_state_file) and os.path.getsize(self.storage_state_file) > 0:
                        context_options["storage_state"] = self.storage_state_file
                        loaded_storage_state = True
                        print(f"加载 storage_state: {self.storage_state_file}")
                except Exception:
                    pass

                self.context = await self.browser.new_context(**context_options)
                self.page = await self.context.new_page()

            # 页面诊断：仅输出 error/warning，便于定位“选中文件但无预览/无上传”的前端异常
            try:
                def _on_console(msg):
                    try:
                        msg_type = getattr(msg, "type", "") or ""
                        if msg_type in ("error", "warning"):
                            text = (msg.text() if callable(getattr(msg, "text", None)) else getattr(msg, "text", "")) or ""
                            if msg_type == "warning" and "Mixed Content" in text:
                                return
                            location = getattr(msg, "location", None)
                            if location:
                                url = location.get("url")
                                line = location.get("lineNumber")
                                col = location.get("columnNumber")
                                print(f"[console:{msg_type}] {text} ({url}:{line}:{col})")
                            else:
                                print(f"[console:{msg_type}] {text}")
                    except Exception:
                        pass

                def _on_page_error(exc):
                    try:
                        print(f"[pageerror] {exc}")
                    except Exception:
                        pass

                def _on_request_failed(req):
                    try:
                        url = getattr(req, "url", "")
                        resource_type = getattr(req, "resource_type", "")
                        failure = None
                        try:
                            failure = getattr(req, "failure", None)
                            if callable(failure):
                                failure = failure()
                        except Exception:
                            failure = None
                        err_text = ""
                        if isinstance(failure, dict):
                            err_text = failure.get("errorText") or ""
                        elif isinstance(failure, str):
                            err_text = failure
                        else:
                            err_text = getattr(failure, "error_text", "") or ""
                            if not err_text and failure is not None:
                                err_text = str(failure)
                        if (url or "").find("/login") != -1 and ("redirectReason=401" in (url or "") or "redirectReason=403" in (url or "")):
                            self._auth_issue = True
                            self._auth_issue_url = url
                        should_log = any(k in (url or "") for k in ("upload", "image", "file", "encryption", "login", "edith", "ark", "creator"))
                        if resource_type in ("xhr", "fetch") and "xiaohongshu.com" in (url or "") and "apm-fe.xiaohongshu.com" not in (url or ""):
                            should_log = True
                        if should_log:
                            print(f"[requestfailed] {resource_type} {url} {err_text}")
                    except Exception:
                        pass
                
                def _on_response(resp):
                    try:
                        status = getattr(resp, "status", None)
                        url = getattr(resp, "url", "") or ""
                        if status in (401, 403) and any(host in url for host in ("creator.xiaohongshu.com", "edith.xiaohongshu.com", "ark.xiaohongshu.com", "www.xiaohongshu.com")):
                            self._auth_issue = True
                            self._auth_issue_url = url
                            print(f"[response:{status}] {url}")
                    except Exception:
                        pass

                def _on_frame_navigated(frame):
                    try:
                        if frame == self.page.main_frame:
                            url = frame.url or ""
                            if "/login" in url:
                                self._auth_issue = True
                                self._auth_issue_url = url
                                print(f"[navigation] {url}")
                    except Exception:
                        pass

                self.page.on("console", _on_console)
                self.page.on("pageerror", _on_page_error)
                self.page.on("requestfailed", _on_request_failed)
                self.page.on("response", _on_response)
                self.page.on("framenavigated", _on_frame_navigated)
            except Exception:
                pass
            
            # 注入stealth.min.js
            webgl_vendor = self._get_env_value("webgl_vendor") or "Intel Open Source Technology Center"
            webgl_renderer = self._get_env_value("webgl_renderer") or "Mesa DRI Intel(R) HD Graphics (SKL GT2)"
            platform = self._get_env_value("platform") or ""
            webgl_vendor_js = json.dumps(webgl_vendor, ensure_ascii=False)
            webgl_renderer_js = json.dumps(webgl_renderer, ensure_ascii=False)
            platform_js = json.dumps(platform, ensure_ascii=False)
            stealth_js = """
            (function(){
                const __xhs_webgl_vendor = %s;
                const __xhs_webgl_renderer = %s;
                const __xhs_platform = %s;

                try {
                    const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
                    if (typeof originalQuery === 'function') {
                        window.navigator.permissions.query = (parameters) => (
                            parameters && parameters.name === 'notifications'
                                ? Promise.resolve({ state: Notification.permission })
                                : originalQuery.call(window.navigator.permissions, parameters)
                        );
                    }
                } catch (e) {}
                
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) {
                        return __xhs_webgl_vendor;
                    }
                    if (parameter === 37446) {
                        return __xhs_webgl_renderer;
                    }
                    return getParameter.apply(this, arguments);
                };

                if (__xhs_platform) {
                    try {
                        Object.defineProperty(navigator, 'platform', { get: () => __xhs_platform });
                    } catch (e) {}
                }

                try {
                    const originalGetBoundingClientRect = Element.prototype.getBoundingClientRect;
                    Element.prototype.getBoundingClientRect = function() {
                        const rect = originalGetBoundingClientRect.apply(this, arguments);
                        try {
                            rect.width = Math.round(rect.width);
                            rect.height = Math.round(rect.height);
                        } catch (e) {}
                        return rect;
                    };
                } catch (e) {}

                try {
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                } catch (e) {}

                try {
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                } catch (e) {}

                try {
                    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
                } catch (e) {}
                
                window.chrome = {
                    runtime: {}
                };
            })();
            """ % (webgl_vendor_js, webgl_renderer_js, platform_js)
            await self.page.add_init_script(stealth_js)
            
            print("浏览器启动成功！")
            logging.debug("浏览器启动成功！")

            # 对 persistent profile：若 storage_state/cookies 已存在，可自动引导一次登录态（无需短信/扫码）
            if use_persistent_context:
                try:
                    await self._maybe_bootstrap_persistent_session()
                except Exception:
                    pass
            else:
                # 如已加载 storage_state，则无需再次 add_cookies，避免用旧 cookies 覆盖更完整的登录态
                if not loaded_storage_state:
                    await self._load_cookies()

        except Exception as e:
            print(f"初始化过程中出现错误: {str(e)}")
            logging.debug(f"初始化过程中出现错误: {str(e)}")
            await self.close(force=True)  # 确保资源被正确释放
            raise

    def _load_token(self):
        """从文件加载token"""
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, 'r') as f:
                    token_data = json.load(f)
                    # 检查token是否过期
                    if token_data.get('expire_time', 0) > time.time():
                        return token_data.get('token')
            except:
                pass
        return None

    def _save_token(self, token):
        """保存token到文件"""
        token_data = {
            'token': token,
            # token有效期设为30天
            'expire_time': time.time() + 30 * 24 * 3600
        }
        with open(self.token_file, 'w') as f:
            json.dump(token_data, f)

    async def _load_cookies(self):
        """从文件加载cookies"""
        if os.path.exists(self.cookies_file):
            try:
                with open(self.cookies_file, 'r') as f:
                    cookies = json.load(f)
                    # 确保cookies包含必要的字段
                    for cookie in cookies:
                        if 'domain' not in cookie:
                            cookie['domain'] = '.xiaohongshu.com'
                        if 'path' not in cookie:
                            cookie['path'] = '/'
                    await self.context.add_cookies(cookies)
            except Exception as e:
                logging.debug(f"加载cookies失败: {str(e)}")

    async def _save_cookies(self):
        """保存cookies到文件"""
        try:
            cookies = await self.context.cookies()
            with open(self.cookies_file, 'w') as f:
                json.dump(cookies, f)
        except Exception as e:
            logging.debug(f"保存cookies失败: {str(e)}")

    async def _save_storage_state(self):
        """保存 storage_state（包含 cookies + localStorage），用于下次会话恢复登录态。"""
        try:
            if not self.context:
                return
            path = getattr(self, "storage_state_file", None)
            if not path:
                return
            await self.context.storage_state(path=path)
        except Exception as e:
            logging.debug(f"保存storage_state失败: {str(e)}")

    async def _restore_storage_state_to_context(self, state: dict) -> None:
        """将 storage_state 写入当前 context（主要用于 persistent profile 的首次引导）。"""
        if not self.context or not self.page:
            return
        if not isinstance(state, dict):
            return

        cookies = state.get("cookies") if isinstance(state.get("cookies"), list) else []
        if cookies:
            try:
                await self.context.add_cookies(cookies)
            except Exception as e:
                logging.debug(f"写入cookies失败: {str(e)}")

        origins = state.get("origins") if isinstance(state.get("origins"), list) else []
        for o in origins:
            if not isinstance(o, dict):
                continue
            origin = str(o.get("origin") or "").strip()
            items = o.get("localStorage") if isinstance(o.get("localStorage"), list) else []
            if not origin or not items:
                continue
            try:
                await self.page.goto(origin, wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                # 即使跳转/超时，也尽量尝试写入 localStorage
                pass
            try:
                await self.page.evaluate(
                    """(items) => {
                        try {
                            for (const it of (items || [])) {
                                if (!it) continue;
                                const k = String(it.name || "");
                                if (!k) continue;
                                const v = (it.value === undefined || it.value === null) ? "" : String(it.value);
                                localStorage.setItem(k, v);
                            }
                        } catch (e) {}
                    }""",
                    items,
                )
            except Exception as e:
                logging.debug(f"写入localStorage失败({origin}): {str(e)}")

    async def _maybe_bootstrap_persistent_session(self) -> bool:
        """若使用 persistent profile 且未登录，尝试用 storage_state/cookies 文件引导一次登录态。"""
        try:
            if not bool(getattr(self, "_use_persistent_context", False)):
                return False
            if not self.context or not self.page:
                return False

            # 已登录则不覆盖
            try:
                if await self._is_creator_logged_in():
                    return False
            except Exception:
                pass

            state_path = str(getattr(self, "storage_state_file", "") or "").strip()
            state = None
            if state_path and os.path.exists(state_path) and os.path.getsize(state_path) > 0:
                try:
                    with open(state_path, "r", encoding="utf-8") as f:
                        state = json.load(f)
                except Exception:
                    state = None

            if isinstance(state, dict):
                await self._restore_storage_state_to_context(state)
            else:
                # 兜底：只有 cookies 文件也尽量恢复一次
                try:
                    await self._load_cookies()
                except Exception:
                    pass

            # 再探测一次
            try:
                await self._warmup_xhs_sso()
            except Exception:
                pass

            try:
                if await self._is_creator_logged_in():
                    try:
                        await self._save_cookies()
                    except Exception:
                        pass
                    try:
                        await self._save_storage_state()
                    except Exception:
                        pass
                    return True
            except Exception:
                pass

            return False
        except Exception:
            return False

    async def login(self, phone, country_code="+86"):
        """登录小红书"""
        await self.ensure_browser()  # 确保浏览器已初始化
        # 注意：token 目前仅用于本地缓存标记，并不等同于 Web 登录态；
        # 不能因为存在 token 就跳过 cookies/短信登录流程，否则会出现页面 401/跳转登录却未察觉。

        self._reset_auth_issue()

        # 若当前浏览器会话已登录，则不要清 cookies（否则容易把完整 storage_state 打回只剩不完整 cookies）
        try:
            await self.page.goto("https://creator.xiaohongshu.com/new/home", wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1)
        except Exception:
            pass
        already_logged_in = False
        try:
            if await self._is_creator_logged_in():
                already_logged_in = True
            elif self.page and ("login" not in (self.page.url or "")) and not self._auth_issue:
                already_logged_in = True
        except Exception:
            already_logged_in = False

        if already_logged_in:
            print("检测到已登录，跳过登录流程")
            await self._warmup_xhs_sso()
            await self._save_cookies()
            await self._save_storage_state()
            return

        async def maybe_clear_cookies(*, reason: str) -> None:
            """Avoid wiping a user's real Chrome profile unless explicitly allowed."""
            if not self.context:
                return

            # Non-persistent contexts are always app-owned; safe to clear.
            if not bool(getattr(self, "_use_persistent_context", False)):
                await self.context.clear_cookies()
                return

            allow = self._is_truthy(self._get_env_value("XHS_ALLOW_CLEAR_COOKIES", None), default=False)
            managed_dir = str(getattr(self, "_managed_chrome_user_data_dir", "") or "").strip()
            current_dir = str(getattr(self, "_chrome_user_data_dir", "") or "").strip()

            is_managed = False
            if managed_dir and current_dir:
                try:
                    managed_abs = os.path.abspath(os.path.expanduser(managed_dir))
                    current_abs = os.path.abspath(os.path.expanduser(current_dir))
                    is_managed = os.path.commonpath([managed_abs, current_abs]) == managed_abs
                except Exception:
                    is_managed = False

            if is_managed or allow:
                await self.context.clear_cookies()
                return

            print(
                f"检测到使用 persistent profile 且可能为外部 Chrome Profile，跳过 clear_cookies（reason={reason}）。"
                "如确需清理，请在 .env 设置 XHS_ALLOW_CLEAR_COOKIES=true。"
            )

        # 尝试加载cookies进行登录
        await self.page.goto("https://creator.xiaohongshu.com/login", wait_until="domcontentloaded")
        # 先清除所有cookies
        await maybe_clear_cookies(reason="cookie_login")
        
        # 重新加载cookies
        await self._load_cookies()
        # 刷新页面并等待加载完成
        await self.page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(1.5)

        # 检查是否已经登录
        current_url = self.page.url
        cookie_login_ok = False
        try:
            cookie_login_ok = await self._is_creator_logged_in()
        except Exception:
            cookie_login_ok = False
        if cookie_login_ok or ("login" not in current_url and not self._auth_issue):
            print("使用cookies登录成功")
            self.token = self._load_token()
            await self._warmup_xhs_sso()
            await self._save_cookies()
            await self._save_storage_state()
            return
        else:
            # 清理无效的cookies
            await maybe_clear_cookies(reason="cookie_login_failed")
            
        # 如果cookies登录失败，则进行手动登录
        await self.page.goto("https://creator.xiaohongshu.com/login", wait_until="domcontentloaded")
        await asyncio.sleep(1)

        # 尝试切到“短信/验证码登录”tab（页面结构经常变化，尽量用文本匹配）
        try:
            tab_candidates = [
                "text=手机验证码登录",
                "text=短信验证码登录",
                "text=验证码登录",
                "text=手机号登录",
            ]
            for tab in tab_candidates:
                loc = self.page.locator(tab).first
                if await loc.count() > 0:
                    try:
                        await loc.click(timeout=1500)
                        await asyncio.sleep(0.4)
                        break
                    except Exception:
                        continue
        except Exception:
            pass

        # 输入手机号（多 selector 兜底）
        phone_selectors = [
            "input[placeholder*='手机号']",
            "input[placeholder*='手机']",
            "input[type='tel']",
            "//input[contains(@placeholder,'手机号')]",
        ]
        phone_filled = False
        for sel in phone_selectors:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() <= 0:
                    continue
                await loc.fill(str(phone))
                phone_filled = True
                break
            except Exception:
                continue
        if not phone_filled:
            print("未找到手机号输入框，可能为扫码登录模式；请在打开的浏览器中手动完成登录...")
            ok = await self._wait_until_creator_logged_in(timeout_s=180)
            if not ok:
                raise Exception("登录失败：未找到手机号输入框且未在限定时间内完成手动登录")
            await self._warmup_xhs_sso()
            await self._save_cookies()
            await self._save_storage_state()
            return

        await asyncio.sleep(2)
        # 点击发送验证码按钮
        sent = False
        send_selectors = [
            ".css-uyobdj",
            ".css-1vfl29",
            "button:has-text('发送验证码')",
            "button:has-text('获取验证码')",
            "//button[contains(text(),'发送验证码')]",
            "//button[contains(text(),'获取验证码')]",
        ]
        for sel in send_selectors:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() <= 0:
                    continue
                await loc.click(timeout=3000)
                sent = True
                break
            except Exception:
                continue
        if not sent:
            print("无法自动发送验证码，请在浏览器中手动点击发送验证码并完成登录（或改用扫码登录）...")
            ok = await self._wait_until_creator_logged_in(timeout_s=180)
            if not ok:
                raise Exception("登录失败：未能自动发送验证码且未在限定时间内完成手动登录")
            await self._warmup_xhs_sso()
            await self._save_cookies()
            await self._save_storage_state()
            return

        # 使用信号机制获取验证码
        verification_code = await self.verification_handler.get_verification_code()
        if not verification_code:
            # 允许用户在浏览器中走“扫码/风控”等其它登录路径：不把“取消输入验证码”视为失败。
            print("未输入验证码，将等待你在浏览器中继续完成登录（扫码/风控验证等）...")
            ok = await self._wait_until_creator_logged_in(timeout_s=300)
            if not ok:
                raise Exception("登录未完成：未输入验证码且未在限定时间内完成手动登录")
            await self._warmup_xhs_sso()
            await self._save_cookies()
            await self._save_storage_state()
            return

        code_selectors = [
            "input[placeholder*='验证码']",
            "input[type='number']",
            "input[inputmode='numeric']",
            "//input[contains(@placeholder,'验证码')]",
        ]
        code_filled = False
        for sel in code_selectors:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() <= 0:
                    continue
                await loc.fill(str(verification_code))
                code_filled = True
                break
            except Exception:
                continue
        if not code_filled:
            raise Exception("无法找到验证码输入框，请检查登录页是否改版")

        # 点击登录按钮
        clicked_login = False
        login_selectors = [
            ".beer-login-btn",
            "button:has-text('登录')",
            "button:has-text('立即登录')",
            "//button[contains(text(),'登录')]",
        ]
        for sel in login_selectors:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() <= 0:
                    continue
                await loc.click(timeout=5000)
                clicked_login = True
                break
            except Exception:
                continue
        if not clicked_login:
            print("未找到登录按钮，请在浏览器中手动点击登录...")

        # 等待登录成功（若自动流程失败，给用户时间在打开的浏览器里手动完成登录）
        print("等待登录完成（如需扫码/确认，请在打开的浏览器中操作）...")
        ok = await self._wait_until_creator_logged_in(timeout_s=180)
        if not ok:
            raise Exception("登录超时或失败：仍停留在登录页，请确认账号是否在该浏览器窗口内完成登录")

        await self._warmup_xhs_sso()
        # 保存cookies
        await self._save_cookies()
        await self._save_storage_state()

    async def post_article(self, title, content, images=None, auto_publish: bool = False):
        """发布文章
        Args:
            title: 文章标题
            content: 文章内容
            images: 图片路径列表
            auto_publish: 是否自动点击最终“发布”按钮（无人值守）
        """
        await self.ensure_browser()  # 确保浏览器已初始化
        
        try:
            # 每次发布前重置登录态异常标记，避免历史请求残留影响本次判断
            self._auth_issue = False
            self._auth_issue_url = None

            async def safe_screenshot(path: str, timeout_ms: int = 8000) -> None:
                try:
                    if self.page:
                        await self.page.screenshot(path=path, timeout=timeout_ms)
                except Exception as e:
                    print(f"截图失败({path}): {e}")

            # 首先导航到创作者中心
            print("导航到创作者中心...")
            await self.page.goto("https://creator.xiaohongshu.com/new/home", wait_until="domcontentloaded")
            await asyncio.sleep(3)
            
            # 检查是否需要登录
            current_url = self.page.url
            if "login" in current_url or self._auth_issue:
                print("需要重新登录...尝试自动恢复登录态")
                phone = self._get_user_phone()
                if phone:
                    await self.login(phone)
                else:
                    print("未找到当前用户手机号，无法自动登录。")

                # 登录后重新进入创作者中心验证
                self._reset_auth_issue()
                await asyncio.sleep(1.0)
                await self.page.goto("https://creator.xiaohongshu.com/new/home", wait_until="domcontentloaded")
                await asyncio.sleep(2.0)

                current_url = self.page.url
                if "login" in current_url or self._auth_issue:
                    try:
                        await self._dump_page_debug(tag="auth_required", include_cookies=True)
                    except Exception:
                        pass
                    raise Exception(f"用户未登录或登录态失效，请先登录: {self._auth_issue_url or current_url}")

            # 确保 www 域名也处于登录态（上传前置加密接口在 www 域名）
            await self._warmup_xhs_sso()
            
            # 直接导航到发布页（跳过点击"发布笔记"按钮，更稳定不受改版影响）
            print("直接导航到发布页...")
            await self.page.goto("https://creator.xiaohongshu.com/publish/publish", wait_until="domcontentloaded")
            await asyncio.sleep(3)

            # 点击"上传图文"选项卡（发布页可能默认是视频 tab）
            print("切换到上传图文选项卡...")
            try:
                tab = self.page.locator(".creator-tab:has-text('上传图文')").last
                await tab.wait_for(state="visible", timeout=8000)
                if "active" not in (await tab.get_attribute("class") or ""):
                    await tab.click()
                    print("已点击'上传图文'选项卡")
                    await asyncio.sleep(2)
                else:
                    print("'上传图文'选项卡已是当前激活状态，无需切换")
            except Exception as e:
                print(f"切换上传图文选项卡失败: {e}")
                await safe_screenshot("debug_tabs.png")
            
            # 上传图片（如果有）
            print("--- 开始图片上传流程 ---")
            if images:
                print("--- 开始图片上传流程 ---")
                upload_success = False
                try:
                    # 等待上传区域关键元素（如上传按钮）出现
                    print("等待上传按钮 '.upload-button' 出现...")
                    await self.page.wait_for_selector(".upload-button", timeout=20000) 
                    await asyncio.sleep(1.5) # 短暂稳定延时
                    if self._auth_issue or ("login" in (self.page.url or "")):
                        _bad_url = self._auth_issue_url or self.page.url
                        print(f"检测到登录态异常/跳转登录，无法继续上传: {_bad_url}")
                        raise RuntimeError(f"登录态已失效，请重新登录后再发布（{_bad_url}）")

                    upload_check_js = '''
	                        () => {
	                            const indicators = [
                                /* Element Plus / picture-card upload list */
                                '.el-upload-list__item',
                                '.el-upload-list__item-thumbnail',
                                /* 预览 blob 图片（最可靠） */
                                'img[src^="blob:"]',
                                /* 小红书笔记图片项（页面结构变化时兜底） */
                                '.note-image-item',
                            ];
                            let foundVisible = false;
                            for (let selector of indicators) {
                                const elements = document.querySelectorAll(selector);
                                if (elements.length > 0) {
                                    for (let el of elements) {
                                        const rect = el.getBoundingClientRect();
                                        const style = getComputedStyle(el);
                                        if (rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                                            foundVisible = true;
                                            break;
                                        }
                                    }
                                }
                                if (foundVisible) break;
                            }
                            return foundVisible;
                        }
                    '''

                    # 上传成功后通常会出现预览缩略图，或直接进入“标题/正文”编辑区
                    title_ready_selectors = [
                        "input.d-text[placeholder='填写标题会有更多赞哦～']",
                        "input[placeholder='填写标题会有更多赞哦～']",
                        "[data-placeholder='标题']",
                    ]
                    title_ready_selector = ", ".join(title_ready_selectors)

                    async def wait_for_upload_ready(timeout_ms: int = 60000) -> bool:
                        deadline = time.time() + (timeout_ms / 1000.0)
                        while time.time() < deadline:
                            # 一旦页面被 401 触发跳转登录，后续上传/预览必然失败，直接提前结束
                            if self._auth_issue or ("login" in (self.page.url or "")):
                                return False

                            try:
                                if await self.page.evaluate(upload_check_js):
                                    return True
                            except Exception:
                                pass

                            try:
                                if await self.page.locator(title_ready_selector).first.is_visible():
                                    return True
                            except Exception:
                                pass

                            await asyncio.sleep(0.5)
                        return False

                    async def get_upload_feedback_texts() -> list:
                        try:
                            return await self.page.evaluate(
                                """
                                () => {
                                  const selectors = [
                                    '.el-message__content',
                                    '.el-notification__content',
                                    '.el-alert__content',
                                    '.el-upload__tip',
                                    '[role="alert"]',
                                    '[class*="toast"]',
                                    '[class*="Toast"]',
                                  ];
                                  const texts = [];
                                  for (const sel of selectors) {
                                    for (const el of document.querySelectorAll(sel)) {
                                      const style = getComputedStyle(el);
                                      if (style && (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')) continue;
                                      const t = (el.innerText || el.textContent || '').trim();
                                      if (t) texts.push(t);
                                    }
                                  }
                                  return Array.from(new Set(texts)).slice(0, 8);
                                }
                                """
                            )
                        except Exception:
                            return []

                    async def dump_upload_debug(tag: str) -> None:
                        try:
                            info = await self.page.evaluate(
                                """
                                () => {
                                  const rectObj = (el) => {
                                    const r = el.getBoundingClientRect();
                                    return { x: r.x, y: r.y, w: r.width, h: r.height };
                                  };

                                  const inputs = Array.from(document.querySelectorAll('input[type="file"]')).slice(0, 10).map((i) => ({
                                    className: i.className,
                                    accept: i.accept,
                                    multiple: !!i.multiple,
                                    disabled: !!i.disabled,
                                    files: i.files ? i.files.length : null,
                                    rect: rectObj(i),
                                  }));

                                  const buttons = Array.from(document.querySelectorAll('.upload-button')).slice(0, 10).map((b) => ({
                                    text: (b.innerText || b.textContent || '').trim(),
                                    ariaDisabled: b.getAttribute('aria-disabled'),
                                    disabled: !!b.disabled,
                                    rect: rectObj(b),
                                  }));

                                  const uploadItems = document.querySelectorAll('.el-upload-list__item, .el-upload-list__item-thumbnail').length;
                                  return { url: location.href, uploadItems, inputs, buttons };
                                }
                                """
                            )
                            print(f"[upload-debug:{tag}] {info}")
                        except Exception:
                            pass

                    # 尽量把 selector 限定在可见上传区域内，避免页面上存在多个同名 input 误命中
                    upload_scope = self.page
                    try:
                        wrapper_loc = self.page.locator(".wrapper")
                        wrapper_count = await wrapper_loc.count()
                        for i in range(min(wrapper_count, 5)):
                            candidate = wrapper_loc.nth(i)
                            try:
                                if await candidate.is_visible():
                                    upload_scope = candidate
                                    break
                            except Exception:
                                upload_scope = candidate
                                break
                    except Exception:
                        upload_scope = self.page

                    async def try_set_input_files(selector: str, label: str) -> bool:
                        try:
                            loc = upload_scope.locator(selector)
                            count = await loc.count()
                            if count <= 0:
                                return False
                            candidate_indices = list(range(count))
                            try:
                                infos = await loc.evaluate_all(
                                    """
                                    (els) => els.map((el) => {
                                      const r = el.getBoundingClientRect();
                                      const s = getComputedStyle(el);
                                      return {
                                        accept: (el.getAttribute('accept') || ''),
                                        disabled: !!el.disabled,
                                        multiple: !!el.multiple,
                                        area: Math.max(0, r.width) * Math.max(0, r.height),
                                        display: s.display,
                                        visibility: s.visibility,
                                        opacity: s.opacity,
                                        pointerEvents: s.pointerEvents,
                                      };
                                    })
                                    """
                                )

                                def _score(info: dict) -> tuple:
                                    accept = str(info.get("accept") or "").lower()
                                    accept_score = 0
                                    if "image" in accept:
                                        accept_score += 2
                                    for ext in (".jpg", ".jpeg", ".png", ".webp"):
                                        if ext in accept:
                                            accept_score += 1

                                    visible_score = 0
                                    if info.get("area", 0) > 0:
                                        if info.get("display") != "none" and info.get("visibility") != "hidden" and str(info.get("opacity")) != "0":
                                            visible_score = 1

                                    pointer_score = 1 if info.get("pointerEvents") != "none" else 0
                                    enabled_score = 1 if not info.get("disabled") else 0
                                    multiple_score = 1 if info.get("multiple") else 0
                                    area = int(info.get("area", 0) or 0)
                                    return (enabled_score, accept_score, visible_score, pointer_score, multiple_score, area)

                                candidate_indices = sorted(range(len(infos)), key=lambda i: _score(infos[i]), reverse=True)
                            except Exception:
                                candidate_indices = list(range(count))

                            for i in candidate_indices:
                                try:
                                    nth = loc.nth(i)
                                    try:
                                        await nth.scroll_into_view_if_needed()
                                    except Exception:
                                        pass
                                    await nth.set_input_files(images, timeout=15000)
                                    try:
                                        files_len = await nth.evaluate("el => el.files ? el.files.length : 0")
                                        print(f" {label}已设置文件: selector={selector} nth={i} files_len={files_len}")
                                        if int(files_len or 0) <= 0:
                                            continue
                                    except Exception:
                                        pass
                                    try:
                                        await nth.dispatch_event("input")
                                        await nth.dispatch_event("change")
                                    except Exception:
                                        pass
                                    if await wait_for_upload_ready(timeout_ms=60000):
                                        print(f" {label}成功: selector={selector} nth={i}")
                                        return True
                                    else:
                                        print(f" {label}已选择文件但未检测到预览: selector={selector} nth={i}")
                                        if self._auth_issue or ("login" in (self.page.url or "")):
                                            print(f" {label}检测到登录态异常/跳转登录: {self._auth_issue_url or self.page.url}")
                                        texts = await get_upload_feedback_texts()
                                        if texts:
                                            print(f" {label}页面提示: {texts}")
                                except Exception as inner_e:
                                    print(f" {label}失败: selector={selector} nth={i} err={inner_e}")
                            return False
                        except Exception as e:
                            print(f" {label}失败: selector={selector} err={e}")
                            return False

                    async def try_set_input_files_by_hit_test(label: str) -> bool:
                        marker_attr = "data-codex-upload-hit"
                        try:
                            btn = self.page.locator(".upload-button:has-text('上传图片')").first
                            if await btn.count() <= 0:
                                btn = self.page.locator(".upload-button").first
                            if await btn.count() <= 0:
                                return False
                            try:
                                await btn.scroll_into_view_if_needed()
                            except Exception:
                                pass
                            box = await btn.bounding_box()
                            if not box:
                                return False
                            cx = box["x"] + (box.get("width", 0) or 0) / 2
                            cy = box["y"] + (box.get("height", 0) or 0) / 2
                            marked = await self.page.evaluate(
                                """
                                ([x, y, markerAttr]) => {
                                  const el = document.elementFromPoint(x, y);
                                  if (!el) return false;
                                  let input = null;
                                  if (el.tagName && el.tagName.toLowerCase() === 'input' && el.type === 'file') {
                                    input = el;
                                  } else if (el.closest) {
                                    input = el.closest('input[type="file"]');
                                  }
                                  if (!input) return false;
                                  input.setAttribute(markerAttr, '1');
                                  return true;
                                }
                                """,
                                [cx, cy, marker_attr],
                            )
                            if not marked:
                                return False

                            target = self.page.locator(f'input[type="file"][{marker_attr}="1"]').first
                            if await target.count() <= 0:
                                return False

                            await target.set_input_files(images, timeout=15000)
                            try:
                                files_len = await target.evaluate("el => el.files ? el.files.length : 0")
                                print(f" {label}已设置文件: files_len={files_len}")
                                if int(files_len or 0) <= 0:
                                    return False
                            except Exception:
                                pass
                            try:
                                await target.dispatch_event("input")
                                await target.dispatch_event("change")
                            except Exception:
                                pass
                            if await wait_for_upload_ready(timeout_ms=60000):
                                print(f" {label}成功: hit-test input")
                                return True
                            texts = await get_upload_feedback_texts()
                            if texts:
                                print(f" {label}页面提示: {texts}")
                            if self._auth_issue or ("login" in (self.page.url or "")):
                                print(f" {label}检测到登录态异常/跳转登录: {self._auth_issue_url or self.page.url}")
                            return False
                        except Exception as e:
                            print(f" {label}失败: {e}")
                            return False
                        finally:
                            try:
                                await self.page.evaluate(
                                    """
                                    (markerAttr) => {
                                      for (const el of document.querySelectorAll(`input[type="file"][${markerAttr}]`)) {
                                        el.removeAttribute(markerAttr);
                                      }
                                    }
                                    """,
                                    marker_attr,
                                )
                            except Exception:
                                pass

                    async def try_file_chooser_click(click_selector: str, label: str, click_timeout: int = 7000) -> bool:
                        try:
                            await self.page.wait_for_selector(click_selector, state="visible", timeout=10000)
                            async with self.page.expect_file_chooser(timeout=15000) as fc_info:
                                await self.page.click(click_selector, timeout=click_timeout)
                            file_chooser = await fc_info.value
                            await file_chooser.set_files(images)
                            if await wait_for_upload_ready(timeout_ms=60000):
                                print(f" {label}成功: 点击 {click_selector} 并设置文件")
                                return True
                            print(f" {label}已设置文件但未检测到预览: 点击 {click_selector}")
                            if self._auth_issue or ("login" in (self.page.url or "")):
                                print(f" {label}检测到登录态异常/跳转登录: {self._auth_issue_url or self.page.url}")
                            return False
                        except Exception as e:
                            print(f" {label}失败: {e}")
                            return False

                    async def prime_upload_mode() -> None:
                        # 先“激活”上传模式（不使用真实 click，避免弹出系统文件选择框）
                        # 有些页面逻辑会在点击按钮时初始化状态（如裁剪比例等），否则 set_input_files 后可能不触发上传。
                        try:
                            btn = upload_scope.locator(".upload-button", has_text="上传图片").first
                            if await btn.count() > 0:
                                await btn.dispatch_event("click")
                                await asyncio.sleep(0.1)
                        except Exception:
                            pass
                    
                    # --- 方法0 (优先): 直接对 <input type=file> 执行 set_input_files ---
                    if not upload_success:
                        print("尝试方法0: 直接对上传 input 执行 set_input_files（避免按钮被 input 覆盖导致 click 失败）")
                        await prime_upload_mode()
                        upload_success = await try_set_input_files_by_hit_test(" 方法0-hit")
                        try:
                            await upload_scope.locator("input[type='file']").first.wait_for(state="attached", timeout=8000)
                        except Exception:
                            pass
                        input_selectors = [
                            ".upload-input",
                            "input[type='file'][accept*='.jpg']",
                            "input[type='file'][multiple]"
                        ]
                        for sel in input_selectors:
                            if upload_success:
                                break
                            await prime_upload_mode()
                            upload_success = await try_set_input_files(sel, " 方法0")

                    use_file_chooser_fallback = str(self._get_env_value("use_file_chooser_fallback", "") or "").strip().lower() in (
                        "1",
                        "true",
                        "yes",
                        "y",
                        "on",
                    )

                    # 说明：在有界面模式下点击会弹出系统文件选择框，容易让用户误以为需要手动操作；
                    # 默认仅使用 set_input_files（不弹窗），如需回退点击策略可在环境配置中开启 use_file_chooser_fallback=true。
                    if not use_file_chooser_fallback:
                        if not upload_success:
                            print("跳过点击/文件选择器回退（use_file_chooser_fallback 未开启），仅使用 set_input_files。")
                    else:
                        # --- 方法0.2 (备选): 点击真实的 input 触发 file chooser ---
                        if not upload_success:
                            print("尝试方法0.2: 点击 '.upload-input' 触发文件选择器")
                            upload_success = await try_file_chooser_click(".upload-input", " 方法0.2")
                            if not upload_success and self.page:
                                await safe_screenshot("debug_upload_input_click_failed.png")

                        # --- 方法0.5 (新增): 点击拖拽区域的文字提示区 ---
                        if not upload_success:
                            print("尝试方法0.5: 点击拖拽提示区域 ( '.wrapper' 或 '.drag-over')")
                            try:
                                clickable_area_selectors = [".wrapper", ".drag-over"]
                                clicked_area_successfully = False
                                for area_selector in clickable_area_selectors:
                                    try:
                                        print(f"尝试点击区域: '{area_selector}'")
                                        await self.page.wait_for_selector(area_selector, state="visible", timeout=5000)
                                        print(f"区域 '{area_selector}' 可见，准备点击.")
                                        async with self.page.expect_file_chooser(timeout=10000) as fc_info:
                                            await self.page.click(area_selector, timeout=5000)
                                            print(f"已点击区域 '{area_selector}'. 等待文件选择器...")
                                        file_chooser = await fc_info.value
                                        print(f"文件选择器已出现 (点击区域 '{area_selector}'): {file_chooser}")
                                        await file_chooser.set_files(images)
                                        print(f"已通过文件选择器 (点击区域 '{area_selector}') 设置文件: {images}")
                                        if await wait_for_upload_ready(timeout_ms=60000):
                                            upload_success = True
                                            clicked_area_successfully = True
                                            print(f" 方法0.5成功: 点击区域 '{area_selector}' 并设置文件")
                                            break
                                        else:
                                            print(f" 方法0.5已设置文件但未检测到预览: 点击区域 '{area_selector}'")
                                    except Exception as inner_e:
                                        print(f"尝试点击区域 '{area_selector}' 失败: {inner_e}")
                                
                                if not clicked_area_successfully: 
                                    print(f" 方法0.5 (点击拖拽提示区域) 所有内部尝试均失败")
                                    await safe_screenshot("debug_upload_all_area_clicks_failed.png")
                                    
                            except Exception as e: 
                                print(f"❌方法0.5 (点击拖拽提示区域) 步骤发生意外错误: {e}")
                                await safe_screenshot("debug_upload_method0_5_overall_failure.png")

                    # --- 方法1 (备选): 直接操作 .upload-input (使用 set_input_files) ---
                    if not upload_success:
                        print("尝试方法1: 直接操作 '.upload-input' 使用 set_input_files")
                        try:
                            await prime_upload_mode()
                            input_selector = ".upload-input"
                            # 对于 set_input_files，元素不一定需要可见，但必须存在于DOM中
                            await self.page.wait_for_selector(input_selector, state="attached", timeout=5000)
                            print(f"找到 '{input_selector}'. 尝试通过 set_input_files 设置文件...")
                            await self.page.set_input_files(input_selector, files=images, timeout=10000)
                            print(f"已通过 set_input_files 为 '{input_selector}' 设置文件: {images}")
                            if await wait_for_upload_ready(timeout_ms=60000):
                                upload_success = True
                                print(" 方法1成功: 直接通过 set_input_files 操作 '.upload-input'")
                            else:
                                print(" 方法1已设置文件但未检测到预览")
                                if self._auth_issue or ("login" in (self.page.url or "")):
                                    print(f" 方法1检测到登录态异常/跳转登录: {self._auth_issue_url or self.page.url}")
                        except Exception as e:
                            print(f" 方法1 (set_input_files on '.upload-input') 失败: {e}")
                            await safe_screenshot("debug_upload_input_set_files_failed.png")
                    
                    # --- 方法3 (备选): JavaScript直接触发隐藏的input点击 ---
                    if use_file_chooser_fallback and not upload_success:
                        print("尝试方法3: JavaScript点击隐藏的 '.upload-input'")
                        try:
                            input_selector = ".upload-input"
                            await self.page.wait_for_selector(input_selector, state="attached", timeout=5000)
                            print(f"找到 '{input_selector}'. 尝试通过JS点击...")
                            async with self.page.expect_file_chooser(timeout=10000) as fc_info:
                                await self.page.evaluate(f"document.querySelector('{input_selector}').click();")
                                print(f"已通过JS点击 '{input_selector}'. 等待文件选择器...")
                            file_chooser = await fc_info.value
                            print(f"文件选择器已出现 (JS点击): {file_chooser}")
                            await file_chooser.set_files(images)
                            print(f"已通过文件选择器 (JS点击后) 设置文件: {images}")
                            if await wait_for_upload_ready(timeout_ms=60000):
                                upload_success = True
                                print(" 方法3成功: JavaScript点击 '.upload-input' 并设置文件")
                            else:
                                print(" 方法3已设置文件但未检测到预览")
                        except Exception as e:
                            print(f"方法3 (JavaScript点击 '.upload-input') 失败: {e}")
                            await safe_screenshot("debug_upload_js_input_click_failed.png")

                    # --- 上传后检查 --- 
                    if upload_success:
                        print("图片已通过某种方法设置/点击，进入上传后检查流程，等待处理和预览...")
                        # 这里已在各上传方法内等待过一次预览；再做一次兜底检查并留截图
                        await asyncio.sleep(2.5)
                        print("执行JS检查图片预览(兜底)...")
                        upload_check_successful = await self.page.evaluate(upload_check_js)
                        if upload_check_successful:
                            print(" 图片上传并处理成功 (检测到可见的预览元素)")
                        else:
                            print(" 图片可能未成功处理或预览未出现(JS检查失败)，请检查截图")
                            await dump_upload_debug("preview-missing")
                            await safe_screenshot("debug_upload_preview_missing_after_js_check.png")
                            upload_success = False
                    else:
                        print(" 所有主要的图片上传方法均失败。无法进行预览检查。")
                        await safe_screenshot("debug_upload_all_methods_failed_final.png")
                        await dump_upload_debug("all-methods-failed")
                        
                except Exception as e:
                    print(f"整个图片上传过程出现严重错误: {e}")
                    import traceback
                    traceback.print_exc() 
                    await safe_screenshot("debug_image_upload_critical_error_outer.png")

                # 如果调用方提供了 images，但图片未上传成功，则停止后续步骤，避免误导“已准备好”
                if not upload_success:
                    print("图片上传失败，停止后续填写标题/正文。请先确认页面能正常显示上传预览。")
                    raise RuntimeError("图片上传失败：所有上传方式均失败。请检查图片格式/网络连接，或尝试重新登录")
            
            # 输入标题和内容
            print("--- 开始输入标题和内容 ---")
            await asyncio.sleep(5)  # 给更多时间让编辑界面加载
            # time.sleep(1000) # 已移除
            # # 尝试查找并点击编辑区域以激活它
            # try:
            #     await self.page.click(".editor-wrapper", timeout=5000)
            #     print("成功点击编辑区域")
            # except:
            #     print("尝试点击编辑区域失败")
            
            # 输入标题
            print("输入标题...")
            try:
                # 使用具体的标题选择器
                title_selectors = [
                    "input.d-text[placeholder='填写标题会有更多赞哦～']",
                    "input.d-text",
                    "input[placeholder='填写标题会有更多赞哦～']",
                    "input.title",
                    "[data-placeholder='标题']",
                    "[contenteditable='true']:first-child",
                    ".note-editor-wrapper input",
                    ".edit-wrapper input"
                ]
                
                title_filled = False
                for selector in title_selectors:
                    try:
                        print(f"尝试标题选择器: {selector}")
                        await self.page.wait_for_selector(selector, timeout=5000)
                        await self.page.fill(selector, title)
                        print(f"标题输入成功，使用选择器: {selector}")
                        title_filled = True
                        break
                    except Exception as e:
                        print(f"标题选择器 {selector} 失败: {e}")
                        continue
                
                if not title_filled:
                    # 尝试使用键盘快捷键输入
                    try:
                        await self.page.keyboard.press("Tab")
                        await self.page.keyboard.type(title)
                        print("使用键盘输入标题")
                    except Exception as e:
                        print(f"键盘输入标题失败: {e}")
                        print("无法输入标题")
                    
            except Exception as e:
                print(f"标题输入失败: {e}")

            # 输入内容
            print("输入内容...")
            try:
                # 内容编辑器经常变动（TipTap/ProseMirror），优先用更稳定的“占位 data-placeholder + contenteditable”定位。
                # 参考 xhs-toolkit PR#49/#50 的选择器调整思路，但这里用 Playwright 更适配的写入方式（fill + 键盘兜底）。
                content_selectors = [
                    # TipTap/ProseMirror (new editor)
                    "div[data-placeholder*='请输入正文'] div[contenteditable='true']",
                    "div[data-placeholder*='正文描述'] div[contenteditable='true']",
                    "div[data-placeholder*='正文'] div[contenteditable='true']",
                    "div.tiptap div.ProseMirror[contenteditable='true']",
                    "div.ProseMirror[contenteditable='true']",
                    "[role='textbox'][contenteditable='true']",
                    "[contenteditable='true'][role='textbox']",
                    # Legacy fallbacks
                    "[contenteditable='true']:nth-child(2)",
                    ".note-content",
                    "[data-placeholder='添加正文']",
                    ".DraftEditor-root",
                    # Empty-state paragraph (click-to-focus fallback)
                    "div[data-placeholder*='请输入正文'] p.is-editor-empty:first-child",
                    "p.is-editor-empty:first-child",
                ]

                content_filled = False
                last_error = None
                for selector in content_selectors:
                    try:
                        print(f"尝试内容选择器: {selector}")
                        loc = self.page.locator(selector).first
                        if await loc.count() <= 0:
                            continue
                        await loc.wait_for(state="visible", timeout=8000)
                        await loc.scroll_into_view_if_needed()

                        # 尝试直接 fill（对 contenteditable 的根节点更可靠）
                        try:
                            await loc.fill(content)
                        except Exception:
                            # 某些选择器命中的是编辑器内部的 <p>，Playwright 不允许 fill，
                            # 但点击后用键盘输入可正常触发编辑器的 input 事件。
                            await loc.click(timeout=5000)
                            await asyncio.sleep(0.2)
                            mod = "Meta" if sys.platform == "darwin" else "Control"
                            try:
                                await self.page.keyboard.press(f"{mod}+A")
                                await self.page.keyboard.press("Backspace")
                            except Exception:
                                pass
                            await self.page.keyboard.insert_text(content)

                        print(f"内容输入成功，使用选择器: {selector}")
                        content_filled = True
                        break
                    except Exception as e:
                        last_error = e
                        print(f"内容选择器 {selector} 失败: {e}")
                        continue
                
                if not content_filled:
                    # 尝试使用键盘快捷键输入
                    try:
                        await self.page.keyboard.press("Tab")
                        await self.page.keyboard.press("Tab")
                        await self.page.keyboard.type(content)
                        print("使用键盘输入内容")
                    except Exception as e:
                        print(f"键盘输入内容失败: {e}")
                        print("无法输入内容")
                        if last_error:
                            print(f"内容编辑器定位最后一次错误: {last_error}")
                    
            except Exception as e:
                print(f"内容输入失败: {e}")

            # 自动/手动发布
            if auto_publish:
                print("尝试自动点击“发布”按钮（无人值守）...")
                publish_success = False

                # 可能的最终发布按钮选择器（页面结构经常变化，尽量覆盖多种情况）
                final_publish_selectors = [
                    "button:has-text('确认发布')",
                    "button:has-text('立即发布')",
                    "button:has-text('发布')",
                    "button:has-text('提交')",
                    ".submit-btn",
                    ".publish-btn",
                    ".publishButton",
                    "[data-testid='publish']",
                    # xhs-toolkit PR#50 中的 xpath（作为兜底；页面 DOM 变动会导致失效）
                    "xpath=//*[@id=\"global\"]/div/div[2]/div[2]/div[2]/button[1]",
                ]

                # 先滚动到底部，避免发布按钮不在视口内导致点击/定位异常
                try:
                    for _ in range(6):
                        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(0.2)
                except Exception:
                    pass

                initial_url = ""
                try:
                    initial_url = self.page.url or ""
                except Exception:
                    initial_url = ""

                last_error = None
                for selector in final_publish_selectors:
                    try:
                        loc = self.page.locator(selector)
                        if await loc.count() <= 0:
                            continue

                        btn = loc.last
                        await btn.wait_for(state="visible", timeout=8000)
                        await btn.scroll_into_view_if_needed()

                        # 有些按钮一开始处于禁用状态，短暂等待其变为可点击
                        for _ in range(20):
                            try:
                                if await btn.is_enabled():
                                    break
                            except Exception:
                                break
                            await asyncio.sleep(0.5)

                        try:
                            await btn.click(timeout=8000)
                        except Exception:
                            # 有些情况下按钮被遮挡/hover层拦截，尝试 JS click / dispatch_event 兜底
                            try:
                                await btn.dispatch_event("click")
                            except Exception:
                                await btn.evaluate("el => el.click()")
                        print(f"已点击最终发布按钮: {selector}")
                        publish_success = True
                        break
                    except Exception as e:
                        last_error = e
                        continue

                if not publish_success:
                    try:
                        await safe_screenshot("debug_final_publish_button.png")
                    except Exception:
                        pass
                    raise Exception(f"无法找到最终发布按钮: {last_error}")

                # 处理可能出现的“确认发布/确定”等弹窗（常见于二次确认、风控提示等）
                confirm_selectors = [
                    # Dialog-scoped selectors first (safer)
                    "div[role='dialog'] button:has-text('确认发布')",
                    "div[role='dialog'] button:has-text('确认')",
                    "div[role='dialog'] button:has-text('确定')",
                    ".el-dialog button:has-text('确认发布')",
                    ".el-dialog button:has-text('确认')",
                    ".el-dialog button:has-text('确定')",
                    ".ant-modal button:has-text('确认发布')",
                    ".ant-modal button:has-text('确认')",
                    ".ant-modal button:has-text('确定')",
                    # Global fallback (last resort)
                    "button:has-text('确认发布')",
                ]
                for selector in confirm_selectors:
                    try:
                        btn = self.page.locator(selector).last
                        if await btn.count() <= 0:
                            continue
                        await btn.wait_for(state="visible", timeout=3000)
                        await btn.scroll_into_view_if_needed()
                        try:
                            await btn.click(timeout=5000)
                        except Exception:
                            try:
                                await btn.dispatch_event("click")
                            except Exception:
                                await btn.evaluate("el => el.click()")
                        print(f"检测到发布确认弹窗，已点击: {selector}")
                        break
                    except Exception:
                        continue

                # 尝试等待“发布成功/审核中”等提示，或等待页面跳转
                success_texts = [
                    "发布成功",
                    "发布完成",
                    "审核中",
                    "发布中",
                    "已发布",
                ]
                deadline = time.time() + 30
                while time.time() < deadline:
                    # 若 401/跳转登录，直接判定失败，避免“误以为发布成功”
                    try:
                        if self._auth_issue or ("login" in (self.page.url or "")):
                            raise Exception(f"发布过程中登录态异常/跳转登录: {self._auth_issue_url or self.page.url}")
                    except Exception:
                        raise

                    for text in success_texts:
                        try:
                            if await self.page.locator(f"text={text}").first.is_visible():
                                print(f"检测到发布状态提示: {text}")
                                return True
                        except Exception:
                            pass

                    # 兜底：发布后常会返回主页/列表页；若 URL 发生变化且不再处于发布页，也认为成功
                    try:
                        cur_url = self.page.url or ""
                        if cur_url and cur_url != initial_url:
                            lowered = cur_url.lower()
                            if ("publish" not in lowered) and ("/edit" not in lowered) and ("login" not in lowered):
                                print(f"检测到发布后页面跳转: {cur_url}")
                                return True
                    except Exception:
                        pass

                    await asyncio.sleep(0.5)

                try:
                    await self.page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass

                # 若无法确认结果，仍返回 True，但保留诊断信息供排查（避免无人值守任务“假成功”没有线索）
                try:
                    await self._dump_page_debug(tag="publish_not_verified", include_cookies=False)
                except Exception:
                    pass
                print("未能在超时时间内确认发布结果（可能已发布，但页面未给出明显提示）。")
                return True

            print("文章已准备好，请在浏览器中检查并手动点击发布。")
            return True
            
        except Exception as e:
            print(f"发布文章时出错: {str(e)}")
            # 截图用于调试
            try:
                if self.page: # Check if page object exists before screenshot
                    await safe_screenshot("error_screenshot.png")
                    print("已保存错误截图: error_screenshot.png")
            except:
                pass # Ignore screenshot errors
            raise

    async def close(self, force=False):
        """关闭浏览器
        Args:
            force: 是否强制关闭浏览器，默认为False
        """
        if not force:
            return

        # 逐步 best-effort 关闭，避免其中一步抛错导致后续资源不释放（尤其是 persistent context）。
        try:
            try:
                await self._save_cookies()
            except Exception:
                pass
            try:
                await self._save_storage_state()
            except Exception:
                pass

            if self.page:
                try:
                    await self.page.close()
                except Exception:
                    pass

            if self.context:
                try:
                    await self.context.close()
                except Exception:
                    pass

            if self.browser:
                try:
                    await self.browser.close()
                except Exception:
                    pass

            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
        except Exception as e:
            logging.debug(f"关闭浏览器时出错: {str(e)}")
        finally:
            self.playwright = None
            self.browser = None
            self.context = None
            self.page = None

    async def ensure_browser(self):
        """确保浏览器已初始化"""
        if not self.playwright:
            await self.initialize()


if __name__ == "__main__":
    async def main():
        poster = XiaohongshuPoster()
        try:
            print("开始初始化...")
            await poster.initialize()
            print("初始化完成")
            
            print("开始登录...")
            await poster.login("18810788888", "+86")
            print("登录完成")
            
            print("开始发布文章...")
            await poster.post_article("测试文章", "这是一个测试内容，用于验证自动发布功能。", [r"C:\Users\Administrator\Pictures\506d9fc834d786df28971fdfa27f5ae7.jpg"])  # 提供图片路径
            print("文章发布流程完成")
            
        except Exception as e:
            print(f"程序执行出错: {str(e)}")
            import traceback
            traceback.print_exc()
            # 截图调试
            try:
                if poster.page: # Check if page object exists before screenshot
                    await poster.page.screenshot(path="error_debug.png")
                    print("已保存错误截图: error_debug.png")
            except:
                pass # Ignore screenshot errors
        finally:
            print("等待10秒后关闭浏览器...")
            await asyncio.sleep(10)
            await poster.close(force=True)
            print("程序结束")
    
    asyncio.run(main())
