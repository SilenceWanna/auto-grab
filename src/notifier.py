"""抢票成功通知。

阶段 4 目标：
- 播放提示音
- 桌面通知
- 可选：Server酱 / 钉钉 推送到手机
"""

from __future__ import annotations

import logging

from .config import Notify

logger = logging.getLogger("auto-grab")


class Notifier:
    """多渠道通知：声音 + 桌面 + 手机推送。"""

    def __init__(self, cfg: Notify):
        self.cfg = cfg

    def notify_success(self, title: str, message: str) -> None:
        """抢票成功后触发所有已启用的通知渠道。"""
        if self.cfg.sound:
            self._play_sound()
        if self.cfg.desktop:
            self._desktop(title, message)
        if self.cfg.serverchan_sendkey:
            self._serverchan(title, message)
        if self.cfg.dingtalk_webhook:
            self._dingtalk(f"{title}\n{message}")

    def _play_sound(self) -> None:
        """播放提示音。

        TODO(阶段4): Windows 下可用 winsound.Beep 或播放音频文件。
        """
        raise NotImplementedError("阶段 4 实现：提示音")

    def _desktop(self, title: str, message: str) -> None:
        """发送桌面通知。

        TODO(阶段4): 用 plyer.notification.notify。
        """
        raise NotImplementedError("阶段 4 实现：桌面通知")

    def _serverchan(self, title: str, message: str) -> None:
        """通过 Server酱 推送到微信。

        TODO(阶段4): POST 到 https://sctapi.ftqq.com/{sendkey}.send
        """
        raise NotImplementedError("阶段 4 实现：Server酱推送")

    def _dingtalk(self, text: str) -> None:
        """通过钉钉群机器人 Webhook 推送。

        TODO(阶段4): POST 到 dingtalk_webhook。
        """
        raise NotImplementedError("阶段 4 实现：钉钉推送")
