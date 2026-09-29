"""Qt 版的外觀：跟網頁版（dashboard.html）同一組色票和元件樣式，三個平台長得一樣。"""
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleFactory, QWidget

# 跟 dashboard.html 的 :root 一樣
LIGHT = dict(bg="#f5f4f0", panel="#ffffff", ink="#1c1c1a", soft="#4a4a45", muted="#85857d", line="#e4e2dc",
             ok="#1d7f47", bad="#bf3a2e", run="#5252b8", warn="#a86d12", tint="#f0efea", run_tint="#ececff",
             accent="#CCCCFF", accent_ink="#1e1e3c", elev="#ffffff", model="#CCCCFF", tool="#eb6834")
DARK = dict(bg="#141413", panel="#1d1d1b", ink="#ecebe6", soft="#c4c3bc", muted="#8d8c85", line="#31312d",
            ok="#52c186", bad="#ee6d61", run="#CCCCFF", warn="#e0a84a", tint="#262623", run_tint="#2a2a40",
            accent="#CCCCFF", accent_ink="#1e1e3c", elev="#2b2b28", model="#CCCCFF", tool="#d95926")
T = dict(LIGHT)


def system_is_dark():
    app = QApplication.instance()
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        return app.palette().color(QPalette.Window).lightness() < 128


def apply(app):
    """Fusion + 自訂色盤 + 樣式表：不用各平台預設外觀，三個平台同一個樣子。"""
    T.clear()
    T.update(DARK if system_is_dark() else LIGHT)
    app.setStyle(QStyleFactory.create("Fusion"))
    pal = QPalette()
    for role, key in ((QPalette.Window, "bg"), (QPalette.Base, "panel"), (QPalette.AlternateBase, "tint"),
                      (QPalette.Text, "ink"), (QPalette.WindowText, "ink"), (QPalette.ButtonText, "ink"),
                      (QPalette.Button, "panel"), (QPalette.Highlight, "run_tint"), (QPalette.HighlightedText, "ink"),
                      (QPalette.PlaceholderText, "muted"), (QPalette.ToolTipBase, "elev"), (QPalette.ToolTipText, "ink"),
                      (QPalette.Link, "run")):
        pal.setColor(role, QColor(T[key]))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet())


