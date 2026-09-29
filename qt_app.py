"""原生介面（Qt / PySide6）。

跟網頁版用同一套後端路由（server.call），所以功能邏輯只有一份；這裡只負責畫面。
所有後端呼叫都丟到背景執行緒，畫面不會卡住。"""
import base64
import datetime
import json
import math
import re
import sys
import time
import webbrowser

from PySide6.QtCore import (QEasingCurve, QObject, QPropertyAnimation, QRectF, QRunnable, Qt, QThreadPool, QTimer,
                            Signal, Slot)
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPalette
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QTextBrowser, QToolButton,
                               QVBoxLayout, QWidget)

import engine
import server
import motion
import theme

# ---------------------------------------------------------------- 白話化（跟網頁版同一套說法）
STATUS = {"success": ("成功", "ok"), "failed": ("失敗", "bad"), "running": ("執行中", "run"),
          "interrupted": ("中斷", "warn"), "cancelled": ("已停止", "warn")}
TRIGGER = {"schedule": "排程", "cli": "命令列"}
SKILL_VERB = {"read_rss": "讀取新聞來源", "fetch_url": "打開網頁", "http_check": "檢查網站是否正常",
              "system_status": "檢查這台電腦的狀態", "tail_file": "讀取檔案", "use_skill": "載入知識",
              "read_skill_file": "翻閱章節", "save_report": "存成報告", "notify": "跳通知給你",
              "create_workflow": "建立新的工作流", "web_search": "搜尋網路", "github_repo": "查看 GitHub repo"}


def dur(sec):
    if sec is None:
        return ""
    sec = round(sec)
    if sec < 60:
        return f"{sec} 秒"
    m, s = divmod(sec, 60)
    return f"{m} 分{f' {s} 秒' if s else ''}" if m < 60 else f"{m // 60} 小時 {m % 60} 分"


def when(ts):
    if not ts:
        return ""
    d, now = datetime.datetime.fromtimestamp(ts), datetime.datetime.now()
    diff = (now - d).total_seconds()
    if 0 <= diff < 60:
        return "剛剛"
    if 0 <= diff < 3600:
        return f"{int(diff // 60)} 分鐘前"
    days = (d.date() - now.date()).days
    hm = d.strftime("%H:%M")
    return {0: "今天 ", -1: "昨天 ", 1: "明天 "}.get(days, f"{d.month}/{d.day} ") + hm


def sched_text(s):
    if not s or (not s.get("daily") and not s.get("every_minutes")):
        return "手動執行"
    if s.get("daily"):
        return f"每天 {s['daily']}"
    m = s["every_minutes"]
    return f"每 {m // 60} 小時" if m % 60 == 0 else f"每 {m} 分鐘"


def is_manual(s):
    return not s or (not s.get("daily") and not s.get("every_minutes"))


def parse_args(a):
    try:
        return json.loads(a) if isinstance(a, str) else (a or {})
    except ValueError:
        return {}


def describe(name, args):
    a = parse_args(args)
    verb = SKILL_VERB.get(name, f"使用 {name}")
    obj = {"read_rss": a.get("url"), "fetch_url": a.get("url"), "http_check": a.get("url"),
           "tail_file": str(a.get("path", "")).rsplit("/", 1)[-1], "use_skill": a.get("skill"),
           "read_skill_file": f"{a.get('skill', '')} / {str(a.get('path', '')).rsplit('/', 1)[-1]}",
           "save_report": f"「{a.get('title', '')}」" if a.get("title") else "", "notify": f"「{a.get('message', '')}」" if a.get("message") else "",
           "web_search": f"「{a.get('query', '')}」" if a.get("query") else "", "github_repo": a.get("repo"),
           "create_workflow": a.get("name")}.get(name) or ""
    obj = re.sub(r"^https?://", "", str(obj))
    return verb, obj


def result_summary(name, out):
    out = out or ""
    if out.startswith("[skill 錯誤]"):
        return True, out.replace("[skill 錯誤]", "出錯：")[:160]
    if re.match(r"^(找不到|抓不到|這個路徑不|沒有這個|只接受)", out):
        return True, out.splitlines()[0][:120]
    if name in ("web_search", "read_rss"):
        n = len(re.findall(r"^\d+\. ", out, re.M))
        return (n == 0), (f"找到 {n} 筆" if n else "沒有結果")
    if name == "http_check":
        return (not out.startswith("OK")), out.splitlines()[0][:120]
    if name == "save_report":
        return False, "存在 " + out.rsplit("/", 1)[-1]
    if name == "notify":
        return False, "已送出"
    return False, f"{len(out):,} 字"


def human_error(e):
    e = e or ""
    if e.startswith("Traceback"):
        e = e.strip().splitlines()[-1]
    e = re.sub(r"^\w*Error: ", "", e)
    if re.search(r"API.?KEY", e, re.I) and "設定" not in e:
        return "還沒設定 API key。"
    if "timed out" in e.lower():
        return "模型太久沒有回應（逾時）。通常是讀進去的內容太多。"
    if "max_steps" in e:
        return "步驟用完了還沒做完，agent 可能在繞圈子。"
    return e


# ---------------------------------------------------------------- 背景呼叫後端
class _Sig(QObject):
    done = Signal(object, object)


class _Job(QRunnable):
    def __init__(self, fn, cb):
        super().__init__()
        self.fn, self.sig = fn, _Sig()
        self.sig.done.connect(cb)

    def run(self):
        try:
            r = self.fn()
        except Exception as e:           # 後端出錯也要回到畫面，不能讓介面卡住
            r = {"status": 500, "json": {"error": str(e)}}
        self.sig.done.emit(r, None)


class Backend(QObject):
    """呼叫 server.call（跟網頁版同一套路由），在背景執行緒跑，結果用 signal 回到畫面執行緒。"""

    def __init__(self):
        super().__init__()
        self.pool = QThreadPool.globalInstance()
        self._keep = set()

    def call(self, method, path, body=None, cb=None):
        job = _Job(lambda: server.call(method, path, body), lambda r, _: self._finish(job, r, cb))
        self._keep.add(job)
        job.setAutoDelete(False)
        self.pool.start(job)

    def get(self, path, cb):
        self.call("GET", path, None, lambda r: cb(r.get("json")))

    def post(self, path, body, cb=None):
        def wrap(r):
            j = r.get("json") or {}
            if cb:
                cb({**j, "ok": r.get("status", 500) < 400 and not j.get("error")} if isinstance(j, dict) else j)
        self.call("POST", path, body or {}, wrap)

    def _finish(self, job, r, cb):
        self._keep.discard(job)
        if cb:
            cb(r)


# ---------------------------------------------------------------- 動態（Dynamization）
# 原生元件本身就快；動態只用在「東西出現 / 換掉」的地方，而且用 transform 類的便宜屬性。
spring_curve = motion.bake


def slide_in(widget, dy=8):
    """進場（enterEl）：淡入 ＋ 從 dy 用彈簧回到原位。"""
    motion.enter(widget, dy=dy)


# ---------------------------------------------------------------- 色彩
ACCENT = "#CCCCFF"


def C(role):
    return theme.T[role]


# ---------------------------------------------------------------- 小元件
class TimeBar(QWidget):
    """橫向時間條：每段寬度 = 花的時間；夠寬才寫字（跟網頁版同樣的規則）。"""

    def __init__(self):
        super().__init__()
        self.segs = []
        self.setFixedHeight(24)
        self.setToolTip("")

    def set_segments(self, segs):
        self.segs = segs
        total = sum(s[1] for s in segs) or 1
        self.setToolTip("\n".join(f"{s[2]}：{dur(s[1]) or '不到 1 秒'}" for s in segs))
        self._total = total
        self.upd()

    def paintEvent(self, _):
        if not self.segs:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, x = self.width(), self.height(), 0.0
        gap = 2
        avail = w - gap * (len(self.segs) - 1)
        for kind, sec, label in self.segs:
            sw = max(3.0, avail * sec / self._total)
            color = QColor(C("model") if kind == "model" else C("tool") if kind == "tool" else C("bad"))
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(x, 0, sw, h), 4, 4)
            if sw > 90:
                p.setPen(QColor("#1e1e3c") if kind == "model" else QColor("white"))
                p.drawText(QRectF(x + 6, 0, sw - 8, h), Qt.AlignVCenter | Qt.AlignLeft,
                           f"{label} {dur(sec) or ''}")
            x += sw + gap
        p.end()


def label(text="", role=None, size=None, bold=False, wrap=True):
    l = QLabel(text)
    l.setWordWrap(wrap)
    l.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
    l.setOpenExternalLinks(True)
    css = []
    if role:
        css.append(f"color:{C(role)}")
    if size:
        css.append(f"font-size:{size}px")
    if bold:
        css.append("font-weight:600")
    if css:
        l.setStyleSheet(";".join(css))
    return l


def hline():
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"color:{C('line')}")
    return f


def esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_html(md):
    """Qt 內建的 Markdown（支援表格、清單、程式碼區塊）轉成 HTML 片段。"""
    from PySide6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setMarkdown(md or "")
    h = doc.toHtml()
    m = re.search(r"<body[^>]*>(.*)</body>", h, re.S)
    return m.group(1) if m else h


def to_rounds(steps):
    rounds = []
    for s in steps:
        if s["kind"] == "llm":
            rounds.append({"llm": s, "acts": []})
        else:
            if not rounds:
                rounds.append({"llm": None, "acts": []})
            rounds[-1]["acts"].append(s)
    return rounds


def seg_list(steps, live=None):
    segs = []
    for s in steps:
        if s["kind"] == "llm":
            o = parse_args(s["output"])
            segs.append(("model", max((s["ms"] or 0) / 1000, 0.05), "讀資料、想下一步" if o.get("tool_calls") else "寫結果"))
        elif s["kind"] == "tool":
            err, _ = result_summary(s["name"], s["output"])
            segs.append(("error" if err else "tool", max((s["ms"] or 0) / 1000, 0.05), describe(s["name"], s["input"])[0]))
    if live:
        segs.append(live)
    return segs


def act_html(s):
    if s["kind"] == "note":
        return f"<div style='margin:3px 0'><span style='color:{C('run')}'>ℹ</span> {esc(s['output'])}</div>"
    if s["kind"] == "tool":
        verb, obj = describe(s["name"], s["input"])
        err, txt = result_summary(s["name"], s["output"])
        mark = f"<span style='color:{C('bad' if err else 'ok')};font-weight:700'>{'✕' if err else '✓'}</span>"
        ms = s["ms"] or 0
        return (f"<div style='margin:3px 0'>{mark} {esc(verb)}{'：' + esc(obj) if obj else ''} "
                f"<span style='color:{C('muted')}'>— {esc(txt)}　{dur(ms / 1000) if ms >= 1000 else str(ms) + ' ms'}</span></div>")
    if s["name"] == "cancelled":
        return f"<div style='margin:3px 0'><span style='color:{C('warn')}'>■</span> 你按了停止</div>"
    fb = re.search(r"→ 改用(\S*) (\S+)", s["output"] or "")
    head = f"{esc(s['name'])} 出錯，改用{fb.group(1)} {esc(fb.group(2))}" if fb else "沒有完成"
    return (f"<div style='margin:3px 0'><span style='color:{C('bad')};font-weight:700'>✕</span> {head} "
            f"<span style='color:{C('muted')}'>— {esc(human_error((s['output'] or '').splitlines()[0] if fb else s['output']))}</span></div>")


