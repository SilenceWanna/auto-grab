"""程序入口：调度登录 -> 查询 -> 下单 -> 通知 全流程。

阶段 5 目标：
- 串联全流程
- 统一异常捕获与自动重试
- 完整日志记录
- 支持长时间无人值守运行

用法：
    python -m src.main
"""

from __future__ import annotations

import sys

from .config import load_config
from .login import LoginManager
from .notifier import Notifier
from .order import OrderManager
from .query import TicketQuery
from .utils import setup_logger, sleep_with_jitter

logger = setup_logger()


def run() -> int:
    """主流程。返回进程退出码（0 成功抢到，非 0 异常/未抢到）。"""
    logger.info("=== 12306 自动抢票脚本启动 ===")

    # 1. 加载配置
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("配置加载失败：%s", exc)
        return 2

    # 2. 初始化各模块
    login_mgr = LoginManager(cfg.account, cfg.browser)
    notifier = Notifier(cfg.notify)

    # 3. 登录
    login_mgr.start_browser()
    try:
        if not login_mgr.login():
            logger.error("登录失败，退出。")
            return 3

        query = TicketQuery(cfg.trip, page=login_mgr.page)
        order_mgr = OrderManager(cfg.passengers, page=login_mgr.page, dry_run=cfg.order.dry_run, trip=cfg.trip)
        query.load_station_map()

        # 4. 轮询抢票主循环
        attempts = 0
        while True:
            attempts += 1
            if cfg.polling.max_attempts and attempts > cfg.polling.max_attempts:
                logger.info("已达最大尝试次数 %d，退出。", cfg.polling.max_attempts)
                return 1

            for date in cfg.trip.dates:
                try:
                    if not login_mgr.is_logged_in():
                        logger.warning("登录态失效，尝试重新登录。")
                        login_mgr.login()

                    hit = query.find_available(date)
                    if hit is None:
                        logger.info("[第 %d 次] %s 暂无余票。", attempts, date)
                        continue

                    train, seat = hit
                    logger.info("发现余票：%s %s %s，尝试下单。", date, train.train_code, seat)
                    if order_mgr.submit(train, seat, date=date):
                        msg = f"{date} {train.train_code} {seat} 占座成功，请尽快支付！"
                        logger.info(msg)
                        notifier.notify_success("抢票成功", msg)
                        return 0
                    logger.info("下单未成功，继续轮询。")
                except NotImplementedError as exc:
                    # 骨架阶段：模块尚未实现，提示后退出，避免死循环刷日志
                    logger.error("功能尚未实现：%s", exc)
                    logger.error("当前为项目骨架，请按 WORKPLAN.md 逐阶段实现各模块。")
                    return 4
                except Exception as exc:  # noqa: BLE001 —— 主循环兜底，防止单点异常退出
                    logger.exception("本轮出现异常，稍后重试：%s", exc)

            sleep_with_jitter(cfg.polling.interval_seconds, cfg.polling.jitter_seconds)
    finally:
        # 无论何种退出路径都回收浏览器进程，避免残留堆积
        login_mgr.close()
        logger.info("浏览器已关闭。")


def main() -> None:
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        logger.info("用户中断，退出。")
        sys.exit(130)


if __name__ == "__main__":
    main()
