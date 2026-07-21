"""选座与提交订单。

下单复用 12306 查询页的官方预订接口：
1. 打开余票查询页并刷新登录会话
2. 将余票接口返回的 secret_str 提交给 submitOrderRequest 接口
3. 在确认页勾选乘客、选择席别
4. 提交订单，处理排队/确认弹窗

安全：dry_run=True 时走到「最终提交」前停住并打印订单信息，不真实占座。
"""

from __future__ import annotations

import logging
import time

from DrissionPage.errors import NoRectError


class SessionExpired(RuntimeError):
    """下单前 UAM 会话恢复失败——需要主循环触发完整重登。"""
from datetime import date as Date

from .query import TrainInfo

logger = logging.getLogger("auto-grab")

# 查询页作为同源请求入口；预订成功后进入确认页。
LEFT_TICKET_INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc"
CONFIRM_PASSENGER_URL = "https://kyfw.12306.cn/otn/confirmPassenger/initDc"
ORDER_CONFIRM_TIMEOUT = 20.0
ORDER_RESULT_TIMEOUT = 180.0

# ---- 选择器集中管理（12306 改版时只需维护此处）----
# 确认页元素
SEL_SUBMIT_ORDER_BTN = "#submitOrder_id"      # 提交订单按钮
SEL_CONFIRM_QUEUE_BTN = "#qr_submit_id"       # 排队确认弹窗的确认按钮

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

    def __init__(
        self,
        passengers: list[str],
        page=None,
        dry_run: bool = True,
        trip=None,
        login_manager=None,
    ):
        self.passengers = passengers
        self.page = page
        self.dry_run = dry_run
        self.trip = trip
        self.login_manager = login_manager

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
        if not self._select_passengers():
            logger.warning("乘车人选择未完成，停止本次下单。")
            return False

        # 3. 选择席别
        if not self._select_seat(seat_type):
            logger.warning("席别选择未完成，停止本次下单。")
            return False

        # 4. 提交
        if self.dry_run:
            if not self._validate_final_payload():
                logger.warning("最终提交参数未准备完整，干跑验收失败。")
                return False
            logger.warning(
                "【干跑模式】已到最终提交前，跳过真实提交。"
                "确认无误后将 config.yaml 的 order.dry_run 改为 false 才会真实抢票。"
            )
            return False

        return self._confirm_order()

    def _post_json(self, path: str, form: dict) -> dict | None:
        """在浏览器上下文里 POST 表单,解析 JSON 返回。同源请求自动带上 cookies。

        JS 内部 try/catch 包住整个 XHR,即使 CORS/网络错误也返回一个失败 dict,
        不让异常冒到 Python 层导致主循环崩溃。
        """
        return self.page.run_js(
            """
            try {
                var path = arguments[0];
                var form = arguments[1];
                var params = new URLSearchParams();
                Object.keys(form).forEach(function(k){ params.set(k, form[k]); });
                var xhr = new XMLHttpRequest();
                xhr.open('POST', path, false);
                xhr.setRequestHeader(
                    'Content-Type',
                    'application/x-www-form-urlencoded; charset=UTF-8'
                );
                xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
                xhr.send(params.toString());
                if (xhr.status < 200 || xhr.status >= 300) {
                    return {httpStatus: xhr.status, status: false, messages: ['HTTP ' + xhr.status]};
                }
                try {
                    var payload = JSON.parse(xhr.responseText);
                    return {
                        httpStatus: xhr.status,
                        status: Boolean(payload.status),
                        data: payload.data,
                        messages: payload.messages || [],
                        validateMessages: payload.validateMessages || {}
                    };
                } catch (parseErr) {
                    return {httpStatus: xhr.status, status: false, messages: ['JSON parse fail']};
                }
            } catch (netErr) {
                // NetworkError / DOMException 等,统一返回失败,不冒到 Python
                return {httpStatus: 0, status: false, messages: [String(netErr && netErr.message || netErr)]};
            }
            """,
            path, form,
        )

    # ------------------------------------------------------------------
    def _dump_confirm_page(self) -> None:
        """将当前确认页 HTML 转储到 logs/confirm_page.html，用于校准选择器。"""
        from pathlib import Path
        out = Path(__file__).resolve().parent.parent / "logs" / "confirm_page.html"
        out.parent.mkdir(exist_ok=True)
        try:
            out.write_text(self.page.html, encoding="utf-8")
            url = self.page.url
            # 严格判定:必须URL带confirmPassenger,页面里"乘车人"字样在其他页也可能有,不可靠
            reached = "confirmPassenger" in url
            logger.info("已转储确认页 HTML 到 %s（当前URL: %s）", out, url)
            logger.info("确认页判定：%s", "已到订单确认页✓" if reached else "疑似未到确认页✗（URL不含confirmPassenger）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("转储确认页失败：%s", exc)

    def _open_booking(self, train: TrainInfo, date: str = "") -> bool:
        """调用官方预订入口进入订单确认页。

        查询接口已经返回预订所需的 ``secret_str``。直接调用页面原生函数
        使用的同一接口，绕开查询表单校验以及偶发假阴性的 checkUser 前置
        检查；服务端接受后再进入 ``confirmPassenger/initDc``。

        懒式认证：先直接尝试 submitOrderRequest，只有当接口返回"需要登录"
        才 restore 一次并重试。避免每次 submit 都强制消耗一次 UAM tk。
        """
        if self.trip is None:
            raise RuntimeError("下单需要行程配置。")
        if not date:
            raise ValueError("下单日期不能为空。")
        if not train.secret_str:
            raise ValueError("余票结果缺少 secret_str，无法预订。")

        self.page.get(LEFT_TICKET_INIT_URL)
        self.page.wait.doc_loaded()
        self.page.wait(2)

        # 先直接尝试提交；若接口回复"需要登录",再 restore 一次并重试。
        response = self._request_submit_order(train, date)
        if self._response_needs_relogin(response):
            logger.info("预订接口提示需要登录，尝试恢复 UAM 会话后重试。")
            if self.login_manager is None or not self.login_manager.restore_session():
                raise SessionExpired("UAM 会话已失效，需要重新登录")
            response = self._request_submit_order(train, date)

        if not isinstance(response, dict) or not response.get("status"):
            messages = response.get("messages") if isinstance(response, dict) else []
            logger.warning(
                "预订入口请求失败（HTTP=%s，消息=%s）。",
                response.get("httpStatus") if isinstance(response, dict) else "未知",
                "；".join(str(message) for message in (messages or [])) or "无",
            )
            return False

        logger.info(
            "预订入口请求成功：%s->%s 日期=%s，准备进入确认页。",
            self.trip.from_station,
            self.trip.to_station,
            date,
        )
        self.page.get(CONFIRM_PASSENGER_URL)
        self.page.wait.doc_loaded()
        found = (
            "confirmPassenger" in str(self.page.url)
            and self.page.ele(SEL_SUBMIT_ORDER_BTN, timeout=10) is not None
        )
        logger.info("已进入订单确认页，提交按钮=%s。", found)
        return found

    @staticmethod
    def _response_needs_relogin(response) -> bool:
        """判断 submitOrderRequest 的响应是否属于"需要重新登录"类失败。"""
        if not isinstance(response, dict):
            return False
        if response.get("status"):
            return False
        http = response.get("httpStatus")
        if http in (401, 403):
            return True
        messages = response.get("messages") or []
        joined = "；".join(str(m) for m in messages)
        return any(kw in joined for kw in ("请重新登录", "未登录", "登录超时", "登录已失效"))

    def _request_submit_order(self, train: TrainInfo, date: str):
        """调用 submitOrderRequest 并返回响应 dict。"""
        from .query import TICKET_TYPE_CODES
        purpose_codes = TICKET_TYPE_CODES.get(
            self.trip.ticket_type if self.trip else "adult", "ADULT",
        )
        return self.page.run_js(
            """
            var params = new URLSearchParams();
            params.set('secretStr', decodeURIComponent(arguments[0]));
            params.set('train_date', arguments[1]);
            params.set('back_train_date', arguments[2]);
            params.set('tour_flag', 'dc');
            params.set('purpose_codes', arguments[5]);
            params.set('query_from_station_name', arguments[3]);
            params.set('query_to_station_name', arguments[4]);
            params.set('undefined', '');
            params.set('bed_level_info', '');
            params.set('seat_discount_info', '');

            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/otn/leftTicket/submitOrderRequest', false);
            xhr.setRequestHeader(
                'Content-Type',
                'application/x-www-form-urlencoded; charset=UTF-8'
            );
            xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
            xhr.send(params.toString());
            if (xhr.status < 200 || xhr.status >= 300) {
                return {httpStatus: xhr.status, status: false, messages: []};
            }
            try {
                var payload = JSON.parse(xhr.responseText);
                return {
                    httpStatus: xhr.status,
                    status: Boolean(payload.status),
                    data: payload.data,
                    messages: payload.messages || [],
                    validateMessages: payload.validateMessages || {}
                };
            } catch (e) {
                return {httpStatus: xhr.status, status: false, messages: []};
            }
            """,
            train.secret_str,
            date,
            Date.today().isoformat(),
            self.trip.from_station,
            self.trip.to_station,
            purpose_codes,
        )

    def _select_passengers(self) -> bool:
        """在确认页的乘车人列表中按姓名勾选，并验证订单行已生成。

        票种由 self.trip.ticket_type 决定:
        - "adult" (默认): 学生身份乘车人弹窗时点击取消,按成人票继续
        - "student": 点击确认,按学生票购买(乘车人必须已在12306认证为学生身份)
        """
        want_student = self.trip is not None and self.trip.ticket_type == "student"
        result = self.page.run_js(
            """
            var wantStudent = arguments[0];
            var wanted = Array.from(arguments).slice(1);
            var labels = Array.from(
                document.querySelectorAll('#normal_passenger_id label')
            );
            var missing = [];
            var matched = [];
            var studentOverrides = [];
            var adultOverrides = [];
            wanted.forEach(function(name) {
                var label = labels.find(function(candidate) {
                    var text = candidate.textContent.trim();
                    return text === name || text.indexOf(name + '(') === 0;
                });
                if (!label) {
                    missing.push(name);
                    return;
                }
                var checkbox = document.getElementById(label.htmlFor);
                if (checkbox && !checkbox.checked) {
                    label.click();
                }
                // 学生身份的乘车人被勾选时,12306 会弹出"是否购买学生票"确认框。
                // adult 模式点取消(按成人票继续),student 模式点确认(按学生票购买)。
                var btnId = wantStudent
                    ? 'dialog_xsertcj_ok'
                    : 'dialog_xsertcj_cancel';
                var btn = document.getElementById(btnId);
                if (btn && btn.offsetParent !== null) {
                    btn.click();
                    if (wantStudent) {
                        studentOverrides.push(name);
                    } else {
                        adultOverrides.push(name);
                    }
                }
                matched.push(name);
            });
            return {
                matched: matched,
                missing: missing,
                studentOverrides: studentOverrides,
                adultOverrides: adultOverrides
            };
            """,
            want_student,
            *self.passengers,
        )
        if not isinstance(result, dict):
            logger.warning("读取乘车人列表失败。")
            return False

        for name in result.get("missing") or []:
            logger.warning("未找到乘车人「%s」，请确认其在12306乘车人列表中。", name)
        if result.get("missing"):
            return False
        if result.get("adultOverrides"):
            logger.info(
                "学生身份乘车人按成人票处理：%s",
                ",".join(result["adultOverrides"]),
            )
        if result.get("studentOverrides"):
            logger.info(
                "已确认购买学生票：%s",
                ",".join(result["studentOverrides"]),
            )

        self.page.wait(0.5)
        selected_names = self.page.run_js(
            """
            return Array.from(
                document.querySelectorAll('input[id^="passenger_name_"]')
            ).filter(function(input) {
                return /^passenger_name_[0-9]+$/.test(input.id) && input.value;
            }).map(function(input) {
                return input.value;
            });
            """
        )
        selected = set(selected_names or [])
        missing_rows = [name for name in self.passengers if name not in selected]
        if missing_rows:
            logger.warning("乘车人已点击但未写入订单行：%s", ",".join(missing_rows))
            return False

        logger.info("已勾选乘车人：%s", ",".join(self.passengers))
        return True

    def _select_seat(self, seat_type: str) -> bool:
        """为所有已选择乘车人的订单行设置席别。"""
        code = SEAT_TYPE_CODE.get(seat_type)
        if not code:
            logger.warning("不支持的席别：%s", seat_type)
            return False

        result = self.page.run_js(
            """
            var code = arguments[0];
            var selects = Array.from(
                document.querySelectorAll('select[id^="seatType_"]')
            ).filter(function(select) {
                return /^seatType_[0-9]+$/.test(select.id);
            });
            var unavailable = [];
            selects.forEach(function(select) {
                var available = Array.from(select.options).some(function(option) {
                    return option.value === code;
                });
                if (!available) {
                    unavailable.push(select.id);
                    return;
                }
                select.value = code;
                select.dispatchEvent(new Event('change', {bubbles: true}));
            });
            return {
                count: selects.length,
                unavailable: unavailable,
                values: selects.map(function(select) { return select.value; })
            };
            """,
            code,
        )
        if not isinstance(result, dict) or not result.get("count"):
            logger.warning("确认页没有可设置的席别下拉框。")
            return False
        if result.get("unavailable") or any(
            value != code for value in (result.get("values") or [])
        ):
            logger.warning("席别「%s」不适用于全部乘车人。", seat_type)
            return False

        logger.info("已为 %d 名乘车人选择席别：%s", result["count"], seat_type)
        return True

    def _validate_final_payload(self) -> bool:
        """验证确认页已具备最终提交参数，不返回或记录乘车人敏感信息。"""
        result = self.page.run_js(
            """
            var info = window.ticketInfoForPassengerForm || {};
            var passengerTicket = typeof window.getpassengerTickets === 'function'
                ? window.getpassengerTickets() : '';
            var oldPassenger = typeof window.getOldPassengers === 'function'
                ? window.getOldPassengers() : '';
            var rows = Array.from(
                document.querySelectorAll('input[id^="passenger_name_"]')
            ).filter(function(input) {
                return /^passenger_name_[0-9]+$/.test(input.id) && input.value;
            });
            return {
                passengerCount: rows.length,
                passengerTicketReady: Boolean(passengerTicket),
                oldPassengerReady: Boolean(oldPassenger),
                repeatTokenReady: Boolean(window.globalRepeatSubmitToken),
                keyReady: Boolean(info.key_check_isChange),
                leftTicketReady: Boolean(info.leftTicketStr),
                trainLocationReady: Boolean(info.train_location)
            };
            """
        )
        required = (
            "passengerTicketReady",
            "oldPassengerReady",
            "repeatTokenReady",
            "keyReady",
            "leftTicketReady",
            "trainLocationReady",
        )
        valid = (
            isinstance(result, dict)
            and result.get("passengerCount") == len(self.passengers)
            and all(result.get(key) for key in required)
        )
        if valid:
            logger.info("最终提交参数校验通过（乘车人数=%d）。", len(self.passengers))
        else:
            logger.warning("最终提交参数校验未通过：%s", result)
        return bool(valid)

    def _visible_order_messages(self) -> list[str]:
        """读取当前可见的订单弹窗文本，用于判错，不读取隐藏模板。"""
        messages = self.page.run_js(
            """
            return Array.from(
                document.querySelectorAll('.dhtmlx_wins_body_outer,.up-box')
            ).filter(function(element) {
                var style = getComputedStyle(element);
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && element.offsetParent !== null;
            }).map(function(element) {
                return element.innerText.trim();
            }).filter(Boolean);
            """
        )
        return [str(message) for message in (messages or [])]

    @staticmethod
    def _is_displayed(element) -> bool:
        if not element:
            return False
        try:
            return bool(element.states.is_displayed)
        except Exception:  # noqa: BLE001
            return False

    def _confirm_order(self) -> bool:
        """真实提交订单，处理排队确认弹窗并严格等待待支付页。"""
        if not self._validate_final_payload():
            logger.error("最终提交参数不完整，拒绝提交订单。")
            return False

        submit = self.page.ele(SEL_SUBMIT_ORDER_BTN, timeout=10)
        if not submit:
            logger.error("未找到提交订单按钮。")
            return False
        submit.click()

        confirm = None
        deadline = time.monotonic() + ORDER_CONFIRM_TIMEOUT
        while time.monotonic() < deadline:
            candidate = self.page.ele(SEL_CONFIRM_QUEUE_BTN, timeout=0.5)
            if self._is_displayed(candidate):
                confirm = candidate
                break
            time.sleep(0.2)
        if confirm is None:
            logger.error(
                "提交订单后未出现可见的排队确认框：%s",
                "；".join(self._visible_order_messages()) or "无页面提示",
            )
            return False

        # 点击确认。若元素在竞态间隙消失(NoRectError),
        # 可能 12306 已自动接受了排队,继续检测 payOrder/init。
        clicked_ok = False
        try:
            confirm.click()
            clicked_ok = True
            logger.info("已确认提交，等待排队结果。")
        except NoRectError:
            logger.warning(
                "排队确认框在 click 前消失(可能被12306自动关闭),尝试用JS点击兜底...",
            )
            # 尝试 JS click(不需要元素可见)
            try:
                # 从 SEL_CONFIRM_QUEUE_BTN 派生 id (去掉开头的 #)
                btn_id = SEL_CONFIRM_QUEUE_BTN.lstrip("#")
                self.page.run_js(
                    "var el = document.getElementById(arguments[0]);"
                    "if (el && typeof el.click === 'function') { el.click(); }",
                    btn_id,
                )
                clicked_ok = True
                logger.info("已用JS点击确认按钮,等待排队结果。")
            except Exception as js_exc:  # noqa: BLE001
                logger.warning("JS 点击也失败:%s。继续检测 URL 是否已跳转...", js_exc)

        failure_words = ("订票失败", "出票失败", "无法提交", "网络忙", "订单已撤销")
        deadline = time.monotonic() + ORDER_RESULT_TIMEOUT
        while time.monotonic() < deadline:
            url = str(self.page.url)
            if "payOrder/init" in url:
                logger.info("订单已进入待支付页。")
                return True
            messages = self._visible_order_messages()
            failure = next(
                (
                    message
                    for message in messages
                    if any(word in message for word in failure_words)
                ),
                None,
            )
            if failure:
                logger.error("订单提交失败：%s", failure)
                return False
            time.sleep(1)

        logger.warning("等待待支付页超时（当前URL=%s）。", self.page.url)
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
        om = OrderManager(
            cfg.passengers,
            page=mgr.page,
            dry_run=cfg.order.dry_run,
            trip=cfg.trip,
            login_manager=mgr,
        )

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
