"""程序入口：调度登录 -> 查询 -> 下单 -> 通知 全流程。

阶段 5 目标：
- 串联全流程
- 统一异常捕获与自动重试
- 完整日志记录
- 支持长时间无人值守运行

阶段 6 目标（可选）：
- 放票整点智能调度（config.schedule.rush_at）
- PyInstaller 打包为 exe

用法：
    python -m src.main
"""

from __future__ import annotations

import sys
import time
from datetime import datetime

from DrissionPage.errors import PageDisconnectedError

from .config import Schedule, load_config
from .login import LoginManager
from .notifier import Notifier
from .order import OrderManager, SessionExpired
from .query import TicketQuery, is_beyond_pre_sale
from .utils import lookup_release_time, next_rush_time, setup_logger, sleep_with_jitter

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


def _current_phase(sched: Schedule) -> tuple[str, float, float, str]:
    """根据当前时间和 schedule 配置，返回本轮的运行阶段。

    返回 (phase, interval, jitter, describe)：
    - phase: "idle" | "prep" | "rush"
    - interval / jitter: 本轮结束后的睡眠参数
    - describe: 打日志用的一句话说明

    未配置 rush_at 时永远返回 "idle"（行为完全等同阶段5）。
    """
    if not sched.rush_at:
        return "idle", 0.0, 0.0, ""

    now = datetime.now()
    # 先检查是否处于「冲刺窗口」内(某个整点刚过且不超过 rush_duration_seconds)
    # 不能靠 next_rush_time(它只返回未来整点,11:20:00 那一刻会返回明日 11:20)
    for t in sched.rush_at:
        hh, mm = t.split(":")
        today_target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        elapsed = (now - today_target).total_seconds()
        if 0 <= elapsed <= sched.rush_duration_seconds:
            return (
                "rush",
                sched.rush_interval_seconds,
                sched.rush_jitter_seconds,
                f"冲刺中(距 {t} 已过 {int(elapsed)}s / 共 {sched.rush_duration_seconds}s)",
            )

    # 未处于冲刺窗口,则算下一个整点还有多久
    nxt = next_rush_time(sched.rush_at, now=now)
    if nxt is None:
        return "idle", 0.0, 0.0, ""
    target, secs_until = nxt
    # 预热期
    if 0 < secs_until <= sched.prep_seconds:
        return (
            "prep",
            secs_until,
            0.0,
            f"预热等待(距 {target:%H:%M} 还有 {int(secs_until)}s)",
        )
    return "idle", 0.0, 0.0, f"离下个整点 {target:%H:%M} 还有 {int(secs_until)}s"


def _pick_candidate_target(query, trip, date, submitted: set[tuple[str, str]]):
    """从当日无票车次里选一个还未候补过的目标。

    优先按 trip.train_codes 顺序选;若 train_codes 为空,取查询结果第一个。
    返回 TrainInfo 或 None(表示没有可候补的车次或都候补过了)。
    """
    try:
        trains = query.query(date)
    except Exception:  # noqa: BLE001
        return None
    if not trains:
        return None
    # 按 train_codes 优先顺序;为空则按查询顺序
    if trip.train_codes:
        order = {code: i for i, code in enumerate(trip.train_codes)}
        trains = sorted(trains, key=lambda t: order.get(t.train_code, 999))
    for t in trains:
        if (date, t.train_code) not in submitted and t.secret_str:
            return t
    return None


