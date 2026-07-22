"""登录与会话保持。

基于 DrissionPage 驱动真实浏览器完成 12306 登录：
- 启动浏览器，打开登录页
- 自动填充账号密码
- 验证码优先人工介入（等待用户滑动/点选），预留打码接口
- 登录成功后持久化 cookies，实现会话复用
- 检测登录态失效并自动重登
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from DrissionPage import ChromiumOptions, ChromiumPage

from .config import Account, Browser

logger = logging.getLogger("auto-grab")

LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"
CHECK_USER_URL = "https://kyfw.12306.cn/otn/login/checkUser"

SESSION_DIR = Path(__file__).resolve().parent.parent / ".session"
COOKIES_PATH = SESSION_DIR / "cookies.json"
# 脚本专用的独立浏览器用户数据目录，与用户日常 Chrome 隔离
BROWSER_PROFILE_DIR = SESSION_DIR / "browser_profile"

# ---- 页面选择器集中管理（12306 改版时只需在此处维护）----
SEL_ACCOUNT_LOGIN_TAB = "text:账号登录"   # 从「扫码登录」切换到「账号登录」的标签
SEL_USERNAME = "#J-userName"
SEL_PASSWORD = "#J-password"
SEL_LOGIN_BTN = "#J-login"


class LoginManager:
    """负责 12306 的登录与会话生命周期。"""

    def __init__(self, account: Account, browser_cfg: Browser):
        self.account = account
        self.browser_cfg = browser_cfg
        self.page: ChromiumPage | None = None

    # ------------------------------------------------------------------
    # 浏览器
    # ------------------------------------------------------------------
    def start_browser(self) -> None:
        """启动浏览器实例，应用配置中的 headless / binary_path。

        - attach 模式(browser.attach_port > 0):连接用户已手动启动的
          chrome/edge --remote-debugging-port=<该端口>. 不自启, 不管 profile,
          直接 set_address 挂上。**首选**方案,避免 auto-launch 在部分机器上失败。
        - auto-launch 模式(默认):库自启浏览器,用独立 Profile,重试 3 次。
        """
        # attach 分支:优先尝试
        if self.browser_cfg.attach_port and self.browser_cfg.attach_port > 0:
            opts = ChromiumOptions()
            opts.set_address(f"127.0.0.1:{self.browser_cfg.attach_port}")
            try:
                self.page = ChromiumPage(opts)
                logger.info(
                    "浏览器已 attach(端口: %d,复用用户已启动的浏览器)。",
                    self.browser_cfg.attach_port,
                )
                return
            except Exception as exc:
                raise RuntimeError(
                    f"attach 到 127.0.0.1:{self.browser_cfg.attach_port} 失败:{exc}\n"
                    f"请确认已手动启动 chrome/edge 且带 --remote-debugging-port={self.browser_cfg.attach_port}"
                ) from exc

        # auto-launch 分支(默认,原逻辑)
        import shutil
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        last_err: Exception | None = None
        for attempt in range(1, 4):
            opts = ChromiumOptions()
            if self.browser_cfg.binary_path:
                opts.set_browser_path(self.browser_cfg.binary_path)
            if self.browser_cfg.headless:
                opts.headless(True)
            opts.set_user_data_path(str(BROWSER_PROFILE_DIR))
            # 使用 9333 附近的常规调试端口。经实测,45000+ 高位端口在部分企业
            # 环境(如带 EDR/防火墙的机器)会被拦截,而 9333/9222 这类"传统"调试
            # 端口通常在白名单内。
            port = 9330 + attempt
            opts.set_local_port(port)
            opts.set_argument("--disable-blink-features=AutomationControlled")
            try:
                self.page = ChromiumPage(opts)
                logger.info(
                    "浏览器已启动（独立 Profile: %s，端口: %d）。",
                    BROWSER_PROFILE_DIR, port,
                )
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("第 %d 次启动失败：%s。清理后重试...", attempt, exc)
                # 清理可能残留的 Singleton 锁
                for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                    p = BROWSER_PROFILE_DIR / lock
                    try:
                        if p.is_symlink() or p.is_file():
                            p.unlink()
                        elif p.exists():
                            shutil.rmtree(p, ignore_errors=True)
                    except OSError:
                        pass
                # 第二次失败后,Profile 可能已被写坏,整体重建
                # (cookies 存在 SESSION_DIR/cookies.json,不在此目录,不会丢)
                if attempt >= 2:
                    try:
                        shutil.rmtree(BROWSER_PROFILE_DIR, ignore_errors=True)
                        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                        logger.info("已重建 Profile 目录。")
                    except OSError:
                        pass
        raise RuntimeError(
            f"浏览器启动失败(重试 3 次)。最后错误：{last_err}\n"
            f"可能原因：\n"
            f"1) 系统上残留了同端口的自动化 chrome 进程,请在任务管理器结束所有 chrome.exe 后重试\n"
            f"2) Profile 目录被占用: {BROWSER_PROFILE_DIR}\n"
            f"3) Chrome 版本与 DrissionPage 不兼容"
        )

    def is_alive(self) -> bool:
        """快速检查浏览器 CDP 连接是否还活着（不发网络请求）。"""
        if self.page is None:
            return False
        try:
            # 读一个已缓存属性,若连接已断会立刻抛
            _ = self.page.url
            return True
        except Exception:  # noqa: BLE001
            return False

    def restart_browser(self) -> None:
        """浏览器断线自愈:关闭当前实例并重新启动。cookies 与 Profile 保留。"""
        logger.warning("浏览器连接异常,尝试重启...")
        self.close()
        self.start_browser()
        # 重启后需要重新恢复登录态
        self.login()

    def close(self) -> None:
        """关闭浏览器,同时清理可能残留的子进程,避免进程堆积。

        attach 模式(attach_port > 0)只解除引用,不 quit 用户的浏览器。
        """
        if self.page is None:
            return
        # attach 模式:只解除引用,不动用户浏览器
        if self.browser_cfg.attach_port and self.browser_cfg.attach_port > 0:
            logger.info("attach 模式,不关闭用户浏览器(仅解除引用)。")
            self.page = None
            return

        # auto-launch 模式:原逻辑,quit + 强杀子进程
        # 先收集浏览器进程的子进程(渲染/GPU/utility 等)
        child_pids: list[int] = []
        try:
            import psutil
            browser_pid = getattr(self.page, "process_id", None) or getattr(self.page.browser, "process_id", None)
            if browser_pid:
                parent = psutil.Process(browser_pid)
                child_pids = [p.pid for p in parent.children(recursive=True)]
        except Exception:  # noqa: BLE001
            pass

        try:
            self.page.quit()
        except Exception:  # noqa: BLE001
            pass
        self.page = None

        # 主进程 quit 后若子进程仍存活则强杀,防止残留
        if child_pids:
            try:
                import psutil
                for pid in child_pids:
                    try:
                        proc = psutil.Process(pid)
                        if proc.is_running():
                            proc.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    def login(self) -> bool:
        """执行登录，成功返回 True。

        流程：先尝试复用本地 cookies；失败再走账号密码 + 人工验证码登录。
        """
        if self.page is None:
            raise RuntimeError("请先调用 start_browser()。")

        # 1. 尝试复用已保存的会话。
        # 只信 restore_session 的结果——它做了完整的 UAM 握手,能验证 tk 还能用。
        # is_logged_in 只查 checkUser,当 UAM tk 已过期而 checkUser 还有效时会假阳性,
        # 导致后续 order 每次都会因 restore 失败抛 SessionExpired。
        if self._load_cookies():
            if self.restore_session():
                logger.info("已复用本地会话，无需重新登录。")
                self._save_cookies()
                return True
            logger.info("本地 cookies 已存在但 UAM 握手失败,走账密登录。")

        # 2. 账号密码登录
        logger.info("开始账号密码登录。")
        self.page.get(LOGIN_URL)
        # 12306 登录页有 nc.js 滑块+登录 SDK 等重资源,显式等待 DOM 就绪,
        # 否则脚本会在页面还没渲染时找 #J-userName 全部 timeout。
        try:
            self.page.wait.doc_loaded(timeout=15)
        except Exception:  # noqa: BLE001
            pass  # 超时也继续,后面找元素时若失败会 return False

        # 切换到「账号登录」标签（默认可能是扫码登录）
        tab = self.page.ele(SEL_ACCOUNT_LOGIN_TAB, timeout=5)
        if tab:
            tab.click()

        user_input = self.page.ele(SEL_USERNAME, timeout=10)
        pwd_input = self.page.ele(SEL_PASSWORD, timeout=10)
        if not user_input or not pwd_input:
            logger.error("未找到账号或密码输入框，页面可能已改版。")
            return False

        user_input.input(self.account.username, clear=True)
        pwd_input.input(self.account.password, clear=True)

        login_btn = self.page.ele(SEL_LOGIN_BTN, timeout=5)
        if login_btn:
            login_btn.click()

        # 3. 验证码处理
        if self.account.manual_captcha:
            self._wait_manual_login()
        else:
            # TODO(进阶): 接入第三方打码平台自动识别验证码
            logger.warning("未开启人工验证码，且暂未接入自动打码，可能无法通过验证。")

        # 4. 判断登录结果。
        # 必须两个都通过:is_logged_in 确认账号态已建立(passport cookie),
        # restore_session 确认 UAM tk 能被 otn 会话接受。
        # 只信 is_logged_in 会假阳性:tk 已过期时 checkUser 可能仍返回登录态。
        if self.is_logged_in() and self.restore_session():
            logger.info("登录成功。")
            self._save_cookies()
            return True

        logger.error("登录失败，请检查账号密码或验证码(或 UAM 握手未完成)。")
        return False

    def restore_session(self) -> bool:
        """用已保存的 passport cookie 恢复当前浏览器的 otn 会话。"""
        if self.page is None:
            return False
        try:
            result = self.page.run_js(
                """
                var post = function(url, data) {
                    var xhr = new XMLHttpRequest();
                    xhr.open('POST', url, false);
                    xhr.setRequestHeader(
                        'Content-Type',
                        'application/x-www-form-urlencoded; charset=UTF-8'
                    );
                    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
                    xhr.send(new URLSearchParams(data).toString());
                    if (xhr.status < 200 || xhr.status >= 300) {
                        return null;
                    }
                    try {
                        return JSON.parse(xhr.responseText);
                    } catch (e) {
                        return null;
                    }
                };
                var auth = post('/passport/web/auth/uamtk', {appid: 'otn'});
                if (!auth || String(auth.result_code) !== '0' || !auth.newapptk) {
                    return false;
                }
                var client = post('/otn/uamauthclient', {tk: auth.newapptk});
                if (!client || String(client.result_code) !== '0') {
                    return false;
                }
                var check = post(arguments[0], {_json_att: ''});
                return Boolean(
                    check && check.status && check.data && check.data.flag
                );
                """,
                CHECK_USER_URL,
            )
            if result:
                logger.info("已通过 UAM 握手恢复登录会话。")
            return bool(result)
        except Exception as exc:  # noqa: BLE001
            logger.debug("恢复登录会话失败：%s", exc)
            return False

    def _wait_manual_login(self, timeout: float = 180.0) -> None:
        """等待用户人工完成验证码/滑块，直到登录成功或超时。"""
        logger.info("请在浏览器中完成验证码/滑块验证（最多等待 %.0f 秒）……", timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_logged_in():
                return
            time.sleep(2)
        logger.warning("等待人工验证超时。")

    def is_logged_in(self) -> bool:
        """检查当前会话是否处于登录态。

        调用查询页自身使用的 checkUser 接口判断。页面上的 ``.login-user``
        容器在未登录时也存在，不能作为登录态判据；同时这里不能通过跳转
        用户中心来检测，否则会打断正在进行的验证码交互。
        """
        if self.page is None:
            return False
        try:
            if not str(self.page.url).startswith("https://kyfw.12306.cn/"):
                self.page.get(LOGIN_URL)

            payload = self.page.run_js(
                """
                var xhr = new XMLHttpRequest();
                xhr.open('POST', arguments[0], false);
                xhr.setRequestHeader(
                    'Content-Type',
                    'application/x-www-form-urlencoded; charset=UTF-8'
                );
                xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
                xhr.send('_json_att=');
                if (xhr.status < 200 || xhr.status >= 300) {
                    return null;
                }
                try {
                    return JSON.parse(xhr.responseText);
                } catch (e) {
                    return null;
                }
                """,
                CHECK_USER_URL,
            )
            return bool(
                isinstance(payload, dict)
                and payload.get("status")
                and isinstance(payload.get("data"), dict)
                and payload["data"].get("flag")
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("登录态检测异常：%s", exc)
            return False

    # ------------------------------------------------------------------
    # cookies 持久化
    # ------------------------------------------------------------------
    def _save_cookies(self) -> None:
        """持久化 cookies 到本地 JSON。"""
        if self.page is None:
            return
        SESSION_DIR.mkdir(exist_ok=True)
        cookies = self.page.cookies(all_domains=True, all_info=True)
        # CookiesList 可迭代为 dict，转成普通 list 存储
        data = [dict(c) for c in cookies]
        COOKIES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("已保存 %d 条 cookies 到 %s", len(data), COOKIES_PATH)

    def _load_cookies(self) -> bool:
        """加载本地 cookies 到浏览器，成功返回 True。"""
        if self.page is None or not COOKIES_PATH.exists():
            return False
        try:
            data = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取 cookies 失败：%s", exc)
            return False
        if not data:
            return False
        # 先打开域名页再注入 cookies，确保 domain 生效
        self.page.get(LOGIN_URL)
        # 用底层 CDP 注入,绕开 DrissionPage 4.2.0b9 的 page.set.cookies 递归 bug
        loaded = 0
        for c in data:
            params = {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain"),
                "path": c.get("path", "/"),
            }
            # Chrome 用 expires=-1 表示会话 cookie。把 -1 传回
            # Network.setCookie 会将其视为已过期时间，导致 uamtk、tk、
            # JSESSIONID 等关键登录 cookie 注入后立即失效。
            expires = c.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                params["expires"] = expires
            for k in ("httpOnly", "secure", "sameSite"):
                if k in c and c[k] is not None:
                    params[k] = c[k]
            try:
                self.page.run_cdp("Network.setCookie", **params)
                loaded += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("注入 cookie %s 失败：%s", c.get("name"), exc)
        if loaded == 0:
            return False
        logger.info("已加载 %d 条本地 cookies。", loaded)
        return True


if __name__ == "__main__":
    # 登录模块自测入口：
    #   python -m src.login
    # 需先复制 config/config.example.yaml 为 config/config.yaml 并填写账号。
    from .config import load_config
    from .utils import setup_logger

    setup_logger()
    cfg = load_config()
    mgr = LoginManager(cfg.account, cfg.browser)
    try:
        mgr.start_browser()
        ok = mgr.login()
        logger.info("登录自测结果：%s", "成功" if ok else "失败")
        input("按回车关闭浏览器……")
    finally:
        mgr.close()
        logger.info("浏览器已关闭。")
