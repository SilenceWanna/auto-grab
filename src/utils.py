"""通用工具：日志、等待、重试。"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def setup_logger(name: str = "auto-grab", level: int = logging.INFO) -> logging.Logger:
    """初始化并返回全局 logger，同时输出到控制台与 logs/run.log。"""
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:  # 避免重复添加 handler
        return logger
    logger.setLevel(level)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.FileHandler(LOG_DIR / "run.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


def sleep_with_jitter(base: float, jitter: float) -> None:
    """休眠 base + rand(0, jitter) 秒，用于轮询防频控。"""
    time.sleep(base + random.uniform(0, jitter))


def retry(times: int = 3, delay: float = 1.0):
    """简单重试装饰器：捕获异常后重试，全部失败则抛出最后一次异常。"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 —— 骨架阶段统一兜底
                    last_exc = exc
                    logging.getLogger("auto-grab").warning(
                        "%s 第 %d/%d 次失败：%s", func.__name__, attempt, times, exc
                    )
                    time.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


def next_rush_time(
    rush_at: list[str],
    now: datetime | None = None,
) -> tuple[datetime, float] | None:
    """算出「下一个放票时间点」及其距 now 的秒数。

    rush_at: HH:MM 字符串列表，如 ["08:00", "13:00"]。
    now: 参考"当前时间"（便于测试注入），默认取系统当前。
    返回 (target_datetime, seconds_until) 或 None（列表为空时）。
    如果今天所有整点都已过，返回明天第一个。
    """
    if not rush_at:
        return None
    now = now or datetime.now()
    candidates: list[datetime] = []
    for t in rush_at:
        hh, mm = t.split(":")
        today_target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if today_target > now:
            candidates.append(today_target)
        else:
            # 今天已过，加入明天同时间
            candidates.append(today_target + timedelta(days=1))
    target = min(candidates)
    return target, (target - now).total_seconds()
