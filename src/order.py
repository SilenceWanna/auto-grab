"""选座与提交订单。

下单走页面 UI 路径（比接口更稳、能自然带上会话与风控参数）：
1. 打开余票查询页，填入行程并查询
2. 在目标车次行点击「预订」
3. 在确认页勾选乘客、选择席别
4. 提交订单，处理排队/确认弹窗

安全：dry_run=True 时走到「最终提交」前停住并打印订单信息，不真实占座。
"""

from __future__ import annotations

import logging
import time

from .query import TrainInfo

logger = logging.getLogger("auto-grab")

# 余票查询页（渲染车次表格，每行带预订按钮）
LEFT_TICKET_INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc"

# ---- 选择器集中管理（12306 改版时只需维护此处）----
SEL_FROM_INPUT = "#fromStationText"
SEL_TO_INPUT = "#toStationText"
SEL_DATE_INPUT = "#train_date"
SEL_QUERY_BTN = "#query_ticket"
# 车次表格中每行的预订按钮（按车次号定位所在行）
SEL_BOOK_BTN_TPL = "@onclick^{code}"  # 兜底，实际用车次文本定位
# 确认页元素
SEL_PASSENGER_LABEL_TPL = "text:{name}"      # 乘客勾选项（按姓名文本）
SEL_SUBMIT_ORDER_BTN = "#submitOrder_id"      # 提交订单按钮
SEL_CONFIRM_QUEUE_BTN = "#qr_submit_id"       # 排队确认弹窗的确认按钮
# 席别下拉（确认页），值为 12306 席别代码
SEL_SEAT_SELECT = "#seatType_1"

# 席别中文名 -> 12306 座位类型代码
SEAT_TYPE_CODE = {
    "商务座": "9",
    "特等座": "P",
    "一等座": "M",
    "二等座": "O",
    "高级软卧": "6",
    "软卧": "4",
    "动卧": "F",
    "硬卧": "3",
    "软座": "2",
    "硬座": "1",
    "无座": "1",
}


class OrderManager:
    """负责命中余票后的下单占座流程。"""

    def __init__(self, passengers: list[str], page=None, dry_run: bool = True):
        self.passengers = passengers
        self.page = page
        self.dry_run = dry_run

    def submit(self, train: TrainInfo, seat_type: str) -> bool:
        """对指定车次、席别下单。

        dry_run=True 时返回 False（未真实占座），并打印将提交的订单信息。
        真实模式下成功进入待支付返回 True。
        """
        if self.page is None:
            raise RuntimeError("下单需要已登录的浏览器会话。")

        logger.info(
            "准备下单：车次=%s 席别=%s 乘客=%s（dry_run=%s）",
            train.train_code, seat_type, ",".join(self.passengers), self.dry_run,
        )

        # 1. 进入预订确认页
        if not self._open_booking(train):
            logger.warning("未能进入预订页（余票可能已被抢空）。")
            return False

        # 2. 勾选乘客
        self._select_passengers()

        # 3. 选择席别
        self._select_seat(seat_type)

        # 4. 提交
        if self.dry_run:
            logger.warning(
                "【干跑模式】已到最终提交前，跳过真实提交。"
                "确认无误后将 config.yaml 的 order.dry_run 改为 false 才会真实抢票。"
            )
            return False

        return self._confirm_order()

    # ------------------------------------------------------------------
    def _open_booking(self, train: TrainInfo) -> bool:
        """在查询结果页点击目标车次的「预订」，进入确认页。"""
        # 在车次行内找「预订」链接。12306 每行预订按钮 id 形如 ticket_<车次序号>，
        # 用车次号文本所在行内的「预订」定位更稳。
        row = self.page.ele(f"@text():{train.train_code}", timeout=10)
        if not row:
            return False
        book = self.page.ele("text:预订", timeout=5)
        if not book:
            return False
        book.click()
        # 等待确认页的提交按钮出现
        return self.page.ele(SEL_SUBMIT_ORDER_BTN, timeout=15) is not None

    def _select_passengers(self) -> None:
        """在确认页按姓名勾选乘客。"""
        for name in self.passengers:
            label = self.page.ele(SEL_PASSENGER_LABEL_TPL.format(name=name), timeout=5)
            if label:
                label.click()
                logger.info("已勾选乘客：%s", name)
            else:
                logger.warning("未找到乘客「%s」，请确认其在12306常用联系人中。", name)

    def _select_seat(self, seat_type: str) -> None:
        """选择席别。"""
        code = SEAT_TYPE_CODE.get(seat_type)
        select = self.page.ele(SEL_SEAT_SELECT, timeout=5)
        if select and code:
            try:
                select.select.by_value(code)
                logger.info("已选择席别：%s", seat_type)
            except Exception as exc:  # noqa: BLE001
                logger.warning("选择席别失败：%s（将使用默认席别）", exc)

    def _confirm_order(self) -> bool:
        """真实提交订单，处理排队确认弹窗。"""
        submit = self.page.ele(SEL_SUBMIT_ORDER_BTN, timeout=10)
        if not submit:
            logger.error("未找到提交订单按钮。")
            return False
        submit.click()

        # 排队确认弹窗
        confirm = self.page.ele(SEL_CONFIRM_QUEUE_BTN, timeout=10)
        if confirm:
            confirm.click()

        # 等待跳转到排队/支付页
        time.sleep(3)
        # 出现「候补」「支付」等字样视为进入下一步
        if self.page.ele("text:支付", timeout=10) or self.page.ele("text:排队", timeout=3):
            logger.info("订单已提交，进入待支付/排队。")
            return True
        logger.warning("提交后未检测到支付/排队页，下单可能失败。")
        return False


if __name__ == "__main__":
    # 下单模块自测入口（干跑）：
    #   python -m src.order
    # 复用登录会话，查询配置行程，命中余票后走到最终提交前停住。
    from .config import load_config
    from .login import LoginManager
    from .query import TicketQuery
    from .utils import setup_logger

    setup_logger()
    cfg = load_config()
    mgr = LoginManager(cfg.account, cfg.browser)
    mgr.start_browser()
    if not mgr.login():
        logger.error("登录失败。")
        raise SystemExit(1)

    q = TicketQuery(cfg.trip, page=mgr.page)
    q.load_station_map()
    om = OrderManager(cfg.passengers, page=mgr.page, dry_run=cfg.order.dry_run)

    for date in cfg.trip.dates:
        hit = q.find_available(date)
        if hit is None:
            logger.info("%s 无可订余票。", date)
            continue
        train, seat = hit
        logger.info("命中：%s %s %s", date, train.train_code, seat)
        om.submit(train, seat)
        break
    input("按回车关闭浏览器……")
    mgr.close()
