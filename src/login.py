"""登录与会话保持。

阶段 1 目标：
- 启动浏览器，打开 12306 登录页
- 自动填充账号密码
- 处理滑块/图形验证码（优先人工介入）
- 登录成功后持久化 cookies，实现会话复用
- 检测登录态失效并自动重登
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Account, Browser

logger = logging.getLogger("auto-grab")

LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"
USER_CENTER_URL = "https://kyfw.12306.cn/otn/view/index.html"
COOKIES_PATH = Path(__file__).resolve().parent.parent / ".session" / "cookies.json"


class LoginManager:
    """负责 12306 的登录与会话生命周期。"""

    def __init__(self, account: Account, browser_cfg: Browser):
        self.account = account
        self.browser_cfg = browser_cfg
        self.page = None  # DrissionPage ChromiumPage 实例，阶段1接入

    def start_browser(self) -> None:
        """启动浏览器实例。

        TODO(阶段1): 用 DrissionPage 的 ChromiumPage 启动，
        应用 browser_cfg 的 headless / binary_path。
        """
        raise NotImplementedError("阶段 1 实现：启动浏览器")

    def login(self) -> bool:
        """执行登录，成功返回 True。

        TODO(阶段1):
          1. 若本地有有效 cookies，先尝试复用会话
          2. 否则打开登录页，填充账号密码
          3. manual_captcha=True 时等待用户人工过验证码
          4. 登录成功后保存 cookies
        """
        raise NotImplementedError("阶段 1 实现：登录流程")

    def is_logged_in(self) -> bool:
        """检查当前会话是否仍处于登录态。"""
        raise NotImplementedError("阶段 1 实现：登录态检测")

    def _save_cookies(self) -> None:
        """持久化 cookies 到本地。"""
        raise NotImplementedError("阶段 1 实现：保存 cookies")

    def _load_cookies(self) -> bool:
        """加载本地 cookies，成功返回 True。"""
        raise NotImplementedError("阶段 1 实现：加载 cookies")
