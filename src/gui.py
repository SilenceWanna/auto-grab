"""简易 tkinter GUI —— 阶段 6 可选前端。

不改后端逻辑，仅提供：
- 用图形界面填写行程/乘客/开关（避免手写 YAML）
- 从/存 config.yaml
- 「开始/停止」按钮启动或中断抢票线程
- 实时日志窗口显示后端 logging 输出

保留 CLI 入口 `python -m src.main`；GUI 用 `python -m src.gui`。
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from datetime import date, timedelta
from tkinter import messagebox, ttk

import yaml
from tkcalendar import Calendar

from .config import DEFAULT_CONFIG_PATH, load_config
from .main import run as run_grab
from .utils import lookup_release_time

logger = logging.getLogger("auto-grab")


class _QueueLogHandler(logging.Handler):
    """把 logging 记录塞进 GUI 主线程可读的 queue。"""

    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except queue.Full:
            pass


class GrabGUI:
    """抢票 GUI 主窗口。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("12306 抢票")
        self.root.geometry("880x900")

        # 后台线程与日志队列
        self._log_queue: "queue.Queue[str]" = queue.Queue(maxsize=1000)
        self._worker: threading.Thread | None = None
        self._stop_flag = threading.Event()
        # 查车次专用的后台线程(与抢票线程互斥,避免同时用一个浏览器)
        self._query_worker: threading.Thread | None = None
        self._query_lock = threading.Lock()

        self._build_form()
        self._build_query_result_panel()
        self._build_log_panel()
        self._attach_log_handler()

        # 尝试加载已存在的 config.yaml
        if DEFAULT_CONFIG_PATH.exists():
            self._load_from_yaml(silent=True)

        # 定时从队列刷新日志（100ms 一次，GUI 主线程安全）
        self.root.after(100, self._drain_log_queue)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_form(self) -> None:
        f = ttk.Frame(self.root, padding=10)
        f.pack(fill="x")

        def row(r: int, label: str, width: int = 40) -> tk.Entry:
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="e", padx=4, pady=3)
            e = tk.Entry(f, width=width)
            e.grid(row=r, column=1, sticky="w", padx=4, pady=3)
            return e

        self.e_user = row(0, "账号:")
        self.e_pwd = row(1, "密码:")
        self.e_pwd.config(show="*")
        self.e_from = row(2, "出发地:", 20)
        self.e_to = row(3, "到达地:", 20)

        # 日期行:输入框 + 弹历按钮 + 查询按钮(v2.2)
        ttk.Label(f, text="日期 (逗号分隔):").grid(row=4, column=0, sticky="e", padx=4, pady=3)
        date_wrap = ttk.Frame(f)
        date_wrap.grid(row=4, column=1, sticky="w", padx=4, pady=3)
        self.e_dates = tk.Entry(date_wrap, width=26)
        self.e_dates.pack(side="left")
        ttk.Button(date_wrap, text="📅 选择", width=8, command=self._pick_date).pack(side="left", padx=(4, 0))
        ttk.Button(date_wrap, text="🔍 查车次", width=10, command=self._query_trains_click).pack(side="left", padx=(4, 0))

        self.e_trains = row(5, "车次 (逗号分隔, 留空=全部):")
        self.e_seats = row(6, "席别偏好 (逗号分隔):")
        self.e_passengers = row(7, "乘车人 (逗号分隔):")

        # 票种下拉(v2)
        ttk.Label(f, text="票种:").grid(row=8, column=0, sticky="e", padx=4, pady=3)
        self.var_ticket = tk.StringVar(value="adult")
        cb_ticket = ttk.Combobox(
            f, textvariable=self.var_ticket, values=("adult", "student"),
            state="readonly", width=12,
        )
        cb_ticket.grid(row=8, column=1, sticky="w", padx=4, pady=3)
        ttk.Label(f, text="(adult=成人票  student=学生票)").grid(
            row=8, column=1, sticky="w", padx=(140, 4),
        )

        # 放票整点(v2):可手写,留空则受 auto 影响
        self.e_rush = row(9, "放票时刻 (HH:MM,逗号分隔,留空则按下方自动):")

        # 自动查放票时刻开关(v2) + 探测结果实时提示
        self.var_auto_sched = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            f, text="自动根据出发地查放票时刻(内置88站)",
            variable=self.var_auto_sched,
        ).grid(row=10, column=1, sticky="w", padx=4, pady=3)
        self.lbl_release = ttk.Label(f, text="", foreground="#2a6")
        self.lbl_release.grid(row=11, column=1, sticky="w", padx=4)
        # 出发地变化时实时刷新提示
        self.e_from.bind("<KeyRelease>", lambda _e: self._refresh_release_hint())
        self.e_from.bind("<FocusOut>", lambda _e: self._refresh_release_hint())

        # dry_run 开关
        self.var_dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            f, text="干跑模式（不真实占座，测试用）",
            variable=self.var_dry,
        ).grid(row=12, column=1, sticky="w", padx=4, pady=6)

        # 浏览器 attach_port(v2.3.2)
        ttk.Label(f, text="attach 端口:").grid(row=13, column=0, sticky="e", padx=4, pady=3)
        attach_wrap = ttk.Frame(f)
        attach_wrap.grid(row=13, column=1, sticky="w", padx=4, pady=3)
        self.e_attach_port = tk.Entry(attach_wrap, width=8)
        self.e_attach_port.pack(side="left")
        ttk.Label(
            attach_wrap,
            text="(可选,填0=自启浏览器。填9333=先手动启浏览器 --remote-debugging-port=9333 后连接)",
            foreground="#666",
        ).pack(side="left", padx=(4, 0))

        # 按钮行
        btns = ttk.Frame(self.root, padding=(10, 0))
        btns.pack(fill="x")
        ttk.Button(btns, text="从 YAML 加载", command=self._load_from_yaml).pack(side="left", padx=4)
        ttk.Button(btns, text="保存到 YAML", command=self._save_to_yaml).pack(side="left", padx=4)
        self.btn_start = ttk.Button(btns, text="▶ 开始抢票", command=self._start_grab)
        self.btn_start.pack(side="left", padx=12)
        self.btn_stop = ttk.Button(btns, text="■ 停止", command=self._stop_grab, state="disabled")
        self.btn_stop.pack(side="left", padx=4)

    def _refresh_release_hint(self) -> None:
        """出发地变化时更新自动放票时刻的探测提示。"""
        station = self.e_from.get().strip()
        if not station:
            self.lbl_release.config(text="", foreground="#2a6")
            return
        release = lookup_release_time(station)
        if release:
            self.lbl_release.config(
                text=f"✓ 已识别 {station} 的放票时刻:{release}",
                foreground="#2a6",
            )
        else:
            self.lbl_release.config(
                text=f"⚠ 内置表未收录 {station},请在上方「放票时刻」手填,例如 13:00",
                foreground="#c60",
            )

    # ------------------------------------------------------------------
    # 日期选择 & 车次查询(v2.2)
    # ------------------------------------------------------------------
    def _pick_date(self) -> None:
        """弹出日历,选中日期后追加到日期字段(YYYY-MM-DD,逗号分隔)。"""
        top = tk.Toplevel(self.root)
        top.title("选择乘车日期")
        top.transient(self.root)
        today = date.today()
        # 默认锚点:若日期字段已有值,落在最后一个;否则落在 today+3
        # (避免用户想选未来日期时,日历默认停在今天,还要手动翻月)
        existing = self._split(self.e_dates.get())
        anchor = today + timedelta(days=3)
        if existing:
            from datetime import datetime as _dt
            for s in reversed(existing):
                try:
                    d = _dt.strptime(s, "%Y-%m-%d").date()
                    if today <= d <= today + timedelta(days=30):
                        anchor = d
                        break
                except ValueError:
                    continue
        cal = Calendar(
            top,
            selectmode="day",
            year=anchor.year, month=anchor.month, day=anchor.day,
            mindate=today,
            maxdate=today + timedelta(days=30),
            date_pattern="yyyy-mm-dd",
        )
        cal.pack(padx=10, pady=10)
        # 显式选中该锚点日期(避免只是"月份定位"而未选中)
        cal.selection_set(anchor)

        def _confirm():
            picked = cal.get_date()   # YYYY-MM-DD
            existing = self._split(self.e_dates.get())
            if picked not in existing:
                existing.append(picked)
                self.e_dates.delete(0, "end")
                self.e_dates.insert(0, ", ".join(existing))
            top.destroy()

        def _replace():
            """清空原有日期,只保留当前选中的日期(常用于重新选择)。"""
            picked = cal.get_date()
            self.e_dates.delete(0, "end")
            self.e_dates.insert(0, picked)
            top.destroy()

        btn_row = ttk.Frame(top)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_row, text="替换", command=_replace).pack(side="right")
        ttk.Button(btn_row, text="追加", command=_confirm).pack(side="right", padx=(0, 6))
        ttk.Button(btn_row, text="取消", command=top.destroy).pack(side="right", padx=(0, 6))
        top.grab_set()

    def _build_query_result_panel(self) -> None:
        """车次查询结果面板(默认隐藏,查询后显示)。"""
        self.qr_frame = ttk.LabelFrame(self.root, text="车次查询结果", padding=6)
        # 先不 pack,等有结果再 pack
        columns = ("train_code", "depart", "arrive", "duration", "seats")
        self.qr_tree = ttk.Treeview(
            self.qr_frame, columns=columns, show="headings", height=8,
        )
        headings = [("train_code", "车次", 80),
                    ("depart", "出发", 80),
                    ("arrive", "到达", 80),
                    ("duration", "历时", 80),
                    ("seats", "余票摘要(有票席别)", 340)]
        for col, text, width in headings:
            self.qr_tree.heading(col, text=text)
            self.qr_tree.column(col, width=width, anchor="w")
        self.qr_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(self.qr_frame, orient="vertical", command=self.qr_tree.yview)
        sb.pack(side="right", fill="y")
        self.qr_tree.config(yscrollcommand=sb.set)
        # 双击一行 -> 把该车次加入"车次"字段
        self.qr_tree.bind("<Double-1>", self._on_tree_double_click)

    def _on_tree_double_click(self, _event) -> None:
        sel = self.qr_tree.selection()
        if not sel:
            return
        code = self.qr_tree.item(sel[0], "values")[0]
        existing = self._split(self.e_trains.get())
        if code and code not in existing:
            existing.append(code)
            self.e_trains.delete(0, "end")
            self.e_trains.insert(0, ", ".join(existing))
            self._append_log(f"[GUI] 已把车次 {code} 加入抢票目标。")

    def _query_trains_click(self) -> None:
        """点"查车次"按钮:在后台线程启动浏览器+登录+查询,结果填入表格。"""
        if not self._query_lock.acquire(blocking=False):
            messagebox.showinfo("提示", "上一次查询还在进行中,请稍候。")
            return

        # 抢票线程正在跑,不要冲突
        if self._worker and self._worker.is_alive():
            self._query_lock.release()
            messagebox.showwarning("提示", "抢票已在运行,查询暂不可用。")
            return

        dates = self._split(self.e_dates.get())
        from_station = self.e_from.get().strip()
        to_station = self.e_to.get().strip()
        if not dates or not from_station or not to_station:
            self._query_lock.release()
            messagebox.showwarning("提示", "请先填写出发地、到达地、至少一个日期。")
            return
        # 只查第一个日期,避免重复启浏览器
        target_date = dates[0]

        # 显示结果面板并清空
        if not self.qr_frame.winfo_ismapped():
            self.qr_frame.pack(fill="both", expand=False, padx=10, pady=(0, 6), before=self.log_wrap)
        self.qr_tree.delete(*self.qr_tree.get_children())
        self._append_log(f"[GUI] 正在查询 {target_date} {from_station}->{to_station} 车次...")

        def _worker():
            try:
                trains = self._run_query(from_station, to_station, target_date)
                self.root.after(0, lambda: self._render_trains(trains, target_date))
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda e=exc: self._append_log(f"[GUI] 查询失败:{e}"))
            finally:
                self._query_lock.release()

        self._query_worker = threading.Thread(target=_worker, daemon=True)
        self._query_worker.start()

    def _run_query(self, from_station: str, to_station: str, target_date: str) -> list:
        """在后台线程里执行:启动浏览器→登录→查询→关闭浏览器。返回 TrainInfo 列表。"""
        try:
            cfg = load_config()
        except Exception:  # noqa: BLE001
            self.root.after(0, lambda: messagebox.showerror(
                "配置有误", "请先保存 config.yaml 再查询。",
            ))
            return []

        # 用一个临时 Trip 覆盖行程,不影响用户配置
        from dataclasses import replace
        temp_trip = replace(
            cfg.trip,
            from_station=from_station,
            to_station=to_station,
            dates=[target_date],
            train_codes=[],   # 不过滤,拿全部
        )

        from .login import LoginManager
        from .query import TicketQuery

        mgr = LoginManager(cfg.account, cfg.browser)
        try:
            mgr.start_browser()
            if not mgr.login():
                self.root.after(0, lambda: self._append_log("[GUI] 登录失败,无法查询。"))
                return []
            q = TicketQuery(temp_trip, page=mgr.page)
            q.load_station_map()
            return q.query(target_date)
        finally:
            mgr.close()

    def _render_trains(self, trains: list, target_date: str) -> None:
        """把车次列表渲染到 Treeview。"""
        from .query import _NO_TICKET
        if not trains:
            self._append_log(f"[GUI] {target_date} 无匹配车次或查询失败。")
            return
        for t in trains:
            seats = [f"{k}:{v}" for k, v in t.seats.items() if v and v not in _NO_TICKET]
            self.qr_tree.insert("", "end", values=(
                t.train_code, t.depart_time, t.arrive_time, t.duration,
                "  ".join(seats) if seats else "(无余票)",
            ))
        self._append_log(
            f"[GUI] {target_date} 查询完成,共 {len(trains)} 趟车次。"
            f"双击某行可加入抢票目标车次。",
        )

    # ------------------------------------------------------------------
    def _build_log_panel(self) -> None:
        self.log_wrap = ttk.LabelFrame(self.root, text="运行日志", padding=6)
        self.log_wrap.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_text = tk.Text(self.log_wrap, wrap="none", state="disabled", height=20)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(self.log_wrap, orient="vertical", command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=sb.set)

    def _attach_log_handler(self) -> None:
        h = _QueueLogHandler(self._log_queue)
        logger.addHandler(h)

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------
    def _load_from_yaml(self, silent: bool = False) -> None:
        try:
            with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except FileNotFoundError:
            if not silent:
                messagebox.showwarning("提示", f"未找到 {DEFAULT_CONFIG_PATH}")
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("加载失败", str(exc))
            return

        acc = raw.get("account", {})
        trip = raw.get("trip", {})
        order = raw.get("order", {})
        schedule = raw.get("schedule", {})
        self._set(self.e_user, acc.get("username", ""))
        self._set(self.e_pwd, acc.get("password", ""))
        self._set(self.e_from, trip.get("from_station", ""))
        self._set(self.e_to, trip.get("to_station", ""))
        self._set(self.e_dates, ", ".join(trip.get("dates", []) or []))
        self._set(self.e_trains, ", ".join(trip.get("train_codes", []) or []))
        self._set(self.e_seats, ", ".join(trip.get("seat_types", []) or []))
        self._set(self.e_passengers, ", ".join(raw.get("passengers", []) or []))
        self.var_dry.set(bool(order.get("dry_run", True)))
        # v2 新字段
        self.var_ticket.set(trip.get("ticket_type", "adult"))
        self._set(self.e_rush, ", ".join(schedule.get("rush_at", []) or []))
        self.var_auto_sched.set(bool(schedule.get("auto", False)))
        # attach_port(v2.3.2)
        browser_cfg = raw.get("browser", {})
        attach = browser_cfg.get("attach_port", 0) or 0
        self._set(self.e_attach_port, str(attach) if attach else "")
        self._refresh_release_hint()  # 加载后立即刷新提示
        if not silent:
            self._append_log(f"[GUI] 已从 {DEFAULT_CONFIG_PATH.name} 加载配置。")

    def _save_to_yaml(self) -> None:
        # 读现有 YAML，合并 GUI 里能编辑的部分（保留 notify/browser/schedule 等 GUI 未涵盖的段）
        raw: dict = {}
        if DEFAULT_CONFIG_PATH.exists():
            try:
                with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            except Exception:  # noqa: BLE001
                raw = {}

        raw.setdefault("account", {})
        raw["account"]["username"] = self.e_user.get().strip()
        raw["account"]["password"] = self.e_pwd.get()
        raw["account"].setdefault("manual_captcha", True)

        raw.setdefault("trip", {})
        raw["trip"]["from_station"] = self.e_from.get().strip()
        raw["trip"]["to_station"] = self.e_to.get().strip()
        raw["trip"]["dates"] = self._split(self.e_dates.get())
        raw["trip"]["train_codes"] = self._split(self.e_trains.get())
        raw["trip"]["seat_types"] = self._split(self.e_seats.get())
        raw["trip"].setdefault("allow_candidate", False)  # 字段保留,GUI 暂不暴露开关
        raw["trip"]["ticket_type"] = self.var_ticket.get()

        raw["passengers"] = self._split(self.e_passengers.get())

        raw.setdefault("order", {})
        raw["order"]["dry_run"] = self.var_dry.get()

        # v2:schedule 部分,保留 GUI 未涵盖的字段(prep/rush_duration/interval 等)
        raw.setdefault("schedule", {})
        raw["schedule"]["rush_at"] = self._split(self.e_rush.get())
        raw["schedule"]["auto"] = self.var_auto_sched.get()

        # attach_port(v2.3.2)
        raw.setdefault("browser", {})
        attach_txt = self.e_attach_port.get().strip()
        try:
            raw["browser"]["attach_port"] = int(attach_txt) if attach_txt else 0
        except ValueError:
            raw["browser"]["attach_port"] = 0

        try:
            with DEFAULT_CONFIG_PATH.open("w", encoding="utf-8") as f:
                yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
            self._append_log(f"[GUI] 已保存到 {DEFAULT_CONFIG_PATH.name}。")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))

    # ------------------------------------------------------------------
    # 抢票线程控制
    # ------------------------------------------------------------------
    def _start_grab(self) -> None:
        # 先保存,后端只读 YAML
        self._save_to_yaml()
        # 校验一下配置能否加载
        try:
            load_config()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("配置有误", str(exc))
            return
        if self._worker and self._worker.is_alive():
            messagebox.showwarning("提示", "抢票已在运行。")
            return

        # 再次确认真实下单
        if not self.var_dry.get():
            ok = messagebox.askyesno(
                "确认真实下单",
                "⚠️ 已关闭干跑，脚本会真实提交订单并真实占座。\n\n确定要开始吗？",
            )
            if not ok:
                return

        self._stop_flag.clear()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")

        stop_event = self._stop_flag  # 传给后端主循环

        def _worker():
            try:
                code = run_grab(stop_event=stop_event)
                self._append_log(f"[GUI] 抢票流程结束，退出码 {code}。")
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"[GUI] 抢票线程异常：{exc}")
            finally:
                # 用 after 回到主线程改按钮
                self.root.after(0, self._reset_buttons)

        self._worker = threading.Thread(target=_worker, daemon=True)
        self._worker.start()
        self._append_log("[GUI] 已启动抢票线程。")

    def _stop_grab(self) -> None:
        """请求停止抢票循环。后端会在下一次轮询边界或睡眠段(<=0.2s)退出并回收浏览器。"""
        if not self._worker or not self._worker.is_alive():
            return
        self._stop_flag.set()
        self.btn_stop.config(state="disabled")
        self._append_log("[GUI] 已发送停止信号,等待浏览器关闭...")

    def _reset_buttons(self) -> None:
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")

    # ------------------------------------------------------------------
    # 日志刷新
    # ------------------------------------------------------------------
    def _drain_log_queue(self) -> None:
        try:
            while True:
                line = self._log_queue.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def _append_log(self, line: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _set(entry: tk.Entry, value: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, value)

    @staticmethod
    def _split(s: str) -> list[str]:
        return [x.strip() for x in s.split(",") if x.strip()]


def main() -> None:
    root = tk.Tk()
    GrabGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
