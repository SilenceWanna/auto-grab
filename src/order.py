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

    def __init__(self, passengers: list[str], page=None, dry_run: bool = True, trip=None):
        self.passengers = passengers
        self.page = page
        self.dry_run = dry_run
        self.trip = trip  # 用于在 HTML 查询页填入出发/到达/日期

    def submit(self, train: TrainInfo, seat_type: str, date: str = "") -> bool:
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
        if not self._open_booking(train, date):
            logger.warning("未能进入预订页（余票可能已被抢空或页面未跳转）。")
            return False

        # 干跑模式下转储确认页 HTML，便于校准选择器
        if self.dry_run:
            self._dump_confirm_page()

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
    def _dump_confirm_page(self) -> None:
        """将当前确认页 HTML 转储到 logs/confirm_page.html，用于校准选择器。"""
        from pathlib import Path
        out = Path(__file__).resolve().parent.parent / "logs" / "confirm_page.html"
        out.parent.mkdir(exist_ok=True)
        try:
            out.write_text(self.page.html, encoding="utf-8")
            url = self.page.url
            reached = "confirmPassenger" in url or "初次" in self.page.html or "乘车人" in self.page.html
            logger.info("已转储确认页 HTML 到 %s（当前URL: %s）", out, url)
            logger.info("确认页判定：%s", "已到订单确认页✓" if reached else "疑似未到确认页✗（URL不含confirmPassenger）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("转储确认页失败：%s", exc)

    def _open_booking(self, train: TrainInfo, date: str = "") -> bool:
        """在 HTML 查询页填入行程、查询，然后点击目标车次的「预订」进入确认页。

        关键：必须走 leftTicket/init 的 HTML 页面（而非查询 JSON 接口页），
        才能看到「预订」按钮并跳转到订单确认页。
        """
        self.page.get(LEFT_TICKET_INIT_URL)

        # 填入出发地/目的地/日期（若提供了 trip/date）
        if self.trip is not None:
            self._fill_query_form(date)

        # 点击查询
        query_btn = self.page.ele(SEL_QUERY_BTN, timeout=10)
        if query_btn:
            query_btn.click()
            self.page.wait.doc_loaded()

        # 在车次表格里定位目标车次所在行，点击该行的「预订」
        book = self._find_book_button(train.train_code)
        if not book:
            logger.warning("未在查询结果中找到车次 %s 的预订按钮。", train.train_code)
            return False
        book.click()

        # 等待确认页的提交按钮出现，作为跳转成功的判据
        return self.page.ele(SEL_SUBMIT_ORDER_BTN, timeout=15) is not None

    def _fill_query_form(self, date: str) -> None:
        """在查询页填入出发地、目的地、日期。"""
        try:
            from_input = self.page.ele(SEL_FROM_INPUT, timeout=5)
            to_input = self.page.ele(SEL_TO_INPUT, timeout=5)
            if from_input:
                from_input.clear()
                from_input.input(self.trip.from_station)
                # 选中下拉联想的第一项
                self.page.wait(0.5)
                from_input.input("\n")
            if to_input:
                to_input.clear()
                to_input.input(self.trip.to_station)
                self.page.wait(0.5)
                to_input.input("\n")
            if date:
                date_input = self.page.ele(SEL_DATE_INPUT, timeout=5)
                if date_input:
                    date_input.clear()
                    date_input.input(date)
        except Exception as exc:  # noqa: BLE001
            logger.warning("填写查询表单出错：%s", exc)

    def _find_book_button(self, train_code: str):
        """在车次结果表中找到指定车次那一行的「预订」按钮。"""
        # 每趟车次在表格中有一行，行内含车次号文本与「预订」链接。
        # 定位车次号元素后，向上找所属行，再在行内找「预订」。
        code_ele = self.page.ele(f"text:{train_code}", timeout=10)
        if not code_ele:
            return None
        # 车次号所在的 <tr> 行
        try:
            row = code_ele.parent("tag:tr")
        except Exception:  # noqa: BLE001
            row = None
        if row:
            book = row.ele("text:预订", timeout=3)
            if book:
                return book
        # 兜底：全页找预订（可能定位到第一趟，仅在单车次场景可靠）
        return self.page.ele("text:预订", timeout=3)

    def _select_passengers(self) -> None:
        """在确认页按姓名勾选乘客。"""
        for name in self.passengers:
            label = self.page.ele(SEL_PASSENGER_LABEL_TPL.format(name=name), timeout=5)
            if label:
                label.click()
                logger.info("已勾选乘客：%s", name)
            else:
                logger.warning("未找到乘客「%s」，请确认其在12306乘车人列表中。", name)

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
    try:
        mgr.start_browser()
        if not mgr.login():
            logger.error("登录失败。")
            raise SystemExit(1)

        q = TicketQuery(cfg.trip, page=mgr.page)
        q.load_station_map()
        om = OrderManager(cfg.passengers, page=mgr.page, dry_run=cfg.order.dry_run, trip=cfg.trip)

        for date in cfg.trip.dates:
            hit = q.find_available(date)
            if hit is None:
                logger.info("%s 无可订余票。", date)
                continue
            train, seat = hit
            logger.info("命中：%s %s %s", date, train.train_code, seat)
            om.submit(train, seat, date=date)
            break
        input("按回车关闭浏览器……")
    finally:
        # 无论正常结束、异常还是关窗，都确保浏览器进程被回收，避免残留堆积
        mgr.close()
        logger.info("浏览器已关闭。")
