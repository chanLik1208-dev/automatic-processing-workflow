"""Qt 版的動態：跟網頁版（dashboard.html）同一組 token、同一套做法。

效能：不用 QGraphicsOpacityEffect（每一幀都要把整個元件重畫到離屏緩衝）。
進場 / 離場改成「截圖一次 → 疊一層只貼圖的遮罩做淡入淡出和位移 → 結束拿掉」，每一幀只是貼一張圖。
清單的列不是元件，由 MotionList 在繪製時套透明度和位移。"""
import math

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QPoint, QRect, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QListWidget, QMenu, QWidget

import theme


# ---------------------------------------------------------------- token（跟 dashboard.html 的 S / T / E 一樣）
def bake(dv, b):
    """spring(Dv, b) 閉式解；回傳 (f(進度 0–1) → 位置, 毫秒)。時長用 t_settle（errata A2）。"""
    z, w = 1 - b, 2 * math.pi / dv

    def d(t):
        if z < 1:
            wd = w * math.sqrt(1 - z * z)
            return math.exp(-z * w * t) * (-math.cos(wd * t) - (z * w / wd) * math.sin(wd * t))
        return -math.exp(-w * t) * (1 + w * t)
    T, t = 0.0, 0.0
    while t < dv * 6:
        if abs(d(t)) >= 0.005:
            T = t
        t += 0.0005
    T = round(T + 0.0005, 2)
    return (lambda p: 1.0 if p >= 1 else 1 + d(max(p, 0) * T)), int(T * 1000)


def cubic(x1, y1, x2, y2):
    """CSS cubic-bezier → f(進度)。"""
    def bez(t, a, b):
        return 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3

    def f(x):
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        lo, hi = 0.0, 1.0
        for _ in range(24):
            mid = (lo + hi) / 2
            if bez(mid, x1, x2) < x:
                lo = mid
            else:
                hi = mid
        return bez((lo + hi) / 2, y1, y2)
    return f


E_OUT, E_IN = cubic(.25, .1, .35, 1), cubic(.4, 0, 1, 1)
S = {"micro": bake(0.15, 0), "enter": bake(0.3, 0.15), "layout": bake(0.35, 0), "page": bake(0.5, 0.1),
     "menu": bake(0.25, 0.1), "dialog": bake(0.28, 0.1)}
T = {"fade": (250, E_OUT), "fadeFast": (150, E_OUT), "exit": (150, E_IN), "lumin": (130, E_OUT)}


def _run(owner, key, total_ms, frame, done=None):
    """同一個通道只留一條動畫（waapi.md「Interruption」）：新的來就停掉舊的。frame(已過毫秒)。"""
    old = getattr(owner, key, None)
    if old is not None:
        old.stop()
    a = QVariantAnimation(owner)
    a.setStartValue(0.0)
    a.setEndValue(float(total_ms))
    a.setDuration(max(1, int(total_ms)))
    a.valueChanged.connect(lambda v: frame(float(v)))

    def fin():
        if getattr(owner, key, None) is a:
            setattr(owner, key, None)
        frame(float(total_ms))
        if done:
            done()
    a.finished.connect(fin)
    setattr(owner, key, a)
    a.start()
    return a


def _bg_for(w):
    """遮罩底下要補的底色：在卡片裡就是卡片色，不然是視窗底色。"""
    p = w.parentWidget()
    while p is not None:
        if p.objectName() in ("panel", "card", "live"):
            return QColor(theme.T["panel"])
        p = p.parentWidget()
    return QColor(theme.T["bg"])


# ---------------------------------------------------------------- 截圖遮罩
class Ghost(QWidget):
    """蓋在目標元件上：先補底色把真的元件遮住，再用指定的透明度和位移貼上截圖。"""

    def __init__(self, target, floating=False):
        super().__init__(target.parentWidget())
        self.target, self.floating = target, floating
        self.pix, self.opacity, self.dy = None, 0.0, 0.0
        self.bg = None if floating else _bg_for(target)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(target.geometry())
        self.show()
        self.raise_()

    def snap(self):
        self.setGeometry(self.target.geometry())
        was = self.floating and not self.target.isVisible()
        if was:
            self.target.show()
        self.pix = self.target.grab()
        if self.floating:
            self.target.hide()

    def set(self, opacity, dy):
        self.opacity, self.dy = opacity, dy
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if self.bg is not None:
            p.fillRect(self.rect(), self.bg)
        if self.pix is not None and self.opacity > 0.001:
            p.setOpacity(self.opacity)
            p.drawPixmap(QPoint(0, round(self.dy)), self.pix)
        p.end()


def _ghost(w, floating=False):
    g = getattr(w, "_ghost", None)
    if g is None:
        g = w._ghost = Ghost(w, floating)
    return g