def run(stop_event=None) -> int:
    """主流程。返回进程退出码（0 成功抢到，非 0 异常/未抢到）。

    stop_event: 可选的 threading.Event。GUI 调用时传入,一旦 set 主循环
    尽快退出并回收浏览器(退出码 130,与 Ctrl+C 一致)。CLI 调用不传即可。
    """
    started_at = time.time()
    logger.info("=== 12306 自动抢票脚本启动 ===")

    # 1. 加载配置
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("配置加载失败：%s", exc)
        return 2

    # 自动查询放票时刻:仅当 schedule.auto=True 且 rush_at 为空时,
    # 根据 trip.from_station 从内置表查出放票时间。手写 rush_at 优先。
    if cfg.schedule.auto and not cfg.schedule.rush_at:
        release = lookup_release_time(cfg.trip.from_station)
        if release:
            cfg.schedule.rush_at = [release]
            logger.info(
                "[调度] 已自动查出 %s 的放票时刻:%s(可在 config.yaml 手动覆盖 schedule.rush_at)",
                cfg.trip.from_station, release,
            )
        else:
            logger.warning(
                "[调度] schedule.auto=true 但内置表未收录 %s 的放票时刻,"
                "请手动填 schedule.rush_at,或去 12306 查询该站放票时间。",
                cfg.trip.from_station,
            )

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
        last_phase = ""                # 上一轮的阶段（用于状态切换时打日志）
        announced_pre_sale: set[str] = set()  # 已提示"等待开票"的日期,避免刷屏
        candidate_submitted: set[tuple[str, str]] = set()  # 已候补过的 (日期, 车次),避免重复提交
        while True:
            # 检查 GUI 停止请求(每轮开头一次)
            if stop_event is not None and stop_event.is_set():
                logger.info("收到停止信号,退出主循环。")
                return 130
            attempts += 1
            if cfg.polling.max_attempts and attempts > cfg.polling.max_attempts:
                logger.info("已达最大尝试次数 %d，退出。", cfg.polling.max_attempts)
                return 1

            # 判定当前所处阶段（idle/prep/rush），据此决定睡眠参数与是否要真的查询
            phase, phase_interval, phase_jitter, phase_desc = _current_phase(cfg.schedule)
            if phase != last_phase and phase_desc:
                logger.info("[调度] 进入 %s: %s", phase.upper(), phase_desc)
                last_phase = phase

            # 心跳日志：每 HEARTBEAT_EVERY 轮打印一次运行状态
            if attempts % HEARTBEAT_EVERY == 0:
                logger.info(
                    "[心跳] 仍在轮询，共 %d 次，运行 %s。",
                    attempts, _fmt_duration(time.time() - started_at),
                )

            # 预热阶段：不查询，直接睡到整点
            if phase == "prep":
                logger.info("[调度] 预热等待 %.1f 秒到整点...", phase_interval)
                # 用可中断的分段睡眠代替 time.sleep,以便 GUI 停止能及时生效
                sleep_with_jitter(phase_interval, 0.0, stop_event=stop_event)
                continue

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
                        # 超预售期的日期静默等待,首次提示一次,后续不再刷屏
                        if is_beyond_pre_sale(date):
                            if date not in announced_pre_sale:
                                logger.info(
                                    "[第 %d 次] %s 尚未开票(超出预售期),脚本会持续等到该日期开票。",
                                    attempts, date,
                                )
                                announced_pre_sale.add(date)
                            continue
                        # 无票:如启用候补,对未候补过的目标车次尝试提交候补
                        if cfg.trip.allow_candidate:
                            candidate_train = _pick_candidate_target(
                                query, cfg.trip, date, candidate_submitted,
                            )
                            if candidate_train is not None:
                                seat = cfg.trip.seat_types[0] if cfg.trip.seat_types else "二等座"
                                logger.info(
                                    "[第 %d 次] %s 无票,尝试候补:车次=%s 席别=%s",
                                    attempts, date, candidate_train.train_code, seat,
                                )
                                key = (date, candidate_train.train_code)
                                if order_mgr.submit_candidate(candidate_train, seat, date):
                                    candidate_submitted.add(key)
                                    notifier.notify_success(
                                        "候补已提交",
                                        f"{date} {candidate_train.train_code} 已加入候补队列,请等待 12306 通知。",
                                    )
                                    logger.info("候补提交成功,继续轮询其它日期/车次。")
                                elif order_mgr.dry_run:
                                    # 干跑模式下候补也不真提交,标记已尝试避免重复走流程
                                    candidate_submitted.add(key)
                                continue
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
                except SessionExpired as exc:
                    # 下单前 UAM 会话失效——必须走完整重登,否则同一命中会死循环
                    logger.warning("下单前发现会话失效：%s，尝试完整重登。", exc)
                    if login_mgr.login():
                        consecutive_login_fails = 0
                        logger.info("重登成功，下一轮继续。")
                    else:
                        consecutive_login_fails += 1
                        logger.warning(
                            "重登失败(连续 %d/%d 次)。",
                            consecutive_login_fails, MAX_LOGIN_RETRIES,
                        )
                        if consecutive_login_fails >= MAX_LOGIN_RETRIES:
                            logger.error("连续重登失败，放弃退出。")
                            return 5
                    # 跳过本日期，让下一轮从查询重新开始
                    continue
                except PageDisconnectedError as exc:
                    # 浏览器 CDP 连接断了(反爬弹窗/资源紧张等),重启浏览器自愈
                    logger.error("浏览器连接断开：%s，重启浏览器自愈。", exc)
                    round_had_error = True
                    try:
                        login_mgr.restart_browser()
                        # 重启后 page 是新实例,查询/下单模块内部持有的旧 page 引用失效,
                        # 重新构造(共享同一 LoginManager)。
                        query = TicketQuery(cfg.trip, page=login_mgr.page)
                        order_mgr = OrderManager(
                            cfg.passengers,
                            page=login_mgr.page,
                            dry_run=cfg.order.dry_run,
                            trip=cfg.trip,
                            login_manager=login_mgr,
                        )
                        query.load_station_map()
                        logger.info("浏览器重启成功，恢复轮询。")
                    except Exception as restart_exc:  # noqa: BLE001
                        logger.exception("重启浏览器失败：%s", restart_exc)
                        return 6
                    break  # 本日期跳出本轮,让下一轮完整重试
                except Exception as exc:  # noqa: BLE001 —— 主循环兜底，防止单点异常退出
                    round_had_error = True
                    logger.exception("本轮出现异常，稍后重试：%s", exc)

            # 睡眠：正常路径用基础间隔+抖动；rush 阶段用高频冲刺；连续多轮异常时指数退避防刷屏
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
                    time.sleep(backoff) if stop_event is None else sleep_with_jitter(backoff, 0.0, stop_event=stop_event)
                else:
                    sleep_with_jitter(cfg.polling.interval_seconds, cfg.polling.jitter_seconds, stop_event=stop_event)
            else:
                consecutive_errors = 0  # 成功一轮就重置
                if phase == "rush":
                    sleep_with_jitter(phase_interval, phase_jitter, stop_event=stop_event)
                else:
                    sleep_with_jitter(cfg.polling.interval_seconds, cfg.polling.jitter_seconds, stop_event=stop_event)
    finally:
        # 无论何种退出路径都回收浏览器进程，避免残留堆积。
        # 用循环 + 屏蔽 KeyboardInterrupt 保证第二次 Ctrl+C 不会打断清理,
        # 否则浏览器子进程可能残留、"浏览器已关闭"日志也打不出来。
        while True:
            try:
                login_mgr.close()
                logger.info("浏览器已关闭。")
                break
            except KeyboardInterrupt:
                logger.warning("清理过程被中断,继续尝试关闭浏览器...")


def main() -> None:
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        logger.info("用户中断，退出。")
        sys.exit(130)


if __name__ == "__main__":
    main()
