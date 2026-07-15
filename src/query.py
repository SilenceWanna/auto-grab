"""余票查询。

阶段 2 目标：
- 将中文站名解析为 12306 站点编码
- 查询余票并解析车次列表
- 按配置过滤目标车次与席别
- 可配置间隔的轮询（含随机抖动）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Trip

logger = logging.getLogger("auto-grab")

# 12306 余票查询接口（返回车次与各席别余票）
LEFT_TICKET_URL = "https://kyfw.12306.cn/otn/leftTicket/query"
# 站名 <-> 站点编码 映射表
STATION_NAME_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"


@dataclass
class TrainInfo:
    """一趟车次的余票信息。"""

    train_code: str          # 车次号，如 G1
    from_station: str
    to_station: str
    depart_time: str
    arrive_time: str
    duration: str
    # 席别 -> 余票状态（"有" / "无" / 具体数字 / "候补"）
    seats: dict[str, str]
    # 提交订单所需的原始票据串（secretStr）
    secret_str: str = ""

    def bookable_seat(self, preferred: list[str]) -> str | None:
        """按偏好顺序返回第一个有票的席别，无票返回 None。"""
        for seat in preferred:
            status = self.seats.get(seat, "")
            if status and status not in ("无", "", "*", "--"):
                return seat
        return None


class TicketQuery:
    """余票查询器。"""

    def __init__(self, trip: Trip, page=None):
        self.trip = trip
        self.page = page  # 复用登录后的 DrissionPage 会话
        self._station_map: dict[str, str] = {}

    def load_station_map(self) -> None:
        """加载并缓存站名->编码映射。

        TODO(阶段2): 拉取 STATION_NAME_URL 并解析为字典。
        """
        raise NotImplementedError("阶段 2 实现：加载站点编码表")

    def station_code(self, name: str) -> str:
        """将中文站名转为站点编码。"""
        raise NotImplementedError("阶段 2 实现：站名转编码")

    def query(self, date: str) -> list[TrainInfo]:
        """查询指定日期的余票，返回过滤后的车次列表。

        TODO(阶段2):
          1. 组装查询参数（出发/到达编码、日期）
          2. 请求 LEFT_TICKET_URL 并解析响应
          3. 按 trip.train_codes 过滤车次
        """
        raise NotImplementedError("阶段 2 实现：余票查询")

    def find_available(self, date: str) -> tuple[TrainInfo, str] | None:
        """返回第一个「命中目标车次且有偏好席别余票」的 (车次, 席别)。"""
        for train in self.query(date):
            seat = train.bookable_seat(self.trip.seat_types)
            if seat:
                return train, seat
        return None