def _drop(w):
    g = getattr(w, "_ghost", None)
    if g is not None:
        w._ghost = None
        g.hide()
        g.deleteLater()


def enter(w, delay=0, dy=8, spring="enter", floating=False, fade=T["fade"]):
    """配方 §1（enterEl）：透明度 0→1（250ms ease-out）＋ 從 dy 用彈簧回到原位；可以延遲錯開。
    floating：浮在其他東西上面的元件（toast），動畫期間把真的元件藏起來，不補底色。"""
    if w is None or not w.isVisible() and not floating:
        return
    g = _ghost(w, floating)
    g.set(0.0 if fade else 1.0, dy)
    f, ms = S[spring]
    fade_ms, ease = fade or (1, lambda x: 1.0)          # fade=None：只移動（換位置，不是新東西）

    def start():
        if getattr(w, "_ghost", None) is not g:
            return
        g.snap()

        def frame(t):
            t -= delay
            g.set(ease(t / fade_ms) if t > 0 else 0.0, dy * (1 - f(t / ms)) if t > 0 else dy)

        def done():
            _drop(w)
            if floating:
                w.show()
        _run(w, "_motion", delay + max(fade_ms, ms), frame, done)
    QTimer.singleShot(0, start)                    # 等版面排好再截圖，不然截到的是舊尺寸


def leave(w, dy=4, done=None, floating=True):
    """離場（toast.leave）：150ms ease-in 淡出，只移動一點點；比進場短、距離更短（金律 3）。"""
    if not w.isVisible():
        if done:
            done()
        return
    g = _ghost(w, floating)
    g.snap()
    ms, ease = T["exit"]

    def fin():
        _drop(w)
        if done:
            done()
    _run(w, "_motion", ms, lambda t: g.set(1 - ease(t / ms), dy * ease(t / ms)), fin)


def swap(w, apply, dy_out=-8, dy_in=8, spring="page", before=None, enter_after=True):
    """先離場再進場（配方 §23，詳情切換 / 分頁切換）：舊內容淡出上移 150ms → 換內容 → 新內容淡入。
    enter_after=False：新內容自己有錯開進場（設定、Skills），這裡只做離場，免得靜態截圖把裡面的動畫蓋住。
    中途又換：沿用目前的遮罩，新的 apply 取代舊的（跟網頁版的 swapToken 一樣）。"""
    w._swap_apply = apply
    g = getattr(w, "_ghost", None)
    if g is not None and getattr(w, "_swapping", False):
        return                                      # 還在離場：時間到會用最新的 apply
    start_op = g.opacity if g is not None else 1.0
    if g is None or g.pix is None:
        g = _ghost(w)
        if before is not None:                      # 呼叫的人先截好了（例如分頁切換時的舊分頁）
            g.pix = before
        else:
            g.snap()
    g.set(start_op, 0.0)                            # 第一幀之前就要蓋著舊內容，不然會閃一下空白
    w._swapping = True
    ms, ease = T["exit"]
    f, ms_in = S[spring]
    fade_ms, e_out = T["fade"]

    def frame_out(t):
        k = ease(t / ms)
        g.set(start_op * (1 - k), dy_out * k)

    def entered():
        w._swapping = False
        w._swap_apply()
        if not enter_after:
            _drop(w)
            return
        g.snap()                                    # 內容已換好，截新的
        _run(w, "_motion", max(fade_ms, ms_in),
             lambda t: g.set(e_out(t / fade_ms), dy_in * (1 - f(t / ms_in))), lambda: _drop(w))
    _run(w, "_motion", ms, frame_out, entered)


def window_in(win, dy=8, delay=60, spring="dialog", fade=T["fade"]):
    """對話框 / 選單這種獨立視窗：用視窗透明度和位置（不用截圖）。"""
    f, ms = S[spring]
    fade_ms, ease = fade
    end = win.pos()
    win.setWindowOpacity(0.0)

    def frame(t):
        t -= delay
        win.setWindowOpacity(ease(t / fade_ms) if t > 0 else 0.0)
        win.move(end + QPoint(0, round(dy * (1 - f(t / ms)) if t > 0 else dy)))
    _run(win, "_motion", delay + max(fade_ms, ms), frame)


class MenuMotion(QObject):
    """所有 QMenu 打開時：淡入 150ms ＋ 從上方 4px 用 menu 彈簧落下（openMenu）。"""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Show and isinstance(obj, QMenu):
            QTimer.singleShot(0, lambda: obj.isVisible() and window_in(obj, dy=-4, delay=0, spring="menu",
                                                                       fade=T["fadeFast"]))
        return False


