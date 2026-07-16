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
import time

from .config import load_config
from .login import LoginManager
from .notifier import Notifier
from .order import OrderManager
from .query import TicketQuery
from .utils import setup_logger, sleep_with_jitter

logger = setup_logger()

# 心跳日志：每这么多轮轮询打印一次"仍在运行"（防止用户误以为脚本挂了）
HEARTBEAT_EVERY = 30
# 连续登录失败达到此次数则放弃退出（避免密码错误/风控时死循环重登）
MAX_LOGIN_RETRIES = 3
# 连续异常达到此次数后启用指数退避（防止刷屏）
ERROR_BACKOFF_THRESHOLD = 3
# 指数退避的最大睡眠秒数
ERROR_BACKOFF_CAP = 60


def _fmt_duration(seconds: float) -> str:
    """把秒数格式化为「M 分 S 秒」或「H 时 M 分」，用于心跳日志。"""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 时 {(seconds % 3600) // 60} 分"


def run() -> int:
    """主流程。返回进程退出码（0 成功抢到，非 0 异常/未抢到）。"""
    started_at = time.time()
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
        order_mgr = OrderManager(
            cfg.passengers,
            page=login_mgr.page,
            dry_run=cfg.order.dry_run,
            trip=cfg.trip,
            login_manager=login_mgr,
        )
        query.load_station_map()

        # 4. 轮询抢票主循环
        attempts = 0
        consecutive_errors = 0        # 连续出错次数（用于指数退避）
        consecutive_login_fails = 0    # 连续重登失败次数（达到 MAX 则放弃）
        while True:
            attempts += 1
            if cfg.polling.max_attempts and attempts > cfg.polling.max_attempts:
                logger.info("已达最大尝试次数 %d，退出。", cfg.polling.max_attempts)
                return 1

            # 心跳日志：每 HEARTBEAT_EVERY 轮打印一次运行状态
            if attempts % HEARTBEAT_EVERY == 0:
                logger.info(
                    "[心跳] 仍在轮询，共 %d 次，运行 %s。",
                    attempts, _fmt_duration(time.time() - started_at),
                )

            round_had_error = False
            for date in cfg.trip.dates:
                try:
                    if not login_mgr.is_logged_in():
                        logger.warning("登录态失效，尝试重新登录。")
                        if login_mgr.login():
                            consecutive_login_fails = 0
                        else:
                            consecutive_login_fails += 1
                            logger.warning(
                                "重登失败（连续 %d/%d 次）。",
                                consecutive_login_fails, MAX_LOGIN_RETRIES,
                            )
                            if consecutive_login_fails >= MAX_LOGIN_RETRIES:
                                logger.error("连续重登失败，放弃退出。")
                                return 5
                            continue  # 跳过本日期，下轮再试

                    hit = query.find_available(date)
                    if hit is None:
                        logger.info("[第 %d 次] %s 暂无余票。", attempts, date)
                        continue

                    train, seat = hit
                    logger.info("发现余票：%s %s %s，尝试下单。", date, train.train_code, seat)
                    if order_mgr.submit(train, seat, date=date):
                        elapsed = _fmt_duration(time.time() - started_at)
                        msg = (
                            f"{date} {train.train_code} {seat} 占座成功，请尽快支付！"
                            f"（本次抢票耗时 {elapsed}，共尝试 {attempts} 次）"
                        )
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
                    round_had_error = True
                    logger.exception("本轮出现异常，稍后重试：%s", exc)

            # 睡眠：正常路径用基础间隔+抖动；连续多轮异常时指数退避防刷屏
            if round_had_error:
                consecutive_errors += 1
                if consecutive_errors >= ERROR_BACKOFF_THRESHOLD:
                    backoff = min(
                        ERROR_BACKOFF_CAP,
                        cfg.polling.interval_seconds * (2 ** (consecutive_errors - ERROR_BACKOFF_THRESHOLD + 1)),
                    )
                    logger.warning(
                        "已连续 %d 轮出错，退避 %.1f 秒后重试。",
                        consecutive_errors, backoff,
                    )
                    time.sleep(backoff)
                else:
                    sleep_with_jitter(cfg.polling.interval_seconds, cfg.polling.jitter_seconds)
            else:
                consecutive_errors = 0  # 成功一轮就重置
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