def stylesheet():
    t = T
    return f"""
    QWidget {{ color:{t['ink']}; }}
    QMainWindow, QDialog, QScrollArea, QScrollArea > QWidget > QWidget {{ background:{t['bg']}; }}
    QToolTip {{ background:{t['elev']}; color:{t['ink']}; border:1px solid {t['line']}; border-radius:6px; padding:4px 8px; }}

    QFrame#panel {{ background:{t['panel']}; border:1px solid {t['line']}; border-radius:12px; }}
    QFrame#card {{ background:{t['panel']}; border:1px solid {t['line']}; border-radius:10px; }}
    QFrame#live {{ background:{t['panel']}; border:1px solid {t['run']}; border-radius:12px; }}
    QFrame#panel QLabel, QFrame#card QLabel, QFrame#live QLabel {{ background:transparent; border:none; }}

    QLabel#h1 {{ font-size:22px; font-weight:600; }}
    QLabel#summary {{ color:{t['soft']}; font-size:15px; }}
    QLabel#section {{ color:{t['muted']}; font-size:13px; font-weight:600; letter-spacing:1px; }}
    QLabel#muted {{ color:{t['muted']}; }}
    QLabel#updatebar {{ background:{t['accent']}; color:{t['accent_ink']}; border-radius:8px; padding:6px 12px; }}
    QLabel#toast {{ background:{t['ink']}; color:{t['bg']}; border-radius:8px; padding:8px 16px; font-size:14px; }}

    QPushButton, QToolButton {{ background:transparent; color:{t['ink']}; border:1px solid {t['line']};
        border-radius:8px; padding:6px 14px; font-size:13.5px; }}
    QPushButton:hover, QToolButton:hover {{ background:{t['tint']}; }}
    QPushButton:pressed, QToolButton:pressed {{ background:{t['line']}; }}
    QPushButton:disabled, QToolButton:disabled {{ color:{t['muted']}; border-color:{t['line']}; }}
    QPushButton[primary="true"], QPushButton:default {{ background:{t['accent']}; color:{t['accent_ink']};
        border:none; font-weight:500; }}
    QPushButton[primary="true"]:hover, QPushButton:default:hover {{ background:#d9d9ff; }}
    QPushButton[primary="true"]:pressed, QPushButton:default:pressed {{ background:#b8b8ee; }}
    QPushButton[primary="true"]:disabled {{ background:{t['tint']}; color:{t['muted']}; }}
    QPushButton[danger="true"] {{ color:{t['bad']}; }}
    QToolButton::menu-indicator {{ image:none; width:0; }}

    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{ background:{t['bg']}; color:{t['ink']};
        border:1px solid {t['line']}; border-radius:8px; padding:5px 8px; selection-background-color:{t['accent']};
        selection-color:{t['accent_ink']}; }}
    QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{ border:1px solid {t['run']}; }}
    QComboBox#picker {{ background:{t['run_tint']}; border:none; font-weight:600; font-size:15px; padding:7px 12px; }}
    QComboBox::drop-down {{ border:none; width:22px; }}
    QComboBox QAbstractItemView {{ background:{t['elev']}; border:1px solid {t['line']}; border-radius:8px;
        padding:4px; selection-background-color:{t['tint']}; selection-color:{t['ink']}; outline:none; }}
    QSpinBox::up-button, QSpinBox::down-button {{ border:none; width:16px; }}
    QCheckBox {{ spacing:8px; }}
    QCheckBox::indicator {{ width:16px; height:16px; border:1px solid {t['line']}; border-radius:4px; background:{t['bg']}; }}
    QCheckBox::indicator:checked {{ background:{t['ok']}; border-color:{t['ok']}; }}

    QMenu {{ background:{t['elev']}; border:1px solid {t['line']}; border-radius:10px; padding:6px; }}
    QMenu::item {{ padding:7px 14px; border-radius:6px; }}
    QMenu::item:selected {{ background:{t['tint']}; color:{t['ink']}; }}
    QMenu::item:disabled {{ color:{t['muted']}; }}
    QMenu::section {{ color:{t['muted']}; font-size:12px; padding:6px 10px 2px; background:transparent; }}
    QMenu::separator {{ height:1px; background:{t['line']}; margin:6px 4px; }}

    QTabWidget::pane {{ border:none; border-top:1px solid {t['line']}; top:-1px; }}
    QTabBar {{ background:transparent; }}
    QTabBar::tab {{ background:transparent; color:{t['soft']}; border:none; padding:8px 16px; font-size:14.5px;
        margin-right:4px; border-top-left-radius:6px; border-top-right-radius:6px; }}
    QTabBar::tab:hover {{ background:{t['tint']}; }}
    QTabBar::tab:selected {{ color:{t['ink']}; font-weight:600; }}

    QListWidget, QTextBrowser {{ background:{t['panel']}; border:1px solid {t['line']}; border-radius:12px; outline:none; }}
    QListWidget::item {{ border:none; }}
    QProgressBar {{ background:{t['tint']}; border:none; border-radius:3px; max-height:5px; }}
    QProgressBar::chunk {{ background:{t['run']}; border-radius:3px; }}
    QSplitter::handle {{ background:transparent; width:14px; }}

    QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
    QScrollBar::handle:vertical {{ background:{t['line']}; border-radius:4px; min-height:30px; }}
    QScrollBar::handle:vertical:hover {{ background:{t['muted']}; }}
    QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px; }}
    QScrollBar::handle:horizontal {{ background:{t['line']}; border-radius:4px; min-width:30px; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background:none; width:0; height:0; }}
    """


def doc_css():
    """內容區（QTextBrowser）的排版：跟網頁版 .result 一樣的層級和間距。"""
    t = T
    return f"""
    body {{ color:{t['ink']}; font-size:15px; line-height:165%; }}
    h1 {{ font-size:20px; margin:10px 0 4px 0; }} h2 {{ font-size:18px; margin:10px 0 4px 0; }}
    h3, h4 {{ font-size:15.5px; margin:10px 0 4px 0; }}
    p {{ margin:6px 0; }} a {{ color:{t['run']}; }}
    code {{ background:{t['tint']}; font-family:Menlo, Consolas, monospace; font-size:13px; }}
    pre {{ background:{t['tint']}; font-family:Menlo, Consolas, monospace; font-size:12.5px; padding:10px; }}
    th {{ background:{t['tint']}; }} td, th {{ padding:6px 10px; border:1px solid {t['line']}; }}
    .section {{ color:{t['muted']}; font-size:13px; font-weight:600; }}
    .muted {{ color:{t['muted']}; }}
    """


# ---------------------------------------------------------------- 清單的樣子
def _motion(p, view, index):
    """列的進場 / 換位置動畫：由 motion.MotionList 算好這一列現在的透明度和位移。"""
    st = getattr(view, "state", None)
    if st:
        op, dy = st(index)
        p.setOpacity(op)
        p.translate(0, dy)