# ---------------------------------------------------------------- 清單
class MotionList(QListWidget):
    """列的進場錯開（stagger）、同一串更新時舊列滑到新位置（FLIP）、選取條滑過去（placeSelInd）。
    列本身由 theme 的 delegate 畫，這裡只提供每一列當下的透明度和位移。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.clock = QElapsedTimer()
        self.clock.start()
        self.rows = {}                               # key -> (開始時間, 延遲, 種類, 位移)
        self.ind = None                              # 選取條：(開始時間, 起始偏移)
        self.ticker = QTimer(self, interval=16, timeout=self._tick)
        self.currentItemChanged.connect(self._sel_changed)

    @staticmethod
    def key(item_or_index):
        return item_or_index.data(Qt.UserRole)

    def tops(self):
        self.doItemsLayout()
        return {self.key(self.item(i)): self.visualItemRect(self.item(i)).top() for i in range(self.count())}

    def stagger(self):
        """換了篩選 / 第一次載入：整串由上往下錯開進場，間隔 40ms，前 12 列（interval × count ≤ 0.5s）。"""
        now, n = self.clock.elapsed(), 0
        for i in range(self.count()):
            it = self.item(i)
            if n >= 12:
                break
            self.rows[self.key(it) if it.flags() & Qt.ItemIsSelectable else ("h", i)] = (now, 60 + n * 40, "enter", 8)
            n += 1
        self.ind = None
        self.ticker.start()

    def flip(self, before):
        """同一串更新：新列從上方進場，舊列從原位置滑到新位置。"""
        now = self.clock.elapsed()
        for k, y in self.tops().items():
            if k not in before:
                self.rows[k] = (now, 0, "enter", -8)
            elif abs(before[k] - y) > 1:
                self.rows[k] = (now, 0, "move", before[k] - y)
        if self.rows:
            self.ticker.start()

    def state(self, index):
        k = self.key(index)
        a = self.rows.get(k if k is not None else ("h", index.row()))
        if not a:
            return 1.0, 0.0
        t0, delay, kind, dy = a
        t = self.clock.elapsed() - t0 - delay
        if kind == "enter":
            f, ms = S["enter"]
            return (E_OUT(t / 250) if t > 0 else 0.0), (dy * (1 - f(t / ms)) if t > 0 else dy)
        f, ms = S["layout"]
        return 1.0, dy * (1 - f(t / ms)) if t > 0 else dy

    def _sel_changed(self, cur, prev):
        if cur is None or prev is None or self.signalsBlocked():
            return
        self.doItemsLayout()
        self.ind = (self.clock.elapsed(), self.visualItemRect(prev).top() - self.visualItemRect(cur).top())
        self.ticker.start()

    def _tick(self):
        now = self.clock.elapsed()
        span = max(S["enter"][1], S["layout"][1], 250)
        self.rows = {k: a for k, a in self.rows.items() if now - a[0] - a[1] < span}
        if self.ind and now - self.ind[0] > S["layout"][1]:
            self.ind = None
        if not self.rows and not self.ind:
            self.ticker.stop()
        self.viewport().update()

    def paintEvent(self, e):
        super().paintEvent(e)
        it = self.currentItem()
        if it is None or not it.isSelected():
            return
        r = self.visualItemRect(it)
        _, dy = self.state(self.indexFromItem(it))
        if self.ind:
            f, ms = S["layout"]
            dy += self.ind[1] * (1 - f((self.clock.elapsed() - self.ind[0]) / ms))
        p = QPainter(self.viewport())
        p.fillRect(QRect(r.left(), round(r.top() + dy), 3, r.height()), QColor(theme.T["run"]))
        p.end()


# ---------------------------------------------------------------- 執行中的圓點
class PulseDot(QWidget):
    """.pulse：10px 圓點，1.2 秒一次，透明度 .35↔1、大小 .85↔1（ease-in-out）。看不到時不跑。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self.k = 0.0
        self.a = QVariantAnimation(self)
        self.a.setStartValue(0.0)
        self.a.setEndValue(1.0)
        self.a.setDuration(1200)
        self.a.setLoopCount(-1)
        self.a.valueChanged.connect(self._v)

    def _v(self, v):
        self.k = (1 - math.cos(2 * math.pi * v)) / 2          # 0 → 1 → 0，兩端緩
        self.update()

    def showEvent(self, e):
        self.a.start()

    def hideEvent(self, e):
        self.a.stop()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setOpacity(0.35 + 0.65 * self.k)
        p.setBrush(QColor(theme.T["run"]))
        r = 5 * (0.85 + 0.15 * self.k)
        p.drawEllipse(self.rect().center() + QPoint(1, 1), r, r)
        p.end()
