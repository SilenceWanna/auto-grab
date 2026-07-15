"""选座与提交订单。

阶段 3 目标：
- 命中余票后点击「预订」
- 勾选乘客、选择席别与座位偏好
- 提交订单并处理排队/确认弹窗
- 处理下单失败并回退到查询轮询
"""

from __future__ import annotations

import logging

from .query import TrainInfo

logger = logging.getLogger("auto-grab")


class OrderManager:
    """负责命中余票后的下单占座流程。"""

    def __init__(self, passengers: list[str], page=None):
        self.passengers = passengers
        self.page = page  # 复用登录后的 DrissionPage 会话

    def submit(self, train: TrainInfo, seat_type: str) -> bool:
        """对指定车次、席别下单，成功占座返回 True。

        TODO(阶段3):
          1. 点击「预订」进入下单页
          2. 勾选 self.passengers 中的乘客
          3. 选择 seat_type 席别与座位偏好
          4. 提交订单，处理排队/确认弹窗与可能的验证码
          5. 成功进入待支付返回 True；余票被抢空等失败返回 False
        """
        raise NotImplementedError("阶段 3 实现：提交订单")

    def _select_passengers(self) -> None:
        """在下单页勾选乘客。"""
        raise NotImplementedError("阶段 3 实现：勾选乘客")

    def _confirm_order(self) -> bool:
        """确认并提交订单，处理排队弹窗。"""
        raise NotImplementedError("阶段 3 实现：确认订单")
