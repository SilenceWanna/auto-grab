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
INIT_MY_URL = "https://kyfw.12306.cn/otn/view/index.html"
# 登录成功后会跳转到该页面（用于判断是否登录成功）
LOGIN_SUCCESS_URL = "https://kyfw.12306.cn/otn/view/index.html"

SESSION_DIR = Path(__file__).resolve().parent.parent / ".session"
COOKIES_PATH = SESSION_DIR / "cookies.json"

# ---- 页面选择器集中管理（12306 改版时只需在此处维护）----
SEL_ACCOUNT_LOGIN_TAB = "text:账号登录"   # 从「扫码登录」切换到「账号登录」的标签
SEL_USERNAME = "#J-userName"
SEL_PASSWORD = "#J-password"
SEL_LOGIN_BTN = "#J-login"
# 登录后用户名显示区域，作为登录态判据
SEL_LOGIN_USER = ".login-user"
# 未登录时首页会出现「登录」入口
SEL_LOGIN_ENTRY = "text:登录"


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
        """启动浏览器实例，应用配置中的 headless / binary_path。"""
        opts = ChromiumOptions()
        if self.browser_cfg.binary_path:
            opts.set_browser_path(self.browser_cfg.binary_path)
        if self.browser_cfg.headless:
            opts.headless(True)
        # 抢票场景下常用的稳定性参数
        opts.set_argument("--disable-blink-features=AutomationControlled")
        self.page = ChromiumPage(opts)
        logger.info("浏览器已启动。")

    def close(self) -> None:
        """关闭浏览器。"""
        if self.page is not None:
            self.page.quit()
            self.page = None

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    def login(self) -> bool:
        """执行登录，成功返回 True。

        流程：先尝试复用本地 cookies；失败再走账号密码 + 人工验证码登录。
        """
        if self.page is None:
            raise RuntimeError("请先调用 start_browser()。")

        # 1. 尝试复用已保存的会话
        if self._load_cookies() and self.is_logged_in():
            logger.info("已复用本地会话，无需重新登录。")
            return True

        # 2. 账号密码登录
        logger.info("开始账号密码登录。")
        self.page.get(LOGIN_URL)

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

        # 4. 判断登录结果
        if self.is_logged_in():
            logger.info("登录成功。")
            self._save_cookies()
            return True

        logger.error("登录失败，请检查账号密码或验证码。")
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

        通过访问用户中心页并检测登录用户元素判断。
        """
        if self.page is None:
            return False
        try:
            self.page.get(INIT_MY_URL)
            # 登录态下能找到用户信息元素；未登录会被重定向或出现登录入口
            return self.page.ele(SEL_LOGIN_USER, timeout=5) is not None
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
        self.page.set.cookies(data)
        logger.info("已加载 %d 条本地 cookies。", len(data))
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
    mgr.start_browser()
    ok = mgr.login()
    logger.info("登录自测结果：%s", "成功" if ok else "失败")
    input("按回车关闭浏览器……")
    mgr.close()