def process_html(steps):
    out = []
    for i, rd in enumerate(to_rounds(steps), 1):
        o = parse_args(rd["llm"]["output"]) if rd["llm"] else {}
        final = rd["llm"] and not o.get("tool_calls")
        verb = "開始前" if not rd["llm"] else "寫出結果" if final else (
            f"決定做 {len(o['tool_calls'])} 件事" if len(o.get("tool_calls") or []) > 1 else "決定下一步")
        said = re.sub(r"<think>.*?</think>", "", o.get("content") or "", flags=re.S).strip()
        spent = f"<span style='color:{C('muted')};font-weight:400'>　模型花了 {dur(rd['llm']['ms'] / 1000) or '不到 1 秒'}</span>" if rd["llm"] else ""
        raw = (o.get("raw") or {})
        st = raw.get("stats") or {}
        rawline = (f"<div style='color:{C('muted')};font-size:12px'>模型原始輸出：{esc(st.get('stopReason', ''))}"
                   f"{'，' + str(round(st['tokensPerSecond'])) + ' tok/s' if st.get('tokensPerSecond') else ''}</div>") if raw else ""
        out.append(f"<div style='margin:10px 0;padding:8px 12px;border:1px solid {C('line')}'>"
                   f"<b>{i}. {verb}</b>{spent}"
                   + (f"<div style='color:{C('muted')}'>{esc(said[:240])}{'…' if len(said) > 240 else ''}</div>" if said and not final else "")
                   + "".join(act_html(a) for a in rd["acts"]) + rawline + "</div>")
    return "".join(out) or f"<div style='color:{C('muted')}'>沒有中間步驟</div>"


class WorkflowTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.b = win, win.backend
        self.sel_wf, self.sel_run, self.shown_detail = None, None, None
        self.runs, self.detail = [], None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0)

        # 工具列
        bar = QFrame()
        bar.setObjectName("panel")
        h = QHBoxLayout(bar)
        self.combo = QComboBox()
        self.combo.setObjectName("picker")
        self.combo.setMinimumWidth(220)
        self.combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.combo.activated.connect(self.on_pick)
        self.info = label("", "muted")
        self.info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.enable = QCheckBox("排程")
        self.enable.toggled.connect(self.on_enable)
        self.inp = QLineEdit()
        self.inp.setPlaceholderText("要處理什麼？")
        self.inp.setMinimumWidth(200)
        self.inp.returnPressed.connect(self.on_run)
        self.run_btn = QPushButton("立即執行")
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self.on_run)
        self.more = QToolButton()
        self.more.setText("⋯")
        self.more.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(self)
        self.act_edit = m.addAction("編輯設定", lambda: self.win.open_editor(self.win.wf.get(self.sel_wf)))
        m.addAction("＋ 新增工作流", lambda: self.win.open_editor(None))
        m.addSeparator()
        self.act_del = m.addAction("刪除這條工作流", self.on_delete)
        self.more.setMenu(m)
        for w in (self.combo, self.info, self.enable, self.inp, self.run_btn, self.more):
            h.addWidget(w)
        v.addWidget(bar)

        # 現在
        self.live = QFrame()
        self.live.setObjectName("live")
        lv = QVBoxLayout(self.live)
        top = QHBoxLayout()
        self.live_title = label("", size=16, bold=True)
        self.live_meta = label("", "muted", wrap=False)
        self.live_stop = QPushButton("停止")
        self.live_stop.clicked.connect(lambda: self.b.post(f"/api/workflows/{self.live_wf}/stop", {}))
        top.addWidget(self.live_title)
        top.addStretch()
        top.addWidget(self.live_meta)
        top.addWidget(self.live_stop)
        lv.addLayout(top)
        self.live_what = label("", size=17, bold=True)
        wrow = QHBoxLayout()
        wrow.setSpacing(8)
        wrow.addWidget(motion.PulseDot())
        wrow.addWidget(self.live_what, 1)
        lv.addLayout(wrow)
        prow = QHBoxLayout()
        self.live_prog_text = label("", "muted")
        self.live_prog = QProgressBar()
        self.live_prog.setTextVisible(False)
        self.live_prog.setMaximumWidth(260)
        self.live_prog.setFixedHeight(6)
        prow.addWidget(self.live_prog_text)
        prow.addWidget(self.live_prog)
        prow.addStretch()
        lv.addLayout(prow)
        self.live_bar = TimeBar()
        lv.addWidget(self.live_bar)
        self.live_note = label("", "muted")
        lv.addWidget(self.live_note)
        self.live_recent = label("")
        lv.addWidget(self.live_recent)
        self.live_stream = QPlainTextEdit()
        self.live_stream.setReadOnly(True)
        self.live_stream.setMaximumHeight(200)
        lv.addWidget(self.live_stream)
        self.live.hide()
        v.addWidget(self.live)
        self.live_wf, self.live_prev = None, {}

        # 紀錄 + 內容
        split = QSplitter()
        left = QWidget()
        lv2 = QVBoxLayout(left)
        lv2.setContentsMargins(0, 0, 0, 0)
        self.runs_title = label("最近的執行", "muted", bold=True)
        lv2.addWidget(self.runs_title)
        self.run_list = motion.MotionList()
        self.run_list.setItemDelegate(theme.RunDelegate(self.run_list))
        self.run_list.setMouseTracking(True)
        self.run_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.run_list.itemSelectionChanged.connect(self.on_select_run)
        lv2.addWidget(self.run_list)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rh = QHBoxLayout()
        rh.addWidget(label("內容", "muted", bold=True))
        rh.addStretch()
        self.export_btn = QToolButton()
        self.export_btn.setText("匯出")
        self.export_btn.setPopupMode(QToolButton.InstantPopup)
        em = QMenu(self)
        for fmt, name in (("docx", "Word（.docx）"), ("pdf", "PDF（.pdf）"), ("md", "Markdown（.md）"),
                          ("html", "網頁（.html）"), ("txt", "純文字（.txt）"), ("json", "完整原始資料（.json）")):
            em.addAction(name, lambda f=fmt: self.win.export(self.detail, f, self.act_steps.isChecked()))
        em.addSeparator()
        self.act_steps = em.addAction("附上執行過程")
        self.act_steps.setCheckable(True)
        self.export_btn.setMenu(em)
        rh.addWidget(self.export_btn)
        rv.addLayout(rh)
        self.view = QTextBrowser()
        self.view.document().setDefaultStyleSheet(theme.doc_css())
        self.view.document().setDocumentMargin(16)
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self.on_link)
        rv.addWidget(self.view)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([380, 900])
        v.addWidget(split, 1)

    # ---------- 資料 → 畫面
    def render(self, wfs, runs):
        self.runs = runs
        self.render_combo(wfs)
        self.render_bar()
        self.render_runs()
        if self.sel_run is None:
            self.show_welcome()

    def render_combo(self, wfs):
        cur = self.sel_wf
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("全部工作流", None)
        for w in wfs:
            st = "執行中" if w["running"] else (STATUS.get((w.get("last") or {}).get("status"), ("", ""))[0] or "還沒跑過")
            nxt = "手動" if is_manual(w["schedule"]) else ("已停用" if not w["enabled"] else f"下次 {when(w['next_due'])}")
            self.combo.addItem(f"{w['title']}　·　{st}　·　{nxt}", w["name"])
        idx = self.combo.findData(cur)
        self.combo.setCurrentIndex(max(idx, 0))
        self.combo.blockSignals(False)

    def render_bar(self):
        w = self.win.wf.get(self.sel_wf)
        manual = w is None or is_manual(w["schedule"])
        self.act_edit.setEnabled(w is not None)
        self.act_del.setEnabled(w is not None)
        self.enable.setVisible(w is not None and not manual)
        self.inp.setVisible(w is not None and manual)
        self.run_btn.setVisible(w is not None)
        if not w:
            self.info.setText("從左邊選一條工作流，可以執行、開關排程或修改設定。")
            return
        bits = [sched_text(w["schedule"])]
        if w.get("last"):
            bits.append(f"上次 {when(w['last']['started'])}")
        if w.get("next_due") and w["enabled"] and not manual:
            bits.append(f"下次 {when(w['next_due'])}")
        self.info.setText(f"{w['description']}\n{' · '.join(bits)} · {self.win.model_name(w.get('model'), w['provider'])}")
        self.enable.blockSignals(True)
        self.enable.setChecked(w["enabled"])
        self.enable.blockSignals(False)
        self.run_btn.setText("執行中…" if w["running"] else "立即執行")
        self.run_btn.setEnabled(not w["running"])

    def render_runs(self):
        rows = [r for r in self.runs if not self.sel_wf or r["workflow"] == self.sel_wf]
        self.runs_title.setText(f"「{self.win.wf_title(self.sel_wf)}」的執行紀錄" if self.sel_wf else "最近的執行")
        sel = self.sel_run
        self.run_list.blockSignals(True)
        known = [self.run_list.item(i).data(Qt.UserRole) for i in range(self.run_list.count())]
        new_ids = [r["id"] for r in rows]
        if known != new_ids or getattr(self, "_rows_sig", None) != json.dumps(rows, sort_keys=True, default=str):
            self._rows_sig = json.dumps(rows, sort_keys=True, default=str)
            before = self.run_list.tops()
            fresh = not before or getattr(self, "_list_filter", 0) != self.sel_wf
            self._list_filter = self.sel_wf
            self.run_list.clear()
            for r in rows:
                st = STATUS.get(r["status"], (r["status"], "muted"))
                took = dur(r["finished"] - r["started"]) if r["finished"] else ("進行中" if r["status"] == "running" else "")
                it = QListWidgetItem(self.win.wf_title(r['workflow']))
                it.setData(Qt.UserRole, r["id"])
                it.setData(Qt.UserRole + 1, {
                    "title": self.win.wf_title(r['workflow']), "dot": C(st[1]),
                    "meta": f"{TRIGGER.get(r['trigger'], '手動')} · {self.win.model_name(r['model'], r['provider'])}",
                    "status_text": st[0], "status_color": C(st[1]),
                    "right": when(r['started']) + (f" · {took}" if took else "")})
                self.run_list.addItem(it)
            self.run_list.stagger() if fresh else self.run_list.flip(before)
        if sel is None and rows:
            sel = rows[0]["id"]
        for i in range(self.run_list.count()):
            if self.run_list.item(i).data(Qt.UserRole) == sel:
                self.run_list.setCurrentRow(i)
        self.run_list.blockSignals(False)
        if sel and sel != self.sel_run or (sel and self._detail_stale()):
            self.sel_run = sel
            self.load_detail()
        elif not rows:
            self.sel_run = None

    def _detail_stale(self):
        r = next((x for x in self.runs if x["id"] == self.sel_run), None)
        return bool(r and self.detail and (r["status"] != self.detail["run"]["status"] or r["status"] == "running"))

    def load_detail(self):
        rid = self.sel_run
        self.b.get(f"/api/runs/{rid}", lambda d: self.show_detail(d) if rid == self.sel_run and d and "run" in d else None)

    def show_detail(self, d):
        self.detail = d
        r = d["run"]
        st = STATUS.get(r["status"], (r["status"], "muted"))
        took = f"，花了 {dur(r['finished'] - r['started'])}" if r["finished"] else ""
        parts = [f"<h2 style='margin:0'>{esc(self.win.wf_title(r['workflow']))} "
                 f"<span style='color:{C(st[1])};font-size:15px'>{st[0]}</span></h2>",
                 f"<p style='color:{C('muted')}'>{when(r['started'])}{TRIGGER.get(r['trigger'], '手動')}開始{took}，"
                 f"使用 {esc(self.win.model_name(r['model'], r['provider']))}</p>"]
        if r["status"] == "running":
            parts.append(f"<p style='color:{C('run')}'>還在跑，看上面「現在」那一區</p>")
        if r["output"]:
            parts.append(f"<h4 style='color:{C('muted')}'>結果</h4>" + md_to_html(r["output"]))
        if r["status"] == "cancelled":
            parts.append(f"<p style='color:{C('muted')}'>你在這裡停止了執行，沒有產出結果。</p>")
        elif r["error"]:
            parts.append(f"<h4 style='color:{C('muted')}'>哪裡出錯</h4><p style='color:{C('bad')}'>{esc(human_error(r['error']))}</p>")
        total = sum((s["ms"] or 0) for s in d["steps"] if s["kind"] in ("llm", "tool")) / 1000
        model = sum((s["ms"] or 0) for s in d["steps"] if s["kind"] == "llm") / 1000
        if total:
            parts.append(f"<h4 style='color:{C('muted')}'>過程</h4><p style='color:{C('muted')}'>"
                         f"模型在讀、在想：{dur(model) or '不到 1 秒'}（{round(model / total * 100)}%）　"
                         f"使用工具：{dur(total - model) or '不到 1 秒'}（{round((total - model) / total * 100)}%）</p>")
        parts.append(process_html(d["steps"]))
        same = self.shown_detail == r["id"]
        scroll = self.view.verticalScrollBar().value() if same else 0

        def apply():
            self.view.setHtml("".join(parts))
            self.view.verticalScrollBar().setValue(scroll)
        self.export_btn.setEnabled(r["status"] != "running")
        if same:
            apply()
        elif self.shown_detail is None:
            apply()
            slide_in(self.view)
        else:
            motion.swap(self.view, apply)               # 換一筆：舊的先離場，新的再進場
        self.shown_detail = r["id"]

    def show_welcome(self):
        self.detail = None
        self.export_btn.setEnabled(False)
        if self.sel_wf:
            self.view.setHtml(f"<p style='color:{C('muted')}'>這條工作流還沒有執行紀錄，按上面的「立即執行」試試看。</p>")
            return
        ok = [self.win.prov_label(n) for n, v in self.win.prov.items() if v.get("ok")]
        wf_rows = "".join(f"<tr><td style='padding:6px 0'><b>{esc(w['title'])}</b><br><span style='color:{C('muted')}'>"
                          f"{esc(w['description'])}</span></td><td style='padding-left:16px'><a href='run:{w['name']}'>執行</a></td></tr>"
                          for w in self.win.wf.values())
        self.view.setHtml(
            "<h2>開始使用</h2><ol>"
            + (f"<li>模型來源已經有 <b>{esc('、'.join(ok))}</b> 可用。</li>" if ok else
               "<li>先到 <a href='tab:settings'>設定 → 模型來源</a> 準備至少一個模型。</li>")
            + "<li>挑一條工作流按「執行」，上面「現在」區會即時顯示它在做什麼。</li>"
              "<li>想讓它照你的方法做事：到 <a href='tab:skills'>Skills</a> 匯入或用對話建立一個 skill。</li></ol>"
            + f"<h4 style='color:{C('muted')}'>現有的工作流</h4><table width='100%'>{wf_rows}</table>")

    def render_live(self, live):
        mine = live[0] if live else None
        if not mine:
            if self.live.isVisible():
                self.live.hide()
            self.live_prev = {}
            return
        self.live_wf = mine["workflow"]
        first = not self.live.isVisible()
        self.live.show()
        if first:
            slide_in(self.live)
        L = mine
        el = max(0, time.time() - (L.get("since") or L["started"]))
        phase = L.get("phase")
        tool = L.get("tool") or {}
        if phase in ("tool", "deciding"):
            v, o = describe(tool.get("name", ""), tool.get("args"))
            what, lab = ("正在" if phase == "tool" else "決定要") + v + (f"：{o}" if o else ""), v
        elif phase == "thinking":
            what, lab = "正在思考", "思考"
        elif phase == "writing":
            what, lab = "正在寫結果", "寫結果"
        else:
            what, lab = ("正在閱讀任務說明" if L["round"] == 1 else "正在消化剛才拿到的資料"), "讀資料"
        self.live_title.setText(L["title"])
        self.live_meta.setText(f"已經跑了 {dur(time.time() - L['started'])} · {self.win.model_name(L.get('model'), L.get('provider'))}")
        if self.live_prev.get("what") != phase + lab:
            slide_in(self.live_what, 4)
        self.live_what.setText(f"{esc(what)}　<span style='color:{C('muted')};font-size:13px'>{dur(el) or '0 秒'}</span>")
        self.live_what.setTextFormat(Qt.RichText)
        mx = (self.win.wf.get(L["workflow"]) or {}).get("max_steps") or (self.win.settings.get("limits") or {}).get("max_steps", 12)
        self.live_prog.setMaximum(mx)
        self.live_prog.setValue(min(L["round"], mx))
        self.live_prog_text.setText(f"第 {L['round']} 輪，最多 {mx} 輪" + ("　快到上限了" if L["round"] >= mx * 0.75 else ""))
        steps = self.live_prev.get("steps", [])
        self.live_bar.set_segments(seg_list(steps, ("tool" if phase == "tool" else "model", max(el, 0.05), lab)))
        idle = 0 if phase == "waiting" else time.time() - (L.get("last_token") or L.get("since") or L["started"])
        stall = (self.win.settings.get("limits") or {}).get("stall_seconds", 45)
        self.live_note.setText(L["broken"] if L.get("broken") else
                               f"已經 {dur(idle)} 沒有新的輸出，模型可能卡住了，可以按「停止」。" if idle > stall else "")
        self.live_note.setStyleSheet(f"color:{C('bad')}")
        acts = [s for s in steps if s["kind"] not in ("llm", "note")][-4:]
        self.live_recent.setText("".join(act_html(s) for s in acts))
        self.live_recent.setTextFormat(Qt.RichText)
        text = L.get("reasoning") if phase == "thinking" else L.get("content") if phase == "writing" else ""
        self.live_stream.setVisible(bool(text))
        if text and self.live_stream.toPlainText() != text:
            sb = self.live_stream.verticalScrollBar()
            stick = sb.value() >= sb.maximum() - 4
            self.live_stream.setPlainText(text)
            if stick:
                sb.setValue(sb.maximum())
        if self.live_prev.get("round") != L["round"] or phase == "waiting":
            rid = L["run_id"]
            self.b.get(f"/api/runs/{rid}", lambda d: self.live_prev.update(steps=d.get("steps", [])) if d else None)
        self.live_prev.update(what=phase + lab, round=L["round"])

    # ---------- 操作
    def on_pick(self, i):
        self.sel_wf = self.combo.itemData(i)
        self.sel_run = None
        self.shown_detail = None
        self.win.refresh()

    def on_select_run(self):
        it = self.run_list.currentItem()
        if it and it.data(Qt.UserRole) != self.sel_run:
            self.sel_run = it.data(Qt.UserRole)
            self.load_detail()

    def on_enable(self, on):
        self.b.post(f"/api/workflows/{self.sel_wf}/enabled", {"enabled": on}, lambda r: self.win.refresh())

    def on_run(self):
        if not self.sel_wf or not self.run_btn.isEnabled():
            return
        self.run_btn.setEnabled(False)
        text = self.inp.text()
        self.inp.clear()
        self.b.post(f"/api/workflows/{self.sel_wf}/run", {"input": text}, lambda r: self._after_run())

    def _after_run(self):
        self.sel_run = None
        QTimer.singleShot(400, self.win.refresh)

    def on_delete(self):
        w = self.win.wf.get(self.sel_wf)
        if not w:
            return
        if QMessageBox.question(self, "刪除工作流", f"刪除「{w['title']}」？\n檔案會移到 workflows/.trash，之後可以救回來。") != QMessageBox.Yes:
            return
        self.b.post(f"/api/workflows/{self.sel_wf}/delete", {}, self._after_delete)

    def _after_delete(self, r):
        if not r.get("ok"):
            QMessageBox.warning(self, "刪除失敗", r.get("error", ""))
            return
        self.sel_wf, self.sel_run = None, None
        self.win.refresh()

    def on_link(self, url):
        u = url.toString()
        if u.startswith("run:"):
            self.sel_wf = u[4:]
            self.sel_run = None
            self.win.refresh()
            w = self.win.wf.get(self.sel_wf)
            if w and is_manual(w["schedule"]):
                self.inp.setFocus()
                self.win.toast("在上面的輸入框寫下要處理什麼，再按「立即執行」")
            else:
                self.on_run()
        elif u.startswith("tab:"):
            self.win.tabs.setCurrentIndex({"skills": 1, "settings": 2}[u[4:]])
        elif u.startswith(("http://", "https://")):
            webbrowser.open(u)


