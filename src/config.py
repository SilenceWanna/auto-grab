"""配置加载与校验。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.yaml"
EXAMPLE_CONFIG_PATH = CONFIG_DIR / "config.example.yaml"


@dataclass
class Account:
    username: str
    password: str
    manual_captcha: bool = True


@dataclass
class Trip:
    from_station: str
    to_station: str
    dates: list[str]
    train_codes: list[str] = field(default_factory=list)
    seat_types: list[str] = field(default_factory=list)
    allow_candidate: bool = False
    # 票种:adult(成人票,默认) / student(学生票)
    ticket_type: str = "adult"


@dataclass
class Polling:
    interval_seconds: float = 3.0
    jitter_seconds: float = 2.0
    max_attempts: int = 0


@dataclass
class Notify:
    sound: bool = True
    desktop: bool = True
    serverchan_sendkey: str = ""
    dingtalk_webhook: str = ""


@dataclass
class Browser:
    headless: bool = False
    binary_path: str = ""
    # attach 模式(v2.3.2):非 0 时不自启浏览器,而是连接用户已手动启动的
    # chrome/edge --remote-debugging-port=<该端口>. 用于绕过库的 auto-launch
    # 在某些机器上不稳的问题, 同时也能复用用户浏览器的登录态。
    attach_port: int = 0


@dataclass
class Order:
    # 是否干跑：True 时走完下单流程但不点击最终提交（不真实占座），默认安全
    dry_run: bool = True


@dataclass
class Schedule:
    """放票时段智能调度（可选）。

    留空时脚本行为与阶段5相同（一直按 polling.interval_seconds 慢刷）。
    """
    # 每日放票时间点列表，格式 "HH:MM"，如 ["08:00", "13:00", "18:30"]
    rush_at: list[str] = field(default_factory=list)
    # 若为 true 且 rush_at 为空,则根据 trip.from_station 自动查出该出发站的放票时刻。
    # 手写 rush_at 时优先使用手写值。
    auto: bool = False
    # 每个整点前多少秒开始"预热"（预登录、预打开查询页），默认 60s
    prep_seconds: int = 60
    # 整点后高频冲刺持续多少秒（默认 3 分钟）
    rush_duration_seconds: int = 180
    # 冲刺期间的查询间隔（秒），越小越快但更容易被 12306 限流
    rush_interval_seconds: float = 0.3
    rush_jitter_seconds: float = 0.4


@dataclass
class Config:
    account: Account
    trip: Trip
    passengers: list[str]
    polling: Polling
    notify: Notify
    browser: Browser
    order: Order
    schedule: Schedule


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """从 YAML 文件加载配置并做基本校验。

    TODO(阶段1+): 补充更完整的字段校验（站名合法性、日期格式、席别枚举等）。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"未找到配置文件 {path}。请先复制 {EXAMPLE_CONFIG_PATH.name} 为 config.yaml 并填写。"
        )

    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    try:
        cfg = Config(
            account=Account(**raw["account"]),
            trip=Trip(**raw["trip"]),
            passengers=list(raw["passengers"]),
            polling=Polling(**raw.get("polling", {})),
            notify=Notify(**raw.get("notify", {})),
            browser=Browser(**raw.get("browser", {})),
            order=Order(**raw.get("order", {})),
            schedule=Schedule(**raw.get("schedule", {})),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"配置文件格式有误：{exc}") from exc

    if not cfg.passengers:
        raise ValueError("至少需要配置一名乘客。")
    if not cfg.trip.dates:
        raise ValueError("至少需要配置一个乘车日期。")
    if cfg.trip.ticket_type not in ("adult", "student"):
        raise ValueError(f"trip.ticket_type 必须是 'adult' 或 'student',当前为 {cfg.trip.ticket_type!r}")
    # 校验放票时间格式 HH:MM
    import re
    for t in cfg.schedule.rush_at:
        if not re.fullmatch(r"\d{1,2}:\d{2}", t):
            raise ValueError(f"schedule.rush_at 中的 {t!r} 不是合法的 HH:MM 时间格式。")

    return cfg
