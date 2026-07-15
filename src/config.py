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


@dataclass
class Order:
    # 是否干跑：True 时走完下单流程但不点击最终提交（不真实占座），默认安全
    dry_run: bool = True


@dataclass
class Config:
    account: Account
    trip: Trip
    passengers: list[str]
    polling: Polling
    notify: Notify
    browser: Browser
    order: Order


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
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"配置文件格式有误：{exc}") from exc

    if not cfg.passengers:
        raise ValueError("至少需要配置一名乘客。")
    if not cfg.trip.dates:
        raise ValueError("至少需要配置一个乘车日期。")

    return cfg
