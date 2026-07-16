"""余票查询。

复用登录后的浏览器会话直接访问 12306 接口：
- 加载站名 <-> 电报码映射
- 通过浏览器会话请求 leftTicket 接口（自带登录 cookie，绕过反爬）
- 解析车次列表，按配置过滤车次与席别
- 提供可配置间隔的轮询入口
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime

from .config import Trip

logger = logging.getLogger("auto-grab")

# 12306 官方公告的预售期天数(2025 年恢复至 15 天,此前一度调整为 30 天/60 天)。
# 若官方调整,只需改这一个常量。
PRE_SALE_DAYS = 15

# 站名 <-> 电报码映射表（公开静态资源，无需登录）
STATION_NAME_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
# 余票查询接口。注意 12306 的 query 接口版本路径偶尔变动（queryZ/queryA 等），
# 若返回异常可在此调整。购票种类 purpose_codes：ADULT=成人票, 0X00=学生票。
LEFT_TICKET_URL = (
    "https://kyfw.12306.cn/otn/leftTicket/query"
    "?leftTicketDTO.train_date={date}"
    "&leftTicketDTO.from_station={from_code}"
    "&leftTicketDTO.to_station={to_code}"
    "&purpose_codes={purpose_codes}"
)

# 票种 -> 12306 purpose_codes
TICKET_TYPE_CODES = {
    "adult": "ADULT",
    "student": "0X00",
}

# data.result 每行按 "|" 分隔后的字段索引（12306 标准布局）
IDX_SECRET_STR = 0
IDX_TRAIN_CODE = 3
IDX_FROM_CODE = 6
IDX_TO_CODE = 7
IDX_DEPART_TIME = 8
IDX_ARRIVE_TIME = 9
IDX_DURATION = 10
# 席别中文名 -> 字段索引
SEAT_INDEX = {
    "商务座": 32,   # 商务座/特等座共用
    "特等座": 32,
    "一等座": 31,
    "二等座": 30,
    "高级软卧": 21,
    "软卧": 23,     # 软卧/一等卧
    "动卧": 33,
    "硬卧": 28,     # 硬卧/二等卧
    "软座": 24,
    "硬座": 29,
    "无座": 26,
}
# 表示「无票」的占位值
_NO_TICKET = {"", "无", "*", "--", "－"}


@dataclass
class TrainInfo:
    """一趟车次的余票信息。"""

    train_code: str
    from_code: str
    to_code: str
    depart_time: str
    arrive_time: str
    duration: str
    seats: dict[str, str] = field(default_factory=dict)
    secret_str: str = ""

    def bookable_seat(self, preferred: list[str]) -> str | None:
        """按偏好顺序返回第一个有票的席别，无票返回 None。"""
        for seat in preferred:
            status = self.seats.get(seat, "")
            if status and status not in _NO_TICKET:
                return seat
        return None


def is_beyond_pre_sale(target_date: str, today: date_type | None = None) -> bool:
    """判定 target_date(YYYY-MM-DD)是否超出 12306 预售期(距今 > PRE_SALE_DAYS 天)。

    Args:
        target_date: 目标乘车日期,格式 YYYY-MM-DD。
        today: 参考"今天"(便于测试注入),默认取系统当前日期。

    Returns:
        True 表示超出预售期,当前查询必然为空。
    """
    if today is None:
        today = date_type.today()
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return False  # 日期格式错误,交由后续逻辑报错
    return (target - today).days > PRE_SALE_DAYS


class TicketQuery:
    """余票查询器。依赖登录后的 DrissionPage 会话。"""

    def __init__(self, trip: Trip, page=None):
        self.trip = trip
        self.page = page
        self._name_to_code: dict[str, str] = {}
        self._code_to_name: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 站名映射
    # ------------------------------------------------------------------
    def load_station_map(self) -> None:
        """加载并缓存站名->电报码映射。"""
        if self.page is None:
            raise RuntimeError("查询需要已登录的浏览器会话。")
        self.page.get(STATION_NAME_URL)
        raw = self.page.html
        # 提取 var station_names='...'; 中的内容
        start = raw.find("'")
        end = raw.rfind("'")
        if start == -1 or end <= start:
            raise ValueError("站名映射格式异常，可能页面已改版。")
        body = raw[start + 1 : end]

        count = 0
        for item in body.split("@"):
            if not item:
                continue
            fields = item.split("|")
            if len(fields) < 5:
                continue
            name, code = fields[1], fields[2]
            self._name_to_code[name] = code
            self._code_to_name[code] = name
            count += 1
        logger.info("已加载 %d 个车站编码。", count)

    def station_code(self, name: str) -> str:
        """将中文站名转为电报码。"""
        if not self._name_to_code:
            self.load_station_map()
        code = self._name_to_code.get(name)
        if not code:
            raise ValueError(f"未找到车站「{name}」的编码，请检查站名是否正确。")
        return code

    # ------------------------------------------------------------------
    # 余票查询
    # ------------------------------------------------------------------
    def query(self, date: str) -> list[TrainInfo]:
        """查询指定日期的余票，返回按 train_codes 过滤后的车次列表。"""
        if self.page is None:
            raise RuntimeError("查询需要已登录的浏览器会话。")

        # 若日期超出 12306 预售期,直接返回空(避免向服务端发送必然无结果的请求)。
        # 由主循环负责根据 schedule 等待放票时刻。
        if is_beyond_pre_sale(date):
            logger.debug("%s 尚未开票(超出预售期 %d 天),跳过查询。", date, PRE_SALE_DAYS)
            return []

        from_code = self.station_code(self.trip.from_station)
        to_code = self.station_code(self.trip.to_station)
        purpose_codes = TICKET_TYPE_CODES.get(self.trip.ticket_type, "ADULT")
        url = LEFT_TICKET_URL.format(
            date=date, from_code=from_code, to_code=to_code, purpose_codes=purpose_codes,
        )

        self.page.get(url)
        # Chrome 的 JSON viewer 会异步把响应放进 <pre>。DrissionPage.page.json
        # 默认只等 0.5 秒，接口重定向到 queryG 时偶尔会抢在 <pre> 出现前读取。
        pre = self.page.ele("t:pre", timeout=3)
        if not pre:
            logger.warning("余票接口响应未渲染为 JSON（当前URL：%s）。", self.page.url)
            return []
        try:
            payload = json.loads(pre.text)
        except (TypeError, json.JSONDecodeError):
            logger.warning("余票接口返回了无法解析的 JSON（当前URL：%s）。", self.page.url)
            return []
        if not isinstance(payload, dict) or "data" not in payload:
            logger.warning("余票接口返回异常（可能被反爬拦截或需重新登录）。")
            return []

        result = (payload.get("data") or {}).get("result") or []
        trains = [self._parse_row(line) for line in result]
        trains = [t for t in trains if t is not None]

        # 按配置过滤目标车次
        if self.trip.train_codes:
            wanted = set(self.trip.train_codes)
            trains = [t for t in trains if t.train_code in wanted]
        return trains

    def _parse_row(self, line: str) -> TrainInfo | None:
        """解析 data.result 中的一行。"""
        f = line.split("|")
        if len(f) <= IDX_DURATION:
            return None
        seats = {name: f[idx] for name, idx in SEAT_INDEX.items() if idx < len(f)}
        return TrainInfo(
            train_code=f[IDX_TRAIN_CODE],
            from_code=f[IDX_FROM_CODE],
            to_code=f[IDX_TO_CODE],
            depart_time=f[IDX_DEPART_TIME],
            arrive_time=f[IDX_ARRIVE_TIME],
            duration=f[IDX_DURATION],
            seats=seats,
            secret_str=f[IDX_SECRET_STR],
        )

    def find_available(self, date: str) -> tuple[TrainInfo, str] | None:
        """返回第一个「命中目标车次且有偏好席别余票」的 (车次, 席别)。"""
        for train in self.query(date):
            seat = train.bookable_seat(self.trip.seat_types)
            if seat:
                return train, seat
        return None


if __name__ == "__main__":
    # 查询模块自测入口：
    #   python -m src.query
    # 会复用阶段1保存的登录会话，查询配置中的行程并打印余票。
    from .config import load_config
    from .login import LoginManager
    from .utils import setup_logger

    setup_logger()
    cfg = load_config()
    mgr = LoginManager(cfg.account, cfg.browser)
    try:
        mgr.start_browser()
        if not mgr.login():
            logger.error("登录失败，无法查询。")
            raise SystemExit(1)

        q = TicketQuery(cfg.trip, page=mgr.page)
        q.load_station_map()
        for date in cfg.trip.dates:
            logger.info("=== 查询 %s %s->%s ===", date, cfg.trip.from_station, cfg.trip.to_station)
            trains = q.query(date)
            if not trains:
                logger.info("无匹配车次或查询失败。")
            for t in trains:
                seat_str = "  ".join(f"{k}:{v}" for k, v in t.seats.items() if v not in _NO_TICKET) or "无票"
                logger.info("%s %s->%s [%s] %s", t.train_code, t.depart_time, t.arrive_time, t.duration, seat_str)
        input("按回车关闭浏览器……")
    finally:
        mgr.close()
        logger.info("浏览器已关闭。")