# ---------------------------------------------------------------- 工作流編輯
class EditorDialog(QDialog):
    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_entered", False):      # 遮罩先到位，內容再進場（配方 §6）
            self._entered = True
            QTimer.singleShot(0, lambda: motion.window_in(self))

    def __init__(self, win, w=None, pre=None):
        super().__init__(win)
        self.win, self.w = win, w
        v = w or pre or {}
        self.setWindowTitle(f"編輯「{w['title']}」" if w else "用這個 skill 建立工作流" if pre else "新增工作流")
        self.setMinimumWidth(640)
        f = QFormLayout(self)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.title = QLineEdit(v.get("title", ""))
        self.name = QLineEdit((pre or {}).get("name", ""))
        self.name.setPlaceholderText("英文小寫、數字、連字號，例如 disk-watch（也是檔名，建立後不能改）")
        self.desc = QLineEdit(v.get("description", ""))
        self.desc.setPlaceholderText("一句話說它解決什麼問題（也會交給模型）")
        f.addRow("名稱", self.title)
        if not w:
            f.addRow("代號", self.name)
        f.addRow("說明", self.desc)

        s = v.get("schedule") or {}
        row = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.addItems(["手動執行", "每天固定時間", "每隔一段時間"])
        self.kind.setCurrentIndex(1 if s.get("daily") else 2 if s.get("every_minutes") else 0)
        self.daily = QLineEdit(s.get("daily", "08:00"))
        self.daily.setInputMask("99:99")
        self.daily.setMaximumWidth(70)
        self.every = QComboBox()
        for m, t in ((15, "15 分鐘"), (30, "30 分鐘"), (60, "1 小時"), (120, "2 小時"), (360, "6 小時"), (720, "12 小時")):
            self.every.addItem(t, m)
        if s.get("every_minutes") and self.every.findData(s["every_minutes"]) < 0:
            self.every.addItem(f"{s['every_minutes']} 分鐘", s["every_minutes"])
        self.every.setCurrentIndex(max(0, self.every.findData(s.get("every_minutes", 60))))
        self.enabled = QCheckBox("啟用排程")
        self.enabled.setChecked(w["enabled"] if w else True)
        for x in (self.kind, self.daily, self.every, self.enabled):
            row.addWidget(x)
        row.addStretch()
        self.kind.currentIndexChanged.connect(self.sync)
        f.addRow("什麼時候跑", row)

        prov = QHBoxLayout()
        self.provider = QComboBox()
        self.provider.addItem("自動（依優先順序和用量挑）", "auto")
        for n, p in win.prov.items():
            self.provider.addItem(win.prov_label(n) + ("" if p.get("ok") else "（不能用）"), n)
        self.provider.setCurrentIndex(max(0, self.provider.findData(v.get("provider") or win.first_provider())))
        self.model = QComboBox()
        self.model.setEditable(True)
        prov.addWidget(self.provider)
        prov.addWidget(self.model, 1)
        f.addRow("模型", prov)
        fb = QHBoxLayout()
        self.fallback = QComboBox()
        self.fb_model = QComboBox()
        self.fb_model.setEditable(True)
        fb.addWidget(self.fallback)
        fb.addWidget(self.fb_model, 1)
        f.addRow("連不上時", fb)
        self.provider.currentIndexChanged.connect(lambda: self.fill_models(""))
        self.fallback.currentIndexChanged.connect(lambda: self.fill_fb_models(""))
        self.fill_models(v.get("model") or "")
        self.fill_fallback(v.get("fallback") if w else "")
        self.fill_fb_models(v.get("fallback_model") or "")

        grid = QGridLayout()
        self.skill_boxes = {}
        have = set(v.get("skills") or [])
        tools = [s for s in win.skills if s["kind"] == "tool"]
        for i, sk in enumerate(tools):
            cb = QCheckBox(SKILL_VERB.get(sk["name"], sk["description"][:14] or sk["name"]))
            cb.setToolTip(f"{sk['name']}：{sk['description']}")
            cb.setChecked(sk["name"] in have)
            self.skill_boxes[sk["name"]] = cb
            grid.addWidget(cb, i // 3, i % 3)
        f.addRow("能做的事", grid)
        self.task = QPlainTextEdit(v.get("task", ""))
        self.task.setPlaceholderText("一步一步寫清楚要做什麼、每步用哪個能力、什麼情況才通知")
        self.task.setMinimumHeight(150)
        f.addRow("任務說明", self.task)
        self.system = QPlainTextEdit(v.get("system") or "你是自動執行任務的 agent，一律用繁體中文（台灣）。")
        self.system.setMaximumHeight(70)
        f.addRow("角色設定", self.system)
        lim = QHBoxLayout()
        self.steps = QSpinBox()
        self.steps.setRange(1, 60)
        self.steps.setValue(v.get("max_steps") or (win.settings.get("limits") or {}).get("max_steps", 12))
        self.tokens = QSpinBox()
        self.tokens.setRange(0, 65536)
        self.tokens.setSingleStep(512)
        self.tokens.setSpecialValueText("預設")
        self.tokens.setValue(v.get("max_tokens") or 0)
        lim.addWidget(QLabel("最多"))
        lim.addWidget(self.steps)
        lim.addWidget(QLabel("輪；單輪最多輸出"))
        lim.addWidget(self.tokens)
        lim.addWidget(QLabel("tokens"))
        lim.addStretch()
        f.addRow("限制", lim)
        self.err = label("", "bad")
        f.addRow(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("儲存")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.sync()

    def sync(self):
        k = self.kind.currentIndex()
        self.daily.setVisible(k == 1)
        self.every.setVisible(k == 2)
        self.enabled.setVisible(k != 0)

    def fill_models(self, cur):
        p = self.provider.currentData()
        self.model.clear()
        if p == "auto":
            self.model.addItem("由自動模式決定（設定 → 自動模式）", "")
            self.model.setEnabled(False)
            self.fallback.setEnabled(False)
            self.fb_model.setEnabled(False)
            return
        self.model.setEnabled(True)
        self.fallback.setEnabled(True)
        self.fb_model.setEnabled(True)
        info = self.win.prov.get(p, {})
        self.model.addItem(f"預設（{info.get('default_model') or '由來源決定'}）", "")
        for m in [x for x in info.get("models", []) if "embed" not in x.lower()]:
            self.model.addItem(m, m)
        if cur:
            i = self.model.findData(cur)
            if i < 0:
                self.model.addItem(cur, cur)
                i = self.model.count() - 1
            self.model.setCurrentIndex(i)
        self.fill_fallback(self.fallback.currentData() or "")

    def fill_fallback(self, cur):
        p = self.provider.currentData()
        self.fallback.blockSignals(True)
        self.fallback.clear()
        self.fallback.addItem("直接失敗", "")
        for n, info in self.win.prov.items():
            if n == p:
                continue
            ok = info.get("ok") or re.search(r"API.?KEY", info.get("error", ""), re.I)
            self.fallback.addItem(f"改用 {self.win.prov_label(n)}{'' if ok else '（不能用）'}", n)
            if not ok and n != cur:
                self.fallback.model().item(self.fallback.count() - 1).setEnabled(False)
        self.fallback.setCurrentIndex(max(0, self.fallback.findData(cur)))
        self.fallback.blockSignals(False)

    def fill_fb_models(self, cur):
        p = self.fallback.currentData()
        self.fb_model.clear()
        self.fb_model.setVisible(bool(p))
        if not p:
            return
        self.fb_model.addItem("用它的預設模型", "")
        for m in [x for x in self.win.prov.get(p, {}).get("models", []) if "embed" not in x.lower()]:
            self.fb_model.addItem(m, m)
        if cur:
            i = self.fb_model.findData(cur)
            if i < 0:
                self.fb_model.addItem(cur, cur)
                i = self.fb_model.count() - 1
            self.fb_model.setCurrentIndex(i)

    def save(self):
        k = self.kind.currentIndex()
        model = self.model.currentData() if self.model.currentText() == self.model.itemText(self.model.currentIndex()) else self.model.currentText()
        body = {"name": self.name.text().strip(), "title": self.title.text(), "description": self.desc.text(),
                "provider": self.provider.currentData(), "model": model or "", "fallback": self.fallback.currentData() or "",
                "fallback_model": self.fb_model.currentData() or "" if self.fb_model.isVisible() else "",
                "enabled": True if k == 0 else self.enabled.isChecked(),
                "schedule": {"daily": self.daily.text()} if k == 1 else {"every_minutes": self.every.currentData()} if k == 2 else {},
                "skills": [n for n, cb in self.skill_boxes.items() if cb.isChecked()],
                "task": self.task.toPlainText(), "system": self.system.toPlainText(), "max_steps": self.steps.value()}
        if self.tokens.value():
            body["max_tokens"] = self.tokens.value()
        path = f"/api/workflows/{self.w['name']}/save" if self.w else "/api/workflows/new"
        self.err.setText("")
        self.win.backend.post(path, body, self._saved)

    def _saved(self, r):
        if not r.get("ok"):
            self.err.setText(r.get("error", "儲存失敗"))
            return
        if not self.w:
            self.win.wf_tab.sel_wf = self.name.text().strip()
        self.accept()
        self.win.refresh()


# ---------------------------------------------------------------- Skills
class SkillsTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.b = win, win.backend
        self.builder_msgs, self.pending_py = [], None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0)
        bar = QFrame()
        bar.setObjectName("panel")
        h = QHBoxLayout(bar)
        imp = QToolButton()
        imp.setText("匯入 skill")
        imp.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(self)
        m.addAction("上傳檔案（.zip、SKILL.md 或 .py）…", self.pick_file)
        m.addAction("從 GitHub 網址匯入…", self.show_github)
        imp.setMenu(m)
        b = QPushButton("用對話建立 skill")
        b.clicked.connect(self.show_builder)
        h.addWidget(imp)
        h.addWidget(b)
        h.addWidget(label("知識型 skill（SKILL.md）是給模型看的做事方法；工具型 skill（.py）是模型能呼叫的動作。", "muted"), 1)
        v.addWidget(bar)
        split = QSplitter()
        self.list = motion.MotionList()
        self.list.setItemDelegate(theme.HeaderDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.itemSelectionChanged.connect(self.on_select)
        self.stack = QStackedWidget()
        split.addWidget(self.list)
        split.addWidget(self.stack)
        split.setSizes([380, 900])
        v.addWidget(split, 1)
        self.empty = label("選一個 skill，或匯入、用對話建立一個", "muted")
        self.empty.setAlignment(Qt.AlignCenter)
        self.stack.addWidget(self.empty)

    def load(self, keep=None, animate=False):
        self.b.get("/api/skills", lambda s: self._render(s or [], keep, animate))

    def _render(self, skills, keep, animate=False):
        self.win.skills = skills
        self.list.blockSignals(True)
        self.list.clear()
        src = {"builtin": "內建", "imported": "匯入", "linked": "連結"}
        for kind, head in (("knowledge", "知識型（SKILL.md）"), ("tool", "工具型")):
            items = [s for s in skills if s["kind"] == kind]
            hdr = QListWidgetItem(f"{head} · {len(items)}")
            hdr.setFlags(Qt.NoItemFlags)
            self.list.addItem(hdr)
            for s in items:
                it = QListWidgetItem(s.get('title') or s['name'])
                it.setData(Qt.UserRole, s["name"])
                it.setData(Qt.UserRole + 1, {
                    "title": s.get('title') or s['name'], "meta": (s['description'] or '（沒有說明）')[:120],
                    "status_text": f"{'知識' if kind == 'knowledge' else '工具'} · {src.get(s['source'], s['source'])}",
                    "status_color": C("muted")})
                self.list.addItem(it)
                if s["name"] == keep:
                    self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        if animate:
            self.list.stagger()
        if keep:
            self.show_skill(keep)

    def set_page(self, w):
        old = self.stack.currentWidget()
        self.stack.addWidget(w)
        self.stack.setCurrentWidget(w)
        if old is not self.empty and old is not None:
            self.stack.removeWidget(old)
            old.deleteLater()
        slide_in(w)

    def on_select(self):
        it = self.list.currentItem()
        if it and it.data(Qt.UserRole):
            self.show_skill(it.data(Qt.UserRole))

    def show_skill(self, name):
        s = next((x for x in self.win.skills if x["name"] == name), None)
        if not s:
            return
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(label(s.get("title") or s["name"], size=18, bold=True))
        v.addWidget(label(f"知識型 skill · 資料夾 skills/{s['name']} · {s.get('files', 1)} 個檔案" if s["kind"] == "knowledge"
                          else f"工具型 skill · skills/{s['name']}.py", "muted"))
        v.addWidget(label(s["description"] or "（沒有說明）"))
        v.addWidget(hline())
        v.addWidget(label("試用", "muted", bold=True))
        v.addWidget(label("模型會照這個 skill 的方法處理你的要求（可以上網搜尋、讀網頁、存報告）。" if s["kind"] == "knowledge"
                          else "模型會用這個工具處理你的要求。", "muted"))
        row = QHBoxLayout()
        inp = QLineEdit()
        inp.setPlaceholderText("要它做什麼？")
        prov = QComboBox()
        for n, p in self.win.prov.items():
            if p.get("ok"):
                prov.addItem(self.win.prov_label(n), n)
        prov.setCurrentIndex(max(0, prov.findData("claude")))
        go = QPushButton("執行")
        row.addWidget(inp, 1)
        row.addWidget(prov)
        row.addWidget(go)
        v.addLayout(row)

        def run():
            if not inp.text().strip():
                inp.setFocus()
                return
            go.setEnabled(False)
            self.b.post(f"/api/skills/{name}/run", {"input": inp.text(), "provider": prov.currentData()}, after_run)

        def after_run(r):
            go.setEnabled(True)
            if not r.get("ok"):
                QMessageBox.warning(self, "無法執行", r.get("error", ""))
                return
            t = self.win.wf_tab
            t.sel_wf, t.sel_run = None, None
            self.win.tabs.setCurrentIndex(0)
            QTimer.singleShot(400, self.win.refresh)
        go.clicked.connect(run)
        inp.returnPressed.connect(run)
        v.addWidget(hline())
        row2 = QHBoxLayout()
        mk = QPushButton("做成工作流")
        mk.clicked.connect(lambda: self.win.open_editor(None, self.prefill(s)))
        row2.addWidget(mk)
        if s["source"] != "builtin":
            d = QPushButton("移除連結" if s["source"] == "linked" else "刪除")
            d.clicked.connect(lambda: self.delete(s))
            row2.addWidget(d)
        else:
            row2.addWidget(label("內建的 skill 不能刪", "muted"))
        row2.addStretch()
        v.addLayout(row2)
        v.addStretch()
        self.set_page(page)

    def prefill(self, s):
        k = s["kind"] == "knowledge"
        return {"name": s["name"].replace("_", "-"), "title": s.get("title") or s["name"], "description": s["description"][:120],
                "provider": self.win.first_provider(),
                "skills": ["use_skill", "read_skill_file", "web_search", "fetch_url", "save_report"] if k else [s["name"]],
                "system": f"你是自動執行任務的 agent，一律用繁體中文（台灣）。一開始先用 use_skill 載入「{s['name']}」，照它的指示做事。" if k else None,
                "task": "（寫下這個工作流每次要做的事；手動執行時輸入框的文字會附在最後）"}

    def delete(self, s):
        if QMessageBox.question(self, "刪除 skill", f"刪除「{s.get('title') or s['name']}」？（會移到 skills/.trash）") != QMessageBox.Yes:
            return
        self.b.post(f"/api/skills/{s['name']}/delete", {}, lambda r: (self.load(), self.stack.setCurrentWidget(self.empty))
                    if r.get("ok") else QMessageBox.warning(self, "刪除失敗", r.get("error", "")))

    # 匯入
    def pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "選擇 skill", "", "Skill (*.zip *.md *.py)")
        if not path:
            return
        data = open(path, "rb").read()
        body = {"type": "file", "filename": path.replace("\\", "/").rsplit("/", 1)[-1], "content": base64.b64encode(data).decode()}
        if path.lower().endswith(".py"):
            if QMessageBox.warning(self, "匯入 Python 工具",
                                   "這是一段 Python 程式。匯入後，模型呼叫它時會以你的帳號權限在這台電腦上執行，"
                                   "可以讀寫檔案、連網路。\n\n只匯入你自己寫的、或你看過內容並信任的檔案。確定要匯入嗎？",
                                   QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
                return
            body["confirm_code"] = True
        self.b.post("/api/skills/import", body, self._imported)

    def show_github(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(label("從 GitHub 匯入", size=18, bold=True))
        v.addWidget(label("貼上含有 SKILL.md 的 repo 網址。整個 repo 裡有好幾個 skill 會一起匯入；只要其中一個，就貼那個資料夾的網址（…/tree/main/路徑）。", "muted"))
        row = QHBoxLayout()
        url = QLineEdit()
        url.setPlaceholderText("https://github.com/owner/repo")
        go = QPushButton("匯入")
        row.addWidget(url, 1)
        row.addWidget(go)
        v.addLayout(row)
        msg = label("", "muted")
        v.addWidget(msg)
        v.addStretch()

        def do():
            msg.setText("匯入中…")
            go.setEnabled(False)
            self.b.post("/api/skills/import", {"type": "github", "url": url.text()},
                        lambda r: (go.setEnabled(True), self._imported(r, msg)))
        go.clicked.connect(do)
        url.returnPressed.connect(do)
        self.set_page(page)
        url.setFocus()

    def _imported(self, r, msg=None):
        if not r.get("ok"):
            if msg:
                msg.setText(f"<span style='color:{C('bad')}'>{esc(r.get('error'))}</span>")
            else:
                QMessageBox.warning(self, "匯入失敗", r.get("error", ""))
            return
        self.win.toast(f"已匯入：{'、'.join(r['imported'])}")
        self.load(keep=r["imported"][0])

    # 對話構建器
    def show_builder(self):
        self.list.clearSelection()
        page = QWidget()
        g = QHBoxLayout(page)
        left, right = QVBoxLayout(), QVBoxLayout()
        self.chat = QTextBrowser()
        self.chat.document().setDefaultStyleSheet(theme.doc_css())
        self.chat.document().setDocumentMargin(16)
        self.chat.setOpenExternalLinks(True)
        self.b_in = QPlainTextEdit()
        self.b_in.setPlaceholderText("描述你想要的 skill，或要怎麼改（⌘/Ctrl + Enter 送出）")
        self.b_in.setMaximumHeight(90)
        self.b_in.installEventFilter(self)
        row = QHBoxLayout()
        self.b_prov = QComboBox()
        for n, p in self.win.prov.items():
            if p.get("ok"):
                self.b_prov.addItem(self.win.prov_label(n), n)
        self.b_prov.setCurrentIndex(max(0, self.b_prov.findData("claude")))
        self.b_send = QPushButton("送出")
        self.b_send.clicked.connect(self.builder_send)
        reset = QPushButton("重新開始")
        reset.clicked.connect(self.builder_reset)
        row.addWidget(self.b_prov)
        row.addWidget(self.b_send)
        row.addWidget(reset)
        row.addStretch()
        left.addWidget(label("Skill 對話構建器", size=18, bold=True))
        left.addWidget(self.chat, 1)
        left.addWidget(self.b_in)
        left.addLayout(row)
        self.draft = QPlainTextEdit()
        self.draft.setPlaceholderText("構建器寫的 SKILL.md 草稿會出現在這裡（可以直接改）")
        f = QFont("Menlo")
        f.setStyleHint(QFont.Monospace)
        self.draft.setFont(f)
        save = QPushButton("存成 skill")
        save.clicked.connect(self.builder_save)
        self.b_msg = label("", "muted")
        right.addWidget(label("SKILL.md 草稿", "muted", bold=True))
        right.addWidget(self.draft, 1)
        sr = QHBoxLayout()
        sr.addWidget(save)
        sr.addWidget(self.b_msg, 1)
        right.addLayout(sr)
        g.addLayout(left, 1)
        g.addLayout(right, 1)
        self.set_page(page)
        self.render_chat()
        self.b_in.setFocus()

    def eventFilter(self, obj, ev):
        if obj is getattr(self, "b_in", None) and ev.type() == ev.Type.KeyPress and \
                ev.key() in (Qt.Key_Return, Qt.Key_Enter) and ev.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            self.builder_send()
            return True
        return super().eventFilter(obj, ev)

    def render_chat(self, waiting=False):
        bubbles = [f"<p style='background:{C('tint')};padding:8px'>想做一個什麼樣的 skill？說說它要處理什麼、輸入是什麼、希望輸出長怎樣。</p>"] \
            if not self.builder_msgs else []
        for m in self.builder_msgs:
            txt = re.sub(r"```(?:skill|markdown|md)?\s*\n---.*?```", "（草稿已更新 →）", m["content"], flags=re.S)
            align = "right" if m["role"] == "user" else "left"
            bubbles.append(f"<p align='{align}' style='background:{C('tint') if m['role'] != 'user' else C('model')};"
                           f"{'color:#1e1e3c;' if m['role'] == 'user' else ''}padding:8px'>{esc(txt).replace(chr(10), '<br>')}</p>")
        if waiting:
            bubbles.append(f"<p style='color:{C('muted')}'>● 正在寫…</p>")
        self.chat.setHtml("".join(bubbles))
        self.chat.verticalScrollBar().setValue(self.chat.verticalScrollBar().maximum())

    def builder_send(self):
        text = self.b_in.toPlainText().strip()
        if not text or not self.b_send.isEnabled():
            return
        self.b_in.clear()
        self.builder_msgs.append({"role": "user", "content": text})
        self.render_chat(True)
        self.b_send.setEnabled(False)
        self.b.post("/api/builder/chat", {"messages": self.builder_msgs, "provider": self.b_prov.currentData()}, self._builder_reply)

    def _builder_reply(self, r):
        self.b_send.setEnabled(True)
        if not r.get("ok"):
            self.builder_msgs.pop()
            self.render_chat()
            self.b_msg.setText(f"<span style='color:{C('bad')}'>{esc(r.get('error'))}</span>")
            return
        self.builder_msgs.append({"role": "assistant", "content": r["reply"]})
        if r.get("draft"):
            self.draft.setPlainText(r["draft"])
        self.render_chat()

    def builder_reset(self):
        self.builder_msgs = []
        self.draft.clear()
        self.render_chat()

    def builder_save(self):
        self.b.post("/api/builder/save", {"draft": self.draft.toPlainText()}, self._builder_saved)

    def _builder_saved(self, r):
        if not r.get("ok"):
            self.b_msg.setText(f"<span style='color:{C('bad')}'>{esc(r.get('error'))}</span>")
            return
        self.builder_msgs = []
        self.win.toast(f"已存成 skill：{r['name']}")
        self.load(keep=r["name"])


# ---------------------------------------------------------------- 設定
class SettingsTab(QScrollArea):
    BUILTIN = {"lmstudio", "deepseek", "claude", "codex", "gemini"}

    def __init__(self, win):
        super().__init__()
        self.win, self.b = win, win.backend
        self.setWidgetResizable(True)
        self.s, self.fields, self.prov_rows, self.auto_rows = {}, {}, {}, []

    def load(self):
        self.b.get("/api/settings", lambda s: self.b.get("/api/usage", lambda u: self.build(s or {}, u or {})))

    def section(self, v, title, note=""):
        box = QFrame()
        box.setObjectName("panel")
        f = QFormLayout(box)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        f.addRow(label(title, size=16, bold=True))
        if note:
            f.addRow(label(note, "muted"))
        v.addWidget(box)
        self._secs.append(box)
        return f

    def num(self, f, text, path, lo, hi, hint="", unit=""):
        sp = QSpinBox()
        sp.setRange(lo, hi)
        cur = self.s
        for k in path.split("."):
            cur = cur.get(k, {}) if isinstance(cur, dict) else {}
        sp.setValue(int(cur) if not isinstance(cur, dict) else lo)
        if unit:
            sp.setSuffix(f" {unit}")
        self.fields[path] = sp
        f.addRow(text, sp)
        if hint:
            f.addRow("", label(hint, "muted"))

    def txt(self, f, text, path, hint="", ph=""):
        cur = self.s
        for k in path.split("."):
            cur = cur.get(k, "") if isinstance(cur, dict) else ""
        e = QLineEdit(str(cur))
        e.setPlaceholderText(ph)
        self.fields[path] = e
        f.addRow(text, e)
        if hint:
            f.addRow("", label(hint, "muted"))

    def sw(self, f, text, path, hint=""):
        cur = self.s
        for k in path.split("."):
            cur = cur.get(k, False) if isinstance(cur, dict) else False
        c = QCheckBox()
        c.setChecked(bool(cur))
        self.fields[path] = c
        f.addRow(text, c)
        if hint:
            f.addRow("", label(hint, "muted"))

    def build(self, s, usage):
        self.s, self.usage, self.fields, self.prov_rows, self.auto_rows = s, usage, {}, {}, []
        self._secs = []
        page = QWidget()
        v = QVBoxLayout(page)
        # 模型來源
        f = self.section(v, "模型來源", "工作流和構建器可以用的模型。停用的不會出現在選單裡。")
        self.prov_box = QVBoxLayout()
        f.addRow(self.prov_box)
        for n, p in s.get("providers", {}).items():
            self.add_provider(n, p)
        add = QPushButton("＋ 新增 OpenAI 相容的服務（OpenRouter、Ollama、自架伺服器…）")
        add.clicked.connect(self.new_provider)
        f.addRow(add)
        # 自動模式
        f = self.section(v, "自動模式", "工作流的模型選「自動」時，照這個順序挑第一個可用、而且沒超過用量上限的來源；跑到一半出錯就換下一個。")
        self.auto_box = QVBoxLayout()
        f.addRow(self.auto_box)
        for it in (s.get("auto") or {}).get("order", []):
            self.add_auto(it)
        a = QPushButton("＋ 加一個來源")
        a.clicked.connect(lambda: self.add_auto({"provider": next((n for n in s["providers"] if n not in [r["p"].currentData() for r in self.auto_rows]), "lmstudio")}))
        f.addRow(a)
        self.thr = QSpinBox()
        self.thr.setRange(10, 100)
        self.thr.setSuffix(" %")
        self.thr.setValue(round((s.get("auto") or {}).get("cli_max_utilization", 0.9) * 100))
        f.addRow("訂閱額度門檻", self.thr)
        f.addRow("", label("訂閱 CLI（例如 Claude）回報的額度使用率超過這個比例，自動模式就先跳過它。", "muted"))
        # 本地備用
        f = self.section(v, "本地備用", "跟自動模式、工作流自己的備援都分開。打開後，其他模型全部失敗時，最後改用這個本地模型把事情做完。")
        lf = s.get("local_fallback") or {}
        self.lf_on = QCheckBox("啟用本地備用")
        self.lf_on.setChecked(bool(lf.get("enabled")))
        self.lf_p = QComboBox()
        for n, p in s.get("providers", {}).items():
            self.lf_p.addItem(p.get("label") or n, n)
        self.lf_p.setCurrentIndex(max(0, self.lf_p.findData(lf.get("provider") or "lmstudio")))
        self.lf_m = QLineEdit(lf.get("model", ""))
        self.lf_m.setPlaceholderText("留空：用那個來源的預設（LM Studio 會用當下載入的模型）")
        f.addRow(self.lf_on)
        f.addRow("用哪個來源", self.lf_p)
        f.addRow("模型", self.lf_m)
        # 一般與限制
        f = self.section(v, "一般")
        self.txt(f, "回覆語言", "language", "會加在每個工作流的系統提示最後。", "繁體中文（台灣）")
        self.num(f, "監控頁 port", "server.port", 1024, 65535, "headless / 瀏覽器模式用；改了要重新啟動。")
        self.num(f, "排程檢查間隔", "tick_seconds", 5, 3600, "", "秒")
        f = self.section(v, "執行限制", "防止模型鬼打牆、卡住或跑太久。")
        self.num(f, "單輪最多輸出", "limits.max_tokens", 256, 65536, "工作流可以另外指定。", "tokens")
        self.num(f, "最多幾輪", "limits.max_steps", 1, 60, "", "輪")
        self.num(f, "連續失敗幾次叫它停", "limits.fail_streak", 1, 20, "", "次")
        self.num(f, "API 逾時", "limits.request_timeout", 30, 3600, "", "秒")
        self.num(f, "訂閱 CLI 逾時", "limits.cli_timeout", 30, 7200, "", "秒")
        self.num(f, "卡住提示", "limits.stall_seconds", 10, 3600, "超過這麼久沒有新輸出就提示可能卡住了。", "秒")
        f = self.section(v, "LM Studio 防護")
        self.sw(f, "NaN 崩潰偵測", "lmstudio_guard.nan_watchdog", "模型數值崩潰時自動中止並卸載模型。")
        self.sw(f, "記錄原始輸出", "lmstudio_guard.raw_capture", "每輪保存模型寫的原文，和實際收到的內容對照。")
        f = self.section(v, "搜尋與網頁")
        self.txt(f, "搜尋地區", "search.region", "DuckDuckGo 的地區代碼，例如 tw-tzh、hk-tzh、us-en。")
        self.num(f, "搜尋筆數", "search.limit", 1, 15, "", "筆")
        self.num(f, "網頁最多讀幾字", "fetch.max_chars", 1000, 100000, "", "字")
        f = self.section(v, "通知與匯出")
        self.sw(f, "桌面通知", "notify.enabled", "關掉後，工作流的「跳通知」會直接略過。")
        self.txt(f, "PDF 用的瀏覽器", "export.browser_path", "留空會自動找 Chrome / Edge / Chromium。")
        f = self.section(v, "更新", "從 GitHub Releases 取得新版本。")
        self.up_status = label("", "muted")
        f.addRow("目前版本", self.up_status)
        self.sw(f, "自動檢查更新", "update.auto_check", "每天在背景檢查一次，有新版會在最上面提示。")
        self.sw(f, "自動安裝", "update.auto_install", "有新版就先在背景下載好，下次開啟時自動換上。")
        self.up_token = QLineEdit()
        self.up_token.setEchoMode(QLineEdit.Password)
        self.up_token.setPlaceholderText(((s.get("update") or {}).get("token_hint") or "不需要（公開 repo）") + "；私人 repo 才要填")
        f.addRow("GitHub token", self.up_token)
        ub = QHBoxLayout()
        self.up_check = QPushButton("檢查更新")
        self.up_check.clicked.connect(lambda: (self.up_status.setText("檢查中…"),
                                               self.b.post("/api/update/check", {}, lambda r: self.win._update_status(r))))
        self.up_install = QPushButton("下載並更新")
        self.up_install.clicked.connect(self.win.start_update)
        ub.addWidget(self.up_check)
        ub.addWidget(self.up_install)
        ub.addStretch()
        f.addRow("", ub)
        self.show_update(getattr(self.win, "upd", {}))
        f = self.section(v, "讀檔白名單", "「讀取檔案」只能讀這些路徑。一行一個；* 不跨資料夾、** 會往下找；{data} 是這個程式的資料夾。")
        self.paths = QPlainTextEdit("\n".join(s.get("readable_paths") or []))
        self.paths.setMaximumHeight(100)
        f.addRow(self.paths)
        row = QHBoxLayout()
        save = QPushButton("儲存設定")
        save.setDefault(True)
        save.clicked.connect(self.save)
        self.msg = label("", "muted")
        row.addWidget(save)
        row.addWidget(self.msg, 1)
        v.addLayout(row)
        v.addStretch()
        self.setWidget(page)
        for i, box in enumerate(self._secs[:12]):
            motion.enter(box, delay=40 + i * 40)

    def show_update(self, st):
        if not getattr(self, "up_status", None):
            return
        try:
            self.up_status.isVisible()
        except RuntimeError:                              # 設定頁重建過，舊的元件已經刪掉
            return
        if not st:
            return
        cur = st.get("current", "")
        if st.get("checking"):
            txt = f"{cur}　檢查中…"
        elif st.get("progress") is not None:
            txt = f"{cur}　下載中 {st['progress']}%"
        elif st.get("error"):
            txt = f"{cur}　<span style='color:{C('bad')}'>{esc(st['error'])}</span>"
        elif st.get("update_available"):
            txt = f"{cur}　→ 有新版本 <b>{esc(st['latest']['version'])}</b>" + ("" if st.get("can_install") else f"（{esc(st.get('why_cannot') or '這個平台沒有安裝檔')}）")
        elif st.get("checked"):
            txt = f"{cur}　已經是最新版本（{when(st['checked'])}檢查）"
        else:
            txt = cur
        self.up_status.setText(txt)
        self.up_status.setTextFormat(Qt.RichText)
        self.up_install.setEnabled(bool(st.get("can_install")))

    def add_provider(self, n, p):
        box = QFrame()
        box.setObjectName("card")
        f = QFormLayout(box)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        st = self.win.prov.get(n, {})
        status = "已停用" if p.get("enabled") is False else "可用" if st.get("ok") else (st.get("error") or "檢查中")
        head = QHBoxLayout()
        head.addWidget(label(f"<b>{esc(p.get('label') or n)}</b>　<span style='color:{C('muted')}'>{n} · "
                             f"{'訂閱 CLI' if p.get('type') == 'cli' else 'OpenAI 相容 API'} · {esc(status)}</span>"), 1)
        row = {"enabled": QCheckBox("啟用"), "label": QLineEdit(p.get("label", "")), "default_model": QComboBox(), "box": box}
        row["enabled"].setChecked(p.get("enabled", True) is not False)
        head.addWidget(row["enabled"])
        if n not in self.BUILTIN:
            rm = QPushButton("移除")
            rm.clicked.connect(lambda: (self.prov_rows.pop(n, None), box.deleteLater(), self.msg.setText("記得按「儲存設定」")))
            head.addWidget(rm)
        f.addRow(head)
        f.addRow("顯示名稱", row["label"])
        if p.get("type") == "cli":
            row["path"] = QLineEdit(p.get("path", ""))
            row["path"].setPlaceholderText(f"留空：自動在 PATH 裡找 {p.get('command', '')}")
            f.addRow("執行檔路徑", row["path"])
            if st.get("missing"):
                f.addRow("", label(f"還沒安裝：{esc((st.get('install') or [''])[0])}　{esc(st.get('login', ''))}", "muted"))
        else:
            row["base_url"] = QLineEdit(p.get("base_url", ""))
            row["needs_key"] = QCheckBox("這個服務需要 API key")
            row["needs_key"].setChecked(bool(p.get("needs_key")))
            row["new_key"] = QLineEdit()
            row["new_key"].setEchoMode(QLineEdit.Password)
            row["new_key"].setPlaceholderText(("目前使用環境變數裡的 key" if p.get("key_from_env") else p.get("key_hint") or "還沒設定")
                                              + "；要換就在這裡貼上新的")
            row["clear_key"] = QCheckBox("儲存時清除已存的 key")
            row["clear_key"].setVisible(bool(p.get("key_hint")))
            f.addRow("API 網址", row["base_url"])
            f.addRow("", row["needs_key"])
            f.addRow("API key", row["new_key"])
            f.addRow("", row["clear_key"])

            def sync_key(on, r=row, form=f):
                form.setRowVisible(r["new_key"], on)
                form.setRowVisible(r["clear_key"], on and bool(p.get("key_hint")))
            row["needs_key"].toggled.connect(sync_key)
            sync_key(row["needs_key"].isChecked())
        dm = row["default_model"]
        dm.setEditable(True)
        models = [x for x in st.get("models", []) if "embed" not in x.lower()]
        dm.addItems(([p["default_model"]] if p.get("default_model") and p["default_model"] not in models else []) + models)
        dm.setCurrentText(p.get("default_model", ""))
        dm.lineEdit().setPlaceholderText("留空：用當下載入的模型" if n == "lmstudio" else "留空：用來源自己的預設")
        f.addRow("預設模型", dm)
        self.prov_rows[n] = row
        self.prov_box.addWidget(box)

    def new_provider(self):
        i = 1
        while f"custom{i}" in self.prov_rows:
            i += 1
        self.add_provider(f"custom{i}", {"label": "新的模型來源", "base_url": "", "needs_key": True, "enabled": True})

    def add_auto(self, it):
        box = QFrame()
        box.setObjectName("card")
        h = QHBoxLayout(box)
        up, down, rm = QPushButton("↑"), QPushButton("↓"), QPushButton("移除")
        for b in (up, down):
            b.setMaximumWidth(34)
        p = QComboBox()
        for n, x in self.s.get("providers", {}).items():
            p.addItem(x.get("label") or n, n)
        p.setCurrentIndex(max(0, p.findData(it.get("provider"))))
        m = QLineEdit(it.get("model", ""))
        m.setPlaceholderText("模型（留空用預設）")
        tok, runs = QSpinBox(), QSpinBox()
        tok.setRange(0, 100_000_000)
        tok.setSingleStep(10000)
        tok.setSpecialValueText("不限")
        tok.setValue(it.get("max_daily_tokens") or 0)
        runs.setRange(0, 100000)
        runs.setSpecialValueText("不限")
        runs.setValue(it.get("max_daily_runs") or 0)
        u = (self.usage.get("today") or {}).get(it.get("provider"), {"tokens": 0, "runs": 0})
        for w in (up, down, p, m, QLabel("每天最多"), tok, QLabel("tokens、"), runs, QLabel("次"),
                  label(f"今天 {u['tokens']:,} tokens / {u['runs']} 次", "muted"), rm):
            h.addWidget(w)
        row = {"box": box, "p": p, "m": m, "tok": tok, "runs": runs}
        self.auto_rows.append(row)
        self.auto_box.addWidget(box)

        def move(d):
            i = self.auto_rows.index(row)
            j = i + d
            if 0 <= j < len(self.auto_rows):
                self.auto_rows[i], self.auto_rows[j] = self.auto_rows[j], self.auto_rows[i]
                self.auto_box.removeWidget(box)
                self.auto_box.insertWidget(j, box)
                motion.enter(box, dy=-box.height() * d, spring="layout", fade=None)
        up.clicked.connect(lambda: move(-1))
        down.clicked.connect(lambda: move(1))
        rm.clicked.connect(lambda: (self.auto_rows.remove(row), box.deleteLater()))

    def collect(self):
        out = {"providers": {}}
        for path, w in self.fields.items():
            val = w.value() if isinstance(w, QSpinBox) else w.isChecked() if isinstance(w, QCheckBox) else w.text()
            cur = out
            keys = path.split(".")
            for k in keys[:-1]:
                cur = cur.setdefault(k, {})
            cur[keys[-1]] = val
        for n, row in self.prov_rows.items():
            p = dict(self.s.get("providers", {}).get(n, {}))
            p.update(enabled=row["enabled"].isChecked(), label=row["label"].text(), default_model=row["default_model"].currentText())
            for k in ("path", "base_url"):
                if k in row:
                    p[k] = row[k].text()
            if "needs_key" in row:
                p["needs_key"] = row["needs_key"].isChecked()
                if row["new_key"].text().strip():
                    p["new_key"] = row["new_key"].text().strip()
                if row["clear_key"].isChecked():
                    p["clear_key"] = True
            out["providers"][n] = p
        out["readable_paths"] = self.paths.toPlainText().splitlines()
        if self.up_token.text().strip():
            out.setdefault("update", {})["new_token"] = self.up_token.text().strip()
        out["local_fallback"] = {"enabled": self.lf_on.isChecked(), "provider": self.lf_p.currentData(), "model": self.lf_m.text()}
        out["auto"] = {"cli_max_utilization": self.thr.value() / 100,
                       "order": [{"provider": r["p"].currentData(), "model": r["m"].text(), "max_daily_tokens": r["tok"].value(),
                                  "max_daily_runs": r["runs"].value()} for r in self.auto_rows]}
        return out

    def save(self):
        self.msg.setText("儲存中…")
        old_port = (self.s.get("server") or {}).get("port")
        self.b.post("/api/settings", self.collect(), lambda r: self._saved(r, old_port))

    def _saved(self, r, old_port):
        if not r.get("ok"):
            self.msg.setText(f"<span style='color:{C('bad')}'>{esc(r.get('error'))}</span>")
            return
        self.win.settings = r["settings"]
        self.win.load_providers()
        new_port = r["settings"]["server"]["port"]
        self.load()
        QTimer.singleShot(300, lambda: self.msg.setText(
            f"<span style='color:{C('ok')}'>已儲存</span>" + (f"。port 改成 {new_port}，重新啟動後生效。" if new_port != old_port else "")))


# ---------------------------------------------------------------- 主視窗
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("自動化工作流")
        self.resize(1320, 900)
        self.backend = Backend()
        self.wf, self.prov, self.settings, self.skills, self.usage, self.wfs = {}, {}, {}, [], {}, []
        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(20, 16, 20, 12)
        head = QHBoxLayout()
        t = label("自動化工作流", size=22, bold=True, wrap=False)
        head.addWidget(t)
        head.addStretch()
        self.model_btn = QToolButton()
        self.model_btn.setText("模型狀態")
        self.model_btn.setPopupMode(QToolButton.InstantPopup)
        self.model_menu = QMenu(self)
        self.model_btn.setMenu(self.model_menu)
        head.addWidget(self.model_btn)
        v.addLayout(head)
        self.summary = label("讀取中…")
        v.addWidget(self.summary)
        self.update_bar = QLabel()
        self.update_bar.setTextFormat(Qt.RichText)
        self.update_bar.setObjectName("updatebar")
        self.update_bar.linkActivated.connect(self.on_update_link)
        self.update_bar.hide()
        v.addWidget(self.update_bar)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.wf_tab, self.skills_tab, self.settings_tab = WorkflowTab(self), SkillsTab(self), SettingsTab(self)
        self.tabs.addTab(self.wf_tab, "工作流")
        self.tabs.addTab(self.skills_tab, "Skills")
        self.tabs.addTab(self.settings_tab, "設定")
        self.tabs.currentChanged.connect(self.on_tab)
        v.addWidget(self.tabs, 1)
        self.tab_line = theme.TabUnderline(self.tabs, motion.S["layout"])
        self.setCentralWidget(root)
        self.toast_label = QLabel(self)
        self.toast_label.hide()
        self.load_providers()
        self.backend.get("/api/settings", lambda s: setattr(self, "settings", s or {}))
        self.backend.get("/api/skills", lambda s: setattr(self, "skills", s or []))
        self.refresh()
        self.timer = QTimer(self, interval=3000, timeout=self.refresh)
        self.timer.start()
        self.live_timer = QTimer(self, interval=700, timeout=self.poll_live)
        self.live_timer.start()
        QTimer(self, interval=30000, timeout=self.load_providers).start()
        self.upd = {}
        QTimer(self, interval=60000, timeout=self.load_update).start()
        QTimer.singleShot(4000, self.load_update)

    # 名稱
    def prov_label(self, n):
        return (self.prov.get(n) or {}).get("label") or {"auto": "自動"}.get(n, n)

    def model_name(self, mid, provider):
        if provider == "auto":
            return "自動模式"
        mid = mid or (self.prov.get(provider) or {}).get("default_model") or ""
        m = re.match(r"^([a-z]+)([\d.]*)[-_]?(\d+(?:\.\d+)?b)?", mid, re.I)
        name = "DeepSeek V3" if mid.startswith("deepseek-chat") else (
            (m.group(1).capitalize() + (m.group(2) or "") + (" " + m.group(3).upper() if m.group(3) else "")) if m else mid)
        return f"{name}（{self.prov_label(provider)}）" if name else self.prov_label(provider)

    def wf_title(self, n):
        if n in self.wf:
            return self.wf[n]["title"]
        return f"試用：{n[6:]}" if str(n).startswith("skill:") else str(n)

    def first_provider(self):
        return next((p for p in ("claude", "lmstudio", "deepseek") if (self.prov.get(p) or {}).get("ok")), "lmstudio")

    # 資料
    def load_providers(self):
        self.backend.get("/api/providers", self._providers)
        self.backend.get("/api/usage", lambda u: setattr(self, "usage", u or {}))

    def _providers(self, p):
        self.prov = p or {}
        self.render_summary()

    def refresh(self):
        self.backend.get("/api/workflows", lambda wfs: self.backend.get("/api/runs", lambda runs: self._data(wfs or [], runs or [])))

    def _data(self, wfs, runs):
        self.wfs, self.wf, self.runs = wfs, {w["name"]: w for w in wfs}, runs
        if self.wf_tab.sel_wf and self.wf_tab.sel_wf not in self.wf:
            self.wf_tab.sel_wf = None
        self.render_summary()
        self.wf_tab.render(wfs, runs)

    def poll_live(self):
        def got(live):
            self.wf_tab.render_live(live or [])
            # 有工作在跑才密集地問；沒有就放慢，閒置時幾乎不花 CPU
            self.live_timer.setInterval(700 if live else 2500)
        self.backend.get("/api/live", got)

    def quota(self, n):
        q = (self.usage.get("cli_limits") or {}).get(n)
        if q and q.get("resetsAt", 0) > time.time() and q.get("utilization") is not None:
            return f"（{ {'five_hour': '5 小時', 'seven_day': '7 天'}.get(q.get('rateLimitType'), '') }額度 {round(q['utilization'] * 100)}%）"
        return ""

    def render_summary(self):
        runs = getattr(self, "runs", [])
        today = datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
        t = [r for r in runs if r["started"] >= today]
        ok, bad = sum(r["status"] == "success" for r in t), sum(r["status"] == "failed" for r in t)
        running = [w["title"] for w in self.wfs if w.get("running")]
        nxt = sorted([w for w in self.wfs if w.get("next_due") and w.get("enabled")], key=lambda w: w["next_due"])
        usable = [n for n, v in self.prov.items() if v.get("ok")]
        parts = [f"<b style='color:{C('run')}'>{esc('、'.join(running))}正在執行</b>" if running else "目前沒有工作在跑",
                 f"今天跑了 <b>{len(t)}</b> 次" + (f"（成功 {ok}{f'、<span style=\"color:{C('bad')}\">失敗 {bad}</span>' if bad else ''}）" if t else "")]
        if nxt:
            parts.append(f"下一個是 <b>{when(nxt[0]['next_due'])}</b> 的「{esc(nxt[0]['title'])}」")
        parts.append("可用模型：" + "、".join(f"<span style='color:{C('warn') if re.search(r'9\d%|100%', self.quota(n)) else C('ok')}'>"
                                             f"{esc(self.prov_label(n))}{self.quota(n)}</span>" for n in usable)
                     if usable else f"<b style='color:{C('bad')}'>還沒有可用的模型</b>：到「設定 → 模型來源」準備一個")
        self.summary.setText("。".join(parts) + "。")
        self.summary.setTextFormat(Qt.RichText)
        # 模型狀態選單：不能用的收在這裡
        off = [n for n, v in self.prov.items() if not v.get("ok")]
        self.model_btn.setText(f"模型狀態{f'（{len(off)} 個不能用）' if off else ''}")
        m = self.model_menu
        m.clear()
        for title, items in (("可用", usable), ("離線或還不能用", [n for n in off if not self.prov[n].get("missing")]),
                             ("還沒安裝", [n for n in off if self.prov[n].get("missing")])):
            if not items:
                continue
            m.addSection(title)
            for n in items:
                v = self.prov[n]
                a = m.addAction(("● " if v.get("ok") else "○ ") + self.prov_label(n) + self.quota(n))
                why = "" if v.get("ok") else ("怎麼安裝：" + " / ".join(v.get("install") or []) + "；" + v.get("login", "")) if v.get("missing") else v.get("error", "")
                if why:
                    a.setToolTip(why)
                    sub = m.addAction("　" + why[:90])
                    sub.setEnabled(False)
        m.addSeparator()
        m.addAction("到設定管理模型來源…", lambda: self.tabs.setCurrentIndex(2))

    def on_tab(self, i):
        old = self.tabs.widget(getattr(self, "_prev_tab", 0))
        self._prev_tab = i
        if old is not None and old is not self.tabs.widget(i):
            motion.swap(self.tabs.widget(i), lambda: None, dy_out=-6, before=old.grab(),
                        enter_after=i == 0)                  # 先離場再進場（配方 §23）；設定、Skills 由裡面的錯開進場接手
        if i == 1:
            self.skills_tab.load(animate=True)
        elif i == 2:
            self.settings_tab.load()

    # 更新
    def load_update(self):
        self.backend.get("/api/update", self._update_status)

    def _update_status(self, st):
        self.upd = st or {}
        if self.upd.get("update_available"):
            v = self.upd["latest"]["version"]
            act = "<a href='install' style='color:#1e1e3c'>立即更新</a>　" if self.upd.get("can_install") else ""
            self.update_bar.setText(f"有新版本 <b>{esc(v)}</b>（目前 {esc(self.upd['current'])}）　{act}"
                                    f"<a href='notes' style='color:#1e1e3c'>看更新內容</a>")
            if not self.update_bar.isVisible():
                self.update_bar.show()
                slide_in(self.update_bar)
        else:
            self.update_bar.hide()
        self.settings_tab.show_update(self.upd)

    def on_update_link(self, link):
        if link == "notes":
            webbrowser.open((self.upd.get("latest") or {}).get("url") or "")
        else:
            self.start_update()

    def start_update(self):
        v = (self.upd.get("latest") or {}).get("version", "")
        if QMessageBox.question(self, "更新", f"下載並安裝 {v}？\n下載完會請你重新啟動來換上新版本。") != QMessageBox.Yes:
            return
        self.toast("下載新版本中…", 4000)
        prog = QTimer(self, interval=500, timeout=self.load_update)
        prog.start()
        self.backend.post("/api/update/download", {}, lambda r: (prog.stop(), self._downloaded(r)))

    def _downloaded(self, r):
        self.load_update()
        if r.get("error"):
            QMessageBox.warning(self, "更新失敗", r["error"])
            return
        if QMessageBox.question(self, "更新", "新版本已經下載好。現在重新啟動來換上新版本？\n（正在執行的工作流會被中斷）") == QMessageBox.Yes:
            self.backend.post("/api/update/restart", {})

    # 其他
    def open_editor(self, w, pre=None):
        EditorDialog(self, w, pre).exec()

    def toast(self, msg, ms=2600):
        t = self.toast_label
        t.setText(msg)
        t.setObjectName("toast")
        t.adjustSize()
        t.move((self.width() - t.width()) // 2, self.height() - t.height() - 24)
        t.show()
        t.raise_()
        motion.enter(t, floating=True)
        QTimer.singleShot(ms, lambda: motion.leave(t, done=t.hide))

    def export(self, d, fmt, with_steps):
        if not d:
            return
        r = d["run"]
        title = self.wf_title(r["workflow"])
        started = datetime.datetime.fromtimestamp(r["started"])
        st = STATUS.get(r["status"], (r["status"],))[0]
        meta = (f"{started:%Y/%m/%d %H:%M} {TRIGGER.get(r['trigger'], '手動')}執行 · {st}"
                + (f" · 花了 {dur(r['finished'] - r['started'])}" if r["finished"] else "") + f" · {self.model_name(r['model'], r['provider'])}")
        body = r["output"] or (f"**沒有完成：** {human_error(r['error'])}" if r["error"] else "（沒有產出）")
        md = f"# {title}\n\n{meta}\n\n---\n\n{body}\n"
        if with_steps:
            lines = []
            for s in d["steps"]:
                if s["kind"] == "tool":
                    v_, o = describe(s["name"], s["input"])
                    err, txt = result_summary(s["name"], s["output"])
                    lines.append(f"{len(lines) + 1}. {v_}{'：' + o if o else ''} — {'失敗，' if err else ''}{txt}")
            if lines:
                md += "\n---\n\n## 執行過程\n\n" + "\n".join(lines) + "\n"
        fname = re.sub(r'[\\/:*?"<>|\s]+', "-", title) + f"-{started:%Y%m%d-%H%M}"
        html = ("<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><title>" + esc(title) + "</title><style>"
                "body{font:15px/1.7 -apple-system,'PingFang TC','Noto Sans TC','Microsoft JhengHei',sans-serif;max-width:780px;margin:40px auto;padding:0 24px}"
                "table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:6px 10px}pre{background:#f0efea;padding:12px;white-space:pre-wrap}"
                "</style></head><body>" + md_to_html(md) + "</body></html>")
        if fmt in ("md", "txt", "json", "html"):
            content = {"md": md, "html": html, "json": json.dumps(d, ensure_ascii=False, indent=2),
                       "txt": re.sub(r"`([^`]+)`", r"\1", re.sub(r"\*\*([^*]+)\*\*", r"\1", re.sub(r"^#+\s*", "", md, flags=re.M)))}[fmt]
            self._save_bytes(content.encode("utf-8"), f"{fname}.{fmt}")
            return
        self.toast("正在產生 PDF…" if fmt == "pdf" else "正在產生 Word 檔…")
        job = _Job(lambda: server.convert(html, fmt), lambda res, _: self._converted(res, fname))
        self.backend._keep.add(job)
        job.setAutoDelete(False)
        self.backend.pool.start(job)

    def _converted(self, res, fname):
        if isinstance(res, dict):
            QMessageBox.warning(self, "匯出失敗", res.get("json", {}).get("error", ""))
            return
        data, ext = res
        self._save_bytes(data, f"{fname}.{ext}")

    def _save_bytes(self, data, name):
        path, _ = QFileDialog.getSaveFileName(self, "匯出", name)
        if path:
            with open(path, "wb") as f:
                f.write(data)
            self.toast(f"已存到 {path}")


def apply_font(app):
    """明確指定有中文字的字型，不靠 Qt 自己找替代字型（各平台結果不一定一樣）。"""
    f = app.font()
    fams = {"win32": ["Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI"],
            "darwin": [".AppleSystemUIFont", "PingFang TC", "Heiti TC"]}.get(sys.platform,
            ["Noto Sans CJK TC", "Noto Sans TC", "WenQuanYi Micro Hei", "Droid Sans Fallback", "Sans Serif"])
    f.setFamilies(fams + [f.family()])
    app.setFont(f)


def run():
    import threading
    engine.init_db()
    threading.Thread(target=engine.scheduler_loop, daemon=True).start()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("自動化工作流")
    apply_font(app)
    theme.apply(app)
    app._menu_motion = motion.MenuMotion(app)
    app.installEventFilter(app._menu_motion)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())


def self_test(outdir):
    """發佈前檢查用：真的開出主視窗，切過每個分頁、打開編輯表單，確認都有資料、沒有例外，並存下截圖。
    CI 用 QT_QPA_PLATFORM=offscreen 跑，不需要螢幕。成功回傳 0。"""
    import os
    import threading
    import traceback
    os.makedirs(outdir, exist_ok=True)
    errors = []
    sys.excepthook = lambda *e: errors.append("".join(traceback.format_exception(*e)))   # 畫面裡的例外也要算失敗
    engine.init_db()
    threading.Thread(target=engine.scheduler_loop, daemon=True).start()
    app = QApplication.instance() or QApplication(sys.argv)
    apply_font(app)
    theme.apply(app)
    app._menu_motion = motion.MenuMotion(app)
    app.installEventFilter(app._menu_motion)
    w = MainWindow()
    w.resize(1320, 880)
    w.show()
    checks = {}
    shots = []

    def shot(name):
        path = os.path.join(outdir, f"{name}.png")
        w.grab().save(path)
        shots.append(path)

    def step1():
        checks["摘要有載入"] = "讀取中" not in w.summary.text() and "今天跑了" in w.summary.text()
        checks["工作流選單有項目"] = w.wf_tab.combo.count() >= 4
        checks["開始使用有顯示"] = "開始使用" in w.wf_tab.view.toPlainText()
        shot("1-workflows")
        w.tabs.setCurrentIndex(1)
        QTimer.singleShot(1500, step2)

    def step2():
        checks["skill 清單有項目"] = w.skills_tab.list.count() >= 10
        shot("2-skills")
        w.tabs.setCurrentIndex(2)
        QTimer.singleShot(1800, step3)

    def step3():
        checks["設定頁有建好"] = bool(w.settings_tab.prov_rows) and bool(w.settings_tab.fields)
        shot("3-settings")
        w.tabs.setCurrentIndex(0)
        d = EditorDialog(w, None)
        d.resize(760, 820)
        d.show()
        QTimer.singleShot(500, lambda: step4(d))

    def step4(d):
        checks["編輯表單有模型選項"] = d.provider.count() >= 2 and len(d.skill_boxes) >= 10
        d.grab().save(os.path.join(outdir, "4-editor.png"))
        d.close()
        app.quit()

    QTimer.singleShot(3000, step1)
    QTimer.singleShot(60000, app.quit)                   # 保險：卡住也不能讓 CI 一直等
    app.exec()
    bad = [k for k, ok in checks.items() if not ok]
    for k, ok in checks.items():
        print(f"{'✓' if ok else '✕'} {k}")
    for e in errors:
        print("介面例外：\n" + e)
    print(f"截圖：{len(shots) + 1} 張，在 {outdir}")
    return 1 if bad or errors or len(checks) < 6 else 0
