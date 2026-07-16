"""阶段 3 登录态与预订入口的回归测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src.config import Account, Browser
from src.login import CHECK_USER_URL, LOGIN_URL, LoginManager
from src.order import (
    CONFIRM_PASSENGER_URL,
    LEFT_TICKET_INIT_URL,
    OrderManager,
    SEL_SUBMIT_ORDER_BTN,
)
from src.query import TrainInfo


class FakeLoginPage:
    def __init__(self, payload, url: str = LOGIN_URL):
        self.payload = payload
        self.url = url
        self.visited: list[str] = []
        self.js_args = ()

    def get(self, url: str) -> None:
        self.visited.append(url)
        self.url = url

    def run_js(self, _script: str, *args):
        self.js_args = args
        return self.payload


class FakeCookiePage:
    def __init__(self):
        self.cdp_calls: list[dict] = []

    def get(self, _url: str) -> None:
        return None

    def run_cdp(self, method: str, **params) -> None:
        if method != "Network.setCookie":
            raise AssertionError(f"unexpected CDP method: {method}")
        self.cdp_calls.append(params)


class FakeWait:
    def doc_loaded(self) -> None:
        return None

    def __call__(self, _seconds: float) -> None:
        return None


class FakeElement:
    def __init__(self, displayed: bool = True, on_click=None):
        self.states = SimpleNamespace(is_displayed=displayed)
        self.on_click = on_click

    def click(self) -> None:
        if self.on_click:
            self.on_click()


class FakeOrderPage:
    def __init__(self):
        self.url = "about:blank"
        self.wait = FakeWait()
        self.calls: list[tuple] = []

    def get(self, url: str) -> None:
        self.calls.append(("get", url))
        self.url = url

    def run_js(self, script: str, *args):
        self.calls.append(("run_js", script, args))
        if "/otn/leftTicket/submitOrderRequest" in script:
            return {
                "httpStatus": 200,
                "status": True,
                "data": "0",
                "messages": [],
            }
        raise AssertionError("unexpected JavaScript")

    def ele(self, selector: str, timeout: float = 0):
        self.calls.append(("ele", selector, timeout))
        if selector == SEL_SUBMIT_ORDER_BTN:
            return FakeElement()
        return None


class FakeLoginManager:
    def __init__(self):
        self.restore_calls = 0

    def restore_session(self) -> bool:
        self.restore_calls += 1
        return True


class FakeSelectionPage:
    def __init__(self):
        self.wait = FakeWait()
        self.calls: list[tuple[str, tuple]] = []

    def run_js(self, script: str, *args):
        self.calls.append((script, args))
        if "passengerTicketReady" in script:
            return {
                "passengerCount": 1,
                "passengerTicketReady": True,
                "oldPassengerReady": True,
                "repeatTokenReady": True,
                "keyReady": True,
                "leftTicketReady": True,
                "trainLocationReady": True,
            }
        if "#normal_passenger_id label" in script:
            return {"matched": list(args), "missing": []}
        if "passenger_name_" in script:
            return ["测试乘车人"]
        if "seatType_" in script:
            return {"count": 1, "unavailable": [], "values": [args[0]]}
        raise AssertionError("unexpected JavaScript")


class FakeSubmitPage(FakeSelectionPage):
    def __init__(self):
        super().__init__()
        self.url = CONFIRM_PASSENGER_URL
        self.submitted = False
        self.confirmed = False

    def ele(self, selector: str, timeout: float = 0):
        if selector == SEL_SUBMIT_ORDER_BTN:
            return FakeElement(on_click=lambda: setattr(self, "submitted", True))
        if selector == "#qr_submit_id":
            return FakeElement(on_click=self._confirm)
        return None

    def _confirm(self) -> None:
        self.confirmed = True
        self.url = "https://kyfw.12306.cn/otn/payOrder/init"

    def run_js(self, script: str, *args):
        if ".dhtmlx_wins_body_outer" in script:
            return []
        return super().run_js(script, *args)


class LoginStateTests(unittest.TestCase):
    def make_manager(self, page: FakeLoginPage) -> LoginManager:
        manager = LoginManager(Account("user", "password"), Browser())
        manager.page = page
        return manager

    def test_check_user_flag_true_is_logged_in(self) -> None:
        page = FakeLoginPage({"status": True, "data": {"flag": True}})

        self.assertTrue(self.make_manager(page).is_logged_in())
        self.assertEqual(page.js_args, (CHECK_USER_URL,))
        self.assertEqual(page.visited, [])

    def test_check_user_flag_false_is_logged_out(self) -> None:
        page = FakeLoginPage({"status": True, "data": {"flag": False}})

        self.assertFalse(self.make_manager(page).is_logged_in())

    def test_check_loads_12306_page_for_external_context(self) -> None:
        page = FakeLoginPage(
            {"status": True, "data": {"flag": True}}, url="about:blank"
        )

        self.assertTrue(self.make_manager(page).is_logged_in())
        self.assertEqual(page.visited, [LOGIN_URL])

    def test_session_cookie_is_injected_without_negative_expiry(self) -> None:
        cookies = [
            {
                "name": "tk",
                "value": "session-token",
                "domain": "kyfw.12306.cn",
                "path": "/otn",
                "expires": -1,
                "session": True,
            },
            {
                "name": "persistent",
                "value": "value",
                "domain": "kyfw.12306.cn",
                "path": "/",
                "expires": 2_000_000_000,
                "session": False,
            },
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            path.write_text(json.dumps(cookies), encoding="utf-8")
            page = FakeCookiePage()
            manager = LoginManager(Account("user", "password"), Browser())
            manager.page = page

            with patch("src.login.COOKIES_PATH", path):
                self.assertTrue(manager._load_cookies())

        by_name = {call["name"]: call for call in page.cdp_calls}
        self.assertNotIn("expires", by_name["tk"])
        self.assertEqual(by_name["persistent"]["expires"], 2_000_000_000)

    def test_restore_session_accepts_successful_uam_handshake(self) -> None:
        page = FakeLoginPage(True)

        self.assertTrue(self.make_manager(page).restore_session())
        self.assertEqual(page.js_args, (CHECK_USER_URL,))


class BookingEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = FakeOrderPage()
        self.login_manager = FakeLoginManager()
        self.manager = OrderManager(
            ["测试乘车人"],
            page=self.page,
            dry_run=True,
            trip=SimpleNamespace(from_station="北京", to_station="桂林"),
            login_manager=self.login_manager,
        )
        self.train = TrainInfo(
            train_code="G309",
            from_code="BJP",
            to_code="GLZ",
            depart_time="08:12",
            arrive_time="18:00",
            duration="09:48",
            secret_str="encoded-secret",
        )

    def test_open_booking_posts_entry_request_and_opens_confirm_page(self) -> None:
        self.assertTrue(self.manager._open_booking(self.train, "2026-07-16"))
        self.assertIn(("get", LEFT_TICKET_INIT_URL), self.page.calls)
        self.assertIn(("get", CONFIRM_PASSENGER_URL), self.page.calls)
        self.assertEqual(self.login_manager.restore_calls, 1)

        submit_calls = [
            call
            for call in self.page.calls
            if call[0] == "run_js"
            and "/otn/leftTicket/submitOrderRequest" in call[1]
        ]
        self.assertEqual(len(submit_calls), 1)
        self.assertEqual(submit_calls[0][2][0:2], ("encoded-secret", "2026-07-16"))
        self.assertEqual(submit_calls[0][2][3:5], ("北京", "桂林"))

    def test_open_booking_rejects_missing_secret(self) -> None:
        self.train.secret_str = ""

        with self.assertRaisesRegex(ValueError, "secret_str"):
            self.manager._open_booking(self.train, "2026-07-16")

    def test_passenger_selection_is_scoped_to_passenger_list(self) -> None:
        page = FakeSelectionPage()
        manager = OrderManager(["测试乘车人"], page=page)

        self.assertTrue(manager._select_passengers())
        self.assertIn("#normal_passenger_id label", page.calls[0][0])

    def test_seat_selection_updates_actual_order_rows(self) -> None:
        page = FakeSelectionPage()
        manager = OrderManager(["测试乘车人"], page=page)

        self.assertTrue(manager._select_seat("二等座"))
        self.assertEqual(page.calls[0][1], ("O",))

    def test_final_payload_validation_checks_all_required_tokens(self) -> None:
        page = FakeSelectionPage()
        manager = OrderManager(["测试乘车人"], page=page)

        self.assertTrue(manager._validate_final_payload())

    def test_confirm_order_requires_visible_confirmation_and_pay_page(self) -> None:
        page = FakeSubmitPage()
        manager = OrderManager(["测试乘车人"], page=page, dry_run=False)

        self.assertTrue(manager._confirm_order())
        self.assertTrue(page.submitted)
        self.assertTrue(page.confirmed)


if __name__ == "__main__":
    unittest.main()
