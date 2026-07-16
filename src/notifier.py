"""抢票成功通知。

多渠道同时触发：提示音 / 桌面通知 / Server酱推送到微信 / 钉钉 Webhook。
每个渠道独立异常隔离——某一路失败不影响其他路，更不能挡住"抢到票"这一事实。
"""

from __future__ import annotations

import logging
import platform
import threading

import requests

from .config import Notify

logger = logging.getLogger("auto-grab")

# 单渠道网络请求的超时（秒），避免推送阻塞抢票主流程
_HTTP_TIMEOUT = 8


class Notifier:
    """多渠道通知：声音 + 桌面 + 手机推送。"""

    def __init__(self, cfg: Notify):
        self.cfg = cfg

    def notify_success(self, title: str, message: str) -> None:
        """抢票成功后触发所有已启用的通知渠道。

        每个渠道各自异常隔离；顺序：先声音（异步）、再桌面、再网络推送。
        网络推送在同一线程串行，总耗时上限 ~16 秒（两路各 8s 超时）。
        """
        if self.cfg.sound:
            try:
                self._play_sound()
            except Exception as exc:  # noqa: BLE001
                logger.warning("提示音播放失败：%s", exc)
        if self.cfg.desktop:
            try:
                self._desktop(title, message)
            except Exception as exc:  # noqa: BLE001
                logger.warning("桌面通知失败：%s", exc)
        if self.cfg.serverchan_sendkey:
            try:
                self._serverchan(title, message)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Server酱推送失败：%s", exc)
        if self.cfg.dingtalk_webhook:
            try:
                self._dingtalk(f"{title}\n{message}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("钉钉推送失败：%s", exc)

    # ------------------------------------------------------------------
    # 提示音（Windows 用 winsound，其他平台降级到终端响铃）
    # ------------------------------------------------------------------
    def _play_sound(self) -> None:
        """播放提示音，异步执行避免阻塞后续通知。

        Windows：winsound.Beep 循环 3 声（880Hz，400ms）
        其他平台：终端响铃字符 \\a 三次
        """
        def _beep():
            try:
                if platform.system() == "Windows":
                    import winsound
                    for _ in range(3):
                        winsound.Beep(880, 400)
                else:
                    print("\a" * 3, flush=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug("_beep 内部异常：%s", exc)

        threading.Thread(target=_beep, daemon=True).start()
        logger.info("已触发提示音（异步播放）。")

    # ------------------------------------------------------------------
    # 桌面通知（跨平台，plyer）
    # ------------------------------------------------------------------
    def _desktop(self, title: str, message: str) -> None:
        from plyer import notification
        notification.notify(
            title=title,
            message=message,
            app_name="12306 抢票",
            timeout=15,  # 秒
        )
        logger.info("已发送桌面通知：%s", title)

    # ------------------------------------------------------------------
    # Server酱（推送到微信）
    # ------------------------------------------------------------------
    def _serverchan(self, title: str, message: str) -> None:
        url = f"https://sctapi.ftqq.com/{self.cfg.serverchan_sendkey}.send"
        r = requests.post(
            url,
            data={"title": title, "desp": message},
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        # Server酱 成功返回 {"code": 0, ...}，非 0 视为业务失败
        if payload.get("code") != 0:
            raise RuntimeError(f"Server酱返回 code={payload.get('code')} {payload.get('message')}")
        logger.info("已通过 Server酱 推送到微信。")

    # ------------------------------------------------------------------
    # 钉钉群机器人（Webhook）
    # ------------------------------------------------------------------
    def _dingtalk(self, text: str) -> None:
        r = requests.post(
            self.cfg.dingtalk_webhook,
            json={"msgtype": "text", "text": {"content": text}},
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        # 钉钉 成功返回 {"errcode": 0, ...}
        if payload.get("errcode") != 0:
            raise RuntimeError(f"钉钉返回 errcode={payload.get('errcode')} {payload.get('errmsg')}")
        logger.info("已通过钉钉 Webhook 推送。")


if __name__ == "__main__":
    # 通知模块自测入口（不依赖 12306）：
    #   python -m src.notifier
    # 会依配置触发已启用的渠道，向你验证提示音/桌面/推送是否可用。
    import time
    from .config import load_config
    from .utils import setup_logger

    setup_logger()
    cfg = load_config()
    n = Notifier(cfg.notify)
    logger.info("=== 通知自测：将触发已启用的渠道 ===")
    logger.info(
        "sound=%s desktop=%s serverchan=%s dingtalk=%s",
        cfg.notify.sound,
        cfg.notify.desktop,
        bool(cfg.notify.serverchan_sendkey),
        bool(cfg.notify.dingtalk_webhook),
    )
    n.notify_success("【测试】12306 抢票成功", "这是通知自测消息，用于验证渠道是否可用。")
    # 等一下让异步提示音跑完
    time.sleep(2)
    logger.info("自测结束。")