class RunDelegate(QStyledItemDelegate):
    """執行紀錄一列：狀態圓點、標題、細字說明、右邊狀態 + 時間；選中那列底色 + 左側強調色條（跟網頁版一樣）。
    資料放在 item 的 UserRole+1：{title, meta, status_text, status_color, right}"""

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 62)

    def paint(self, p, option, index):
        d = index.data(Qt.UserRole + 1) or {}
        r = option.rect
        p.save()
        _motion(p, self.parent(), index)
        p.setRenderHint(QPainter.Antialiasing)
        if option.state & QStyle.State_Selected:
            p.fillRect(r, QColor(T["tint"]))                 # 左側強調色條由 MotionList 畫（會滑）
        elif option.state & QStyle.State_MouseOver:
            p.fillRect(r, QColor(T["tint"]))
        p.setPen(QPen(QColor(T["line"])))
        p.drawLine(r.left() + 12, r.bottom(), r.right() - 12, r.bottom())
        if d.get("dot"):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(d["dot"]))
            p.drawEllipse(QPoint(r.left() + 20, r.center().y()), 5, 5)
        left = r.left() + (36 if d.get("dot") else 16)
        f = QFont(option.font)
        f.setWeight(QFont.DemiBold)
        right_w = 170
        p.setFont(f)
        p.setPen(QColor(T["ink"]))
        fm = QFontMetrics(f)
        p.drawText(QRect(left, r.top() + 10, r.width() - (left - r.left()) - right_w, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(d.get("title", ""), Qt.ElideRight, r.width() - (left - r.left()) - right_w))
        f2 = QFont(option.font)
        f2.setPointSizeF(max(8.0, option.font.pointSizeF() - 1.5))
        p.setFont(f2)
        p.setPen(QColor(T["muted"]))
        fm2 = QFontMetrics(f2)
        p.drawText(QRect(left, r.top() + 32, r.width() - (left - r.left()) - 16, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   fm2.elidedText(d.get("meta", ""), Qt.ElideRight, r.width() - (left - r.left()) - right_w + 60))
        if d.get("status_text"):
            p.setPen(QColor(d.get("status_color") or T["muted"]))
            p.drawText(QRect(r.right() - right_w, r.top() + 10, right_w - 14, 20), Qt.AlignRight | Qt.AlignVCenter, d["status_text"])
        if d.get("right"):
            p.setPen(QColor(T["muted"]))
            p.drawText(QRect(r.right() - right_w, r.top() + 32, right_w - 14, 18), Qt.AlignRight | Qt.AlignVCenter, d["right"])
        p.restore()


class HeaderDelegate(RunDelegate):
    """清單裡的分組標題（例如「知識型 · 2」）。"""

    def sizeHint(self, option, index):
        if not index.flags() & Qt.ItemIsSelectable:
            return QSize(option.rect.width(), 30)
        return super().sizeHint(option, index)

    def paint(self, p, option, index):
        if index.flags() & Qt.ItemIsSelectable:
            return super().paint(p, option, index)
        p.save()
        _motion(p, self.parent(), index)
        f = QFont(option.font)
        f.setPointSizeF(max(8.0, option.font.pointSizeF() - 2))
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(T["muted"]))
        p.drawText(option.rect.adjusted(16, 6, -8, 0), Qt.AlignLeft | Qt.AlignVCenter, index.data())
        p.restore()


# ---------------------------------------------------------------- 分頁底線（配方 §10：是滑過去的，不是閃過去的）
class TabUnderline(QObject):
    def __init__(self, tabs, curve):
        super().__init__(tabs)
        self.tabs, self.f, self.ms = tabs, curve[0], curve[1]
        self.bar = QWidget(tabs)
        self.bar.setStyleSheet(f"background:{T['run']}; border-radius:1px;")
        self.bar.setFixedHeight(2)
        self.anim = None
        tabs.currentChanged.connect(lambda i: self.move(True))
        tabs.tabBar().installEventFilter(self)
        self.move(False)

    def target(self):
        tb = self.tabs.tabBar()
        r = tb.tabRect(self.tabs.currentIndex())
        return QRect(tb.x() + r.x(), tb.y() + r.bottom() - 1, r.width(), 2)

    def move(self, animate):
        end = self.target()
        self.bar.raise_()
        if not animate or not self.bar.isVisible():
            self.bar.setGeometry(end)
            self.bar.show()
            return
        start = self.bar.geometry()
        if self.anim:
            self.anim.stop()
        self.anim = QVariantAnimation(self)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setDuration(self.ms)
        f = self.f

        def step(v):
            k = f(v)
            self.bar.setGeometry(QRect(round(start.x() + (end.x() - start.x()) * k), end.y(),
                                       round(start.width() + (end.width() - start.width()) * k), 2))
        self.anim.valueChanged.connect(step)
        self.anim.start()

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
            self.move(False)
        return False
