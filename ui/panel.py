"""Animated floating panel. Existing backend signals and public methods retained.

Run with --demo for a local, simulated conversation (no AI/network requests).
Set LASHY_REDUCED_MOTION=1 to disable decorative/reveal animations.
"""
import math
import os
import sys
from pathlib import Path
from enum import Enum, auto

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, pyqtProperty, QPropertyAnimation, QEasingCurve,
    QRectF, QPointF, QEvent, QSize, QSequentialAnimationGroup, QPauseAnimation,
)
from PyQt6.QtGui import QColor, QPainter, QPen, QLinearGradient, QPainterPath, QRadialGradient, QCursor, QRegion, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QComboBox, QFrame, QLineEdit, QGraphicsOpacityEffect,
    QListView, QStyledItemDelegate, QStyle,
)
from ui.design import *

DEMO = __name__ == '__main__' and '--demo' in sys.argv
if DEMO:
    class _DemoConfig:
        hotkey = 'ctrl+win'
        ollama_model = 'local-model'
        def llm_provider(self):
            return 'gemini'
    cfg = _DemoConfig()
else:
    from config import cfg

REDUCED_MOTION = os.environ.get('LASHY_REDUCED_MOTION') == '1'

class AppState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()


def _hotkey_label():
    return '+'.join(p.strip().capitalize() for p in getattr(cfg, 'hotkey', 'ctrl+win').split('+'))

STATE_LABELS = {
    AppState.IDLE: 'Ready when you are',
    AppState.LISTENING: 'Listening to you…',
    AppState.THINKING: 'Working on it…',
    AppState.SPEAKING: 'Speaking…',
}
STATE_COLORS = {
    AppState.IDLE: STATE_IDLE, AppState.LISTENING: STATE_LISTENING,
    AppState.THINKING: STATE_THINKING, AppState.SPEAKING: STATE_SPEAKING,
}
PROVIDER_LABELS = {
    'claude': 'Claude', 'openai': 'OpenAI', 'gemini': 'Gemini',
    'copilot': 'Copilot', 'ollama': 'Ollama',
}


def _copilot_model_choices():
    try:
        from ai.github_copilot_provider import sorted_model_ids, model_label
        return [(mid, model_label(mid)) for mid in sorted_model_ids()] or [('gpt-4o-mini', 'gpt-4o-mini  (free)')]
    except ImportError:
        return [('gpt-4o-mini', 'gpt-4o-mini  (free)')]



def blend(a, b, t):
    a, b = QColor(a), QColor(b)
    return QColor(*(round(x + (y - x) * t) for x, y in zip(a.getRgb(), b.getRgb())))


def tween(owner, prop, start, end, duration=180, curve=QEasingCurve.Type.OutCubic):
    animation = QPropertyAnimation(owner, prop, owner)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setDuration(0 if REDUCED_MOTION else duration)
    animation.setEasingCurve(curve)
    return animation


class ComposerFrame(QFrame):
    """Paint the focus transition without moving the text or layout."""
    def __init__(self):
        super().__init__()
        self._focus = 0.0
        self._focus_anim = tween(self, b'focusLevel', 0., 0.)

    @pyqtProperty(float)
    def focusLevel(self):
        return self._focus

    @focusLevel.setter
    def focusLevel(self, value):
        self._focus = value
        self.update()

    def set_focused(self, active):
        self._focus_anim.stop()
        self._focus_anim.setStartValue(self._focus)
        self._focus_anim.setEndValue(1. if active else 0.)
        self._focus_anim.start()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        fill = QLinearGradient(r.topLeft(), r.bottomRight())
        fill.setColorAt(0, QColor(194, 215, 255, int(28 + 12 * self._focus)))
        fill.setColorAt(.5, QColor(111, 141, 199, 14))
        fill.setColorAt(1, QColor(183, 168, 230, 22))
        p.setBrush(fill)
        rim = QLinearGradient(r.topLeft(), r.bottomRight())
        rim.setColorAt(0, QColor(233, 244, 255, int(100 + 70 * self._focus)))
        rim.setColorAt(.4, QColor(189, 211, 255, 28))
        rim.setColorAt(1, QColor(202, 190, 255, int(65 + 50 * self._focus)))
        p.setPen(QPen(rim, 1.1))
        p.drawRoundedRect(r, 23, 23)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(240, 247, 255, 12), 1))
        p.drawRoundedRect(r.adjusted(2, 2, -2, -2), 21, 21)
        p.end()


class GlassModelDelegate(QStyledItemDelegate):
    """Large readable choices with a stable selection check and soft hover."""
    def __init__(self, combo):
        super().__init__(combo)
        self.combo = combo

    def sizeHint(self, option, index):
        return QSize(270, 40)

    def paint(self, p, option, index):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(5, 2, -5, -2)
        checked = index.row() == self.combo.currentIndex()
        active = bool(option.state & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver))
        if active or checked:
            fill = QLinearGradient(r.topLeft(), r.bottomRight())
            fill.setColorAt(0, QColor(207, 228, 255, 48 if active else 23))
            fill.setColorAt(1, QColor(167, 161, 233, 26 if active else 12))
            p.setBrush(fill)
            p.setPen(QPen(QColor(225, 238, 255, 60 if active else 24), 1))
            p.drawRoundedRect(r, 12, 12)
        p.setPen(QColor('#f0f5ff' if active or checked else '#bdcbe0'))
        p.setFont(font(9))
        text = p.fontMetrics().elidedText(str(index.data() or ''), Qt.TextElideMode.ElideRight, int(r.width()-48))
        p.drawText(r.adjusted(13, 0, -30, 0), Qt.AlignmentFlag.AlignVCenter, text)
        if checked:
            x, y = r.right()-17, r.center().y()
            p.setPen(QPen(QColor('#d9eaff'), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(x-4,y), QPointF(x-1,y+3))
            p.drawLine(QPointF(x-1,y+3), QPointF(x+5,y-4))
        p.restore()


class ModelCombo(QComboBox):
    """Visible chevron, smooth hover border and a fading native model list."""
    def __init__(self):
        super().__init__()
        self._angle = self._glow = 0.0
        self._arrow_anim = tween(self, b'arrowAngle', 0., 0.)
        self._glow_anim = tween(self, b'glow', 0., 0.)
        self._popup_anim = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet('QComboBox::down-arrow { image: none; }')
        self.setMinimumHeight(34)
        self.setMaxVisibleItems(7)
        view = QListView()
        view.setMouseTracking(True)
        view.setSpacing(1)
        view.setMinimumWidth(286)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        view.setStyleSheet('QListView { background: transparent; border: none; padding: 5px; outline: 0; }')
        self.setView(view)
        self.setItemDelegate(GlassModelDelegate(self))

    @pyqtProperty(float)
    def arrowAngle(self):
        return self._angle

    @arrowAngle.setter
    def arrowAngle(self, value):
        self._angle = value
        self.update()

    @pyqtProperty(float)
    def glow(self):
        return self._glow

    @glow.setter
    def glow(self, value):
        self._glow = value
        self.update()

    def _turn(self, angle):
        self._arrow_anim.stop()
        self._arrow_anim.setStartValue(self._angle)
        self._arrow_anim.setEndValue(angle)
        self._arrow_anim.start()

    def _highlight(self, value):
        self._glow_anim.stop()
        self._glow_anim.setStartValue(self._glow)
        self._glow_anim.setEndValue(value)
        self._glow_anim.start()

    def enterEvent(self, event):
        self._highlight(1.)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._highlight(0.)
        super().leaveEvent(event)

    def showPopup(self):
        super().showPopup()
        popup = self.view().window()
        popup.setStyleSheet("""
            QFrame { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                stop:0 #394960, stop:0.5 #202b40, stop:1 #2d2b43);
                border: 1px solid rgba(218,235,255,100); border-radius: 18px; }
            QListView { background: transparent; border: none; padding: 6px; outline: 0; }
            QScrollBar:vertical { background: transparent; width: 4px; }
            QScrollBar::handle:vertical { background: #71829f; border-radius: 2px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        mask = QPainterPath()
        mask.addRoundedRect(QRectF(popup.rect()), 18, 18)
        popup.setMask(QRegion(mask.toFillPolygon().toPolygon()))
        self._turn(180.)
        if self._popup_anim is None:
            effect = QGraphicsOpacityEffect(self.view())
            self.view().setGraphicsEffect(effect)
            self._popup_anim = tween(effect, b'opacity', .15, 1., 220)
        self._popup_anim.stop()
        self._popup_anim.start()

    def hidePopup(self):
        if self._popup_anim:
            self._popup_anim.stop()
            self.view().graphicsEffect().setOpacity(1.)
        super().hidePopup()
        self._turn(0.)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(1. if self.isEnabled() else .4)
        r = QRectF(self.rect()).adjusted(.8, .8, -.8, -.8)
        fill = QLinearGradient(r.topLeft(), r.bottomRight())
        fill.setColorAt(0, QColor(207, 227, 255, int(34+24*self._glow)))
        fill.setColorAt(.5, QColor(139, 170, 221, 12))
        fill.setColorAt(1, QColor(188, 165, 243, 25))
        rim = QLinearGradient(r.topLeft(), r.bottomRight())
        rim.setColorAt(0, QColor(230, 244, 255, int(100+70*self._glow)))
        rim.setColorAt(.5, QColor(210, 228, 255, 25))
        rim.setColorAt(1, QColor(219, 204, 255, 70))
        p.setBrush(fill)
        p.setPen(QPen(rim, 1))
        p.drawRoundedRect(r, 16, 16)
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(203, 225, 255, 135), 1))
            p.drawRoundedRect(r.adjusted(2, 2, -2, -2), 14, 14)
        p.setFont(self.font())
        p.setPen(QColor('#dde9fb'))
        text = p.fontMetrics().elidedText(self.currentText(), Qt.TextElideMode.ElideRight, self.width()-40)
        p.drawText(r.adjusted(12, 0, -28, 0), Qt.AlignmentFlag.AlignVCenter, text)
        p.translate(self.width()-16, self.height()/2)
        p.rotate(self._angle)
        p.setPen(QPen(QColor('#d8e8ff'), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(-3.5, -1.5), QPointF(0, 2))
        p.drawLine(QPointF(0, 2), QPointF(3.5, -1.5))
        p.end()


class GlassButton(QPushButton):
    """Painted glass highlight, consistent line icons and reversible hover fade."""
    def __init__(self, icon, label, parent=None, primary=False, text=''):
        super().__init__(text, parent)
        self.icon_name, self.primary, self._hover = icon, primary, 0.0
        self._press = 0.0
        self._availability = 1.0
        self.setToolTip(label)
        self.setAccessibleName(label)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(36, 36)
        self.anim = QPropertyAnimation(self, b'hover', self)
        self.anim.setDuration(0 if REDUCED_MOTION else ANIM_FAST_MS)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._press_anim = tween(self, b'pressLevel', 0., 0., 180)
        self._enabled_anim = tween(self, b'availability', 1., 1.)
        self.pressed.connect(lambda: self._press_to(1.))
        self.released.connect(lambda: self._press_to(0.))

    @pyqtProperty(float)
    def pressLevel(self):
        return self._press

    @pressLevel.setter
    def pressLevel(self, value):
        self._press = value
        self.update()

    @pyqtProperty(float)
    def availability(self):
        return self._availability

    @availability.setter
    def availability(self, value):
        self._availability = value
        self.update()

    def _press_to(self, value):
        self._press_anim.stop()
        self._press_anim.setStartValue(self._press)
        self._press_anim.setEndValue(value)
        self._press_anim.setEasingCurve(QEasingCurve.Type.OutCubic if value else QEasingCurve.Type.OutBack)
        self._press_anim.start()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.EnabledChange and hasattr(self, '_enabled_anim'):
            self._enabled_anim.stop()
            self._enabled_anim.setStartValue(self._availability)
            self._enabled_anim.setEndValue(1. if self.isEnabled() else .35)
            self._enabled_anim.start()
            if not self.isEnabled():
                self._press_to(0.)
        super().changeEvent(event)

    @pyqtProperty(float)
    def hover(self):
        return self._hover

    @hover.setter
    def hover(self, value):
        self._hover = value
        self.update()

    def _animate(self, target):
        self.anim.stop()
        self.anim.setStartValue(self._hover)
        self.anim.setEndValue(target)
        self.anim.start()

    def enterEvent(self, event):
        self._animate(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(self._availability)
        p.translate(self.width() / 2, self.height() / 2)
        scale = 1. - .055 * self._press
        p.scale(scale, scale)
        p.translate(-self.width() / 2, -self.height() / 2)
        r = QRectF(self.rect()).adjusted(.7, .7, -.7, -.7)
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        if self.primary:
            g.setColorAt(0, blend('#e0edff', '#ffffff', self._hover))
            g.setColorAt(.45, QColor('#afcbff'))
            g.setColorAt(1, QColor('#a7a5ed'))
            ink = QColor('#1b2333')
        else:
            v = int(65 + 45 * self._hover - 12 * self._press)
            g.setColorAt(0, QColor(206, 223, 255, v))
            g.setColorAt(.48, QColor(155, 184, 232, int(20 + 20 * self._hover)))
            g.setColorAt(1, QColor(179, 167, 230, 25))
            ink = QColor('#e2e7f1')
        p.setBrush(g)
        rim = QLinearGradient(r.topLeft(), r.bottomRight())
        rim.setColorAt(0, QColor(246, 251, 255, int(115 + 65 * self._hover)))
        rim.setColorAt(.5, QColor(213, 230, 255, 25))
        rim.setColorAt(1, QColor(210, 195, 255, 70))
        p.setPen(QPen(rim, 1))
        radius = min(r.width(), r.height()) / 2
        p.drawRoundedRect(r, radius, radius)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 18), 1))
        p.drawRoundedRect(r.adjusted(2, 2, -2, -2), radius - 2, radius - 2)
        if self._hover > .01:
            spot = self.mapFromGlobal(QCursor.pos())
            lens = QRadialGradient(QPointF(spot), max(self.width(), self.height()) * .8)
            lens.setColorAt(0, QColor(255, 255, 255, int(48 * self._hover)))
            lens.setColorAt(1, QColor(230, 240, 255, 0))
            p.setBrush(lens)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), radius, radius)
        if self.hasFocus():
            p.setPen(QPen(QColor('#aabbdf'), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(2, 2, -2, -2), 9, 9)
        p.save()
        p.translate(10 if self.text() else (self.width() - 20) / 2, (self.height() - 20) / 2)
        p.translate(0, self._press)
        p.setPen(QPen(ink, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        n = self.icon_name
        if n == 'stop':
            p.setBrush(ink)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(5, 5, 10, 10), 2, 2)
        elif n == 'send':
            p.drawLine(QPointF(10, 16), QPointF(10, 4))
            p.drawLine(QPointF(10, 4), QPointF(5, 9))
            p.drawLine(QPointF(10, 4), QPointF(15, 9))
        elif n == 'copy':
            p.drawRoundedRect(QRectF(7, 7, 10, 11), 2, 2)
            p.drawLine(QPointF(12, 3), QPointF(3, 3))
            p.drawLine(QPointF(3, 3), QPointF(3, 13))
        elif n == 'check':
            p.drawLine(QPointF(4, 10), QPointF(8, 14))
            p.drawLine(QPointF(8, 14), QPointF(16, 6))
        elif n == 'hide':
            p.drawLine(QPointF(4, 10), QPointF(16, 10))
        elif n == 'mic':
            p.drawRoundedRect(QRectF(7, 2, 6, 10), 3, 3)
            p.drawArc(QRectF(4, 5, 12, 11), 180 * 16, 180 * 16)
            p.drawLine(QPointF(10, 16), QPointF(10, 19))
        elif n in ('muted', 'sound'):
            path = QPainterPath(QPointF(3, 8))
            for x, y in [(6, 8), (10, 4), (10, 16), (6, 12), (3, 12)]:
                path.lineTo(x, y)
            path.closeSubpath()
            p.drawPath(path)
            if n == 'muted':
                p.drawLine(QPointF(14, 8), QPointF(18, 12))
                p.drawLine(QPointF(18, 8), QPointF(14, 12))
            else:
                p.drawArc(QRectF(10, 4, 8, 12), -65 * 16, 130 * 16)
        p.restore()
        if self.text():
            p.setPen(ink)
            p.setFont(font(9))
            p.drawText(QRectF(36, 0, self.width() - 44, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()


class ActivityWidget(QWidget):
    """Quiet moving highlight while thinking; disappears when work completes."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(3)
        self.phase = 0.0
        self.active = False
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._tick)

    def _tick(self):
        self.phase = (self.phase + .012) % 1
        self.update()

    def set_active(self, active):
        self.active = active
        if active and self.isVisible() and not REDUCED_MOTION:
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def showEvent(self, event):
        self.set_active(self.active)
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        if not self.active:
            return
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), 0)
        if REDUCED_MOTION:
            g.setColorAt(0, QColor('#8f9dcb'))
            g.setColorAt(1, QColor('#b5a8cb'))
        else:
            for i in range(21):
                x = i / 20
                glow = (1 + math.cos((x - self.phase) * math.tau)) / 2
                g.setColorAt(x, QColor(165, 180, 231, int(15 + 220 * glow ** 4)))
        p.fillRect(self.rect(), g)
        p.end()


class WaveformWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self._levels = [0.0] * 20
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._decay)

    def set_level(self, rms):
        peak = max(0, min(1, rms * 3))
        self._levels = self._levels[1:] + [peak]
        self.update()

    def _decay(self):
        self._levels = [v * .87 for v in self._levels]
        self.update()

    def start(self):
        if self.isVisible():
            self._timer.start(50)

    def stop(self):
        self._timer.stop()
        self._levels = [0.0] * 20
        self.update()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        self.start()
        super().showEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(STATE_LISTENING)
        for i, level in enumerate(self._levels):
            h = max(3, level * 24)
            x = self.width() / 2 - 78 + i * 8
            p.drawRoundedRect(QRectF(x, (28 - h) / 2, 3, h), 1.5, 1.5)
        p.end()


class ProviderBadge(QLabel):
    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.setObjectName('badge')
        self.set_provider(provider)

    def set_provider(self, provider):
        self.setText(PROVIDER_LABELS.get(provider, provider))


class CompanionPanel(QWidget):
    on_push_to_talk_pressed = pyqtSignal()
    on_push_to_talk_released = pyqtSignal()
    on_text_submitted = pyqtSignal(str)
    on_stop_requested = pyqtSignal()
    on_tts_toggled = pyqtSignal(bool)
    on_model_changed = pyqtSignal(str)
    on_document_dropped = pyqtSignal(str)
    _sig_copilot_code = pyqtSignal(str, str)
    _sig_copilot_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._glass_hover = 0.0
        self._glass_anim = tween(self, b'glassHover', 0., 0., 280)
        self._light_timer = QTimer(self)
        self._light_timer.setInterval(32)
        self._light_timer.timeout.connect(self.update)
        self._state = AppState.IDLE
        self._response_text = self._last_user_query = self._visible_text = ''
        self._tts_enabled = False
        self._drag_pos = None
        self._fades = {}
        self._setup_window()
        self._reveal = QTimer(self)
        self._reveal.setInterval(25)
        self._reveal.timeout.connect(self._advance_reveal)
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self._reset_copy)
        self._build_ui()
        self._position_bottom_right()
        self._sig_copilot_code.connect(self._on_copilot_code)
        self._sig_copilot_error.connect(self._on_copilot_error)

    def _setup_window(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(PANEL_WIDTH, PANEL_HEIGHT)
        self.setObjectName('panel')
        self.setWindowTitle('Lashy')
        self.setStyleSheet(PANEL_QSS)
        self.setAcceptDrops(True)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(29, 29, 29, 25)
        root.setSpacing(14)
        header = QHBoxLayout()
        brand = QVBoxLayout()
        brand.setSpacing(2)
        title = QLabel('Lashy')
        title.setObjectName('title')
        subtitle = QLabel('Your screen companion')
        subtitle.setObjectName('subtitle')
        brand.addWidget(title)
        brand.addWidget(subtitle)
        header.addLayout(brand)
        header.addStretch()
        self._tts_btn = GlassButton('muted', 'Enable spoken responses')
        self._tts_btn.setCheckable(True)
        self._tts_btn.clicked.connect(self._toggle_tts)
        self._copy_btn = GlassButton('copy', 'Copy response')
        self._copy_btn.clicked.connect(self._copy_response)
        self._copy_btn.setEnabled(False)
        self._min_btn = GlassButton('hide', 'Hide panel')
        self._min_btn.clicked.connect(self.hide)
        for b in (self._tts_btn, self._copy_btn, self._min_btn):
            header.addWidget(b)
        root.addLayout(header)
        self._unicas_brand = QWidget()
        campus_row = QHBoxLayout(self._unicas_brand)
        campus_row.setContentsMargins(0, 8, 0, 4)
        campus_row.setSpacing(16)
        edition = QVBoxLayout()
        edition.setSpacing(0)
        edition.addStretch()
        campus_name = QLabel('UNICAS')
        campus_name.setObjectName('campusTitle')
        campus_edition = QLabel('EDITION')
        campus_edition.setObjectName('campusEdition')
        edition.addWidget(campus_name)
        edition.addWidget(campus_edition)
        edition.addStretch()
        campus_row.addLayout(edition)
        campus_row.addStretch()
        logo_card = QFrame()
        logo_card.setObjectName('unicasLogoCard')
        logo_card.setFixedSize(232, 94)
        logo_layout = QVBoxLayout(logo_card)
        logo_layout.setContentsMargins(8, 8, 8, 8)
        self._unicas_logo = QLabel()
        self._unicas_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._unicas_logo.setAccessibleName('Università degli Studi di Cassino e del Lazio Meridionale')
        logo = QPixmap(str(Path(_PROJECT_ROOT) / 'assets' / 'unicas-logo.png'))
        self._unicas_logo.setPixmap(logo.scaled(216, 78, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        logo_layout.addWidget(self._unicas_logo)
        campus_row.addWidget(logo_card)
        root.addWidget(self._unicas_brand)
        self._activity = ActivityWidget()
        root.addWidget(self._activity)
        status_row = QHBoxLayout()
        self._status_dot = QLabel('●')
        self._status_dot.setFixedWidth(12)
        self._status_label = QLabel(STATE_LABELS[AppState.IDLE])
        self._status_label.setObjectName('status')
        status_row.addWidget(self._status_dot)
        status_row.addWidget(self._status_label, 1)
        self._badge = ProviderBadge(cfg.llm_provider())
        status_row.addWidget(self._badge)
        root.addLayout(status_row)
        self._waveform = WaveformWidget()
        self._waveform.hide()
        root.addWidget(self._waveform)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName('conversation')
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(0, 8, 7, 10)
        self._content_layout.setSpacing(18)
        self._empty = QWidget()
        empty_layout = QVBoxLayout(self._empty)
        empty_layout.setContentsMargins(0, 38, 0, 22)
        empty_layout.setSpacing(14)
        for text, name in [('HERE TO HELP', 'eyebrow'), ('What are we\nlooking at?', 'hero'), ('Ask about your screen, or hold\nthe voice button and talk it through.', 'hint')]:
            label = QLabel(text)
            label.setObjectName(name)
            label.setWordWrap(True)
            empty_layout.addWidget(label)
        self._content_layout.addWidget(self._empty)
        self._query_label = QLabel()
        self._query_label.setObjectName('query')
        self._query_label.setTextFormat(Qt.TextFormat.PlainText)
        self._query_label.setWordWrap(True)
        self._query_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._content_layout.addWidget(self._query_label)
        self._answer_heading = QLabel('LASHY')
        self._answer_heading.setObjectName('eyebrow')
        self._content_layout.addWidget(self._answer_heading)
        self._response_label = QLabel()
        self._response_label.setObjectName('response')
        self._response_label.setWordWrap(True)
        self._response_label.setTextFormat(Qt.TextFormat.MarkdownText)
        self._response_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._response_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._content_layout.addWidget(self._response_label)
        self._content_layout.addStretch()
        self._scroll.setWidget(content)
        root.addWidget(self._scroll, 1)
        self._composer = ComposerFrame()
        self._composer.setObjectName('composer')
        composer_layout = QVBoxLayout(self._composer)
        composer_layout.setContentsMargins(12, 9, 9, 9)
        composer_layout.setSpacing(8)
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText('Ask anything about your screen…')
        self._chat_input.setAccessibleName('Message to Lashy')
        self._chat_input.setMinimumHeight(32)
        self._chat_input.installEventFilter(self)
        self._chat_input.returnPressed.connect(self._on_send_clicked)
        self._chat_input.textChanged.connect(self._sync_send)
        composer_layout.addWidget(self._chat_input)
        actions = QHBoxLayout()
        self._ptt_btn = GlassButton('mic', 'Hold to talk', text='Hold to talk')
        self._ptt_btn.setFixedSize(131, 36)
        self._ptt_btn.pressed.connect(self._ptt_pressed)
        self._ptt_btn.released.connect(self._ptt_released)
        self._ptt_active = False
        actions.addWidget(self._ptt_btn)
        actions.addStretch()
        self._send_btn = GlassButton('send', 'Send message', primary=True)
        self._send_btn.clicked.connect(self._on_send_clicked)
        actions.addWidget(self._send_btn)
        composer_layout.addLayout(actions)
        root.addWidget(self._composer)
        footer = QHBoxLayout()
        footer.setSpacing(10)
        shortcut = QLabel(_hotkey_label())
        shortcut.setObjectName('hint')
        shortcut.setToolTip('Configured voice shortcut')
        footer.addWidget(shortcut)
        footer.addStretch()
        self._model_label = QLabel('Model')
        self._model_label.setObjectName('hint')
        footer.addWidget(self._model_label)
        self._model_combo = ModelCombo()
        self._model_combo.setAccessibleName('AI model')
        self._model_combo.setMinimumWidth(160)
        self._model_combo.setMaximumWidth(220)
        self._model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._model_combo.setMinimumContentsLength(14)
        self._populate_models()
        self._model_combo.currentIndexChanged.connect(self._emit_model)
        footer.addWidget(self._model_combo)
        root.addLayout(footer)
        self._credit = QLabel('Made by Abderrahman ezzin')
        self._credit.setObjectName('credit')
        self._credit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._credit)
        self._entrance_widgets = [title, subtitle, self._tts_btn, self._copy_btn,
                                  self._unicas_brand,
                                  self._min_btn, self._status_dot, self._status_label,
                                  self._badge, self._empty, self._query_label,
                                  self._answer_heading, self._response_label,
                                  self._chat_input, self._ptt_btn, self._send_btn,
                                  shortcut, self._model_label, self._model_combo, self._credit]
        self._render_conversation()
        self.set_state(AppState.IDLE)
        self._chat_input.setFocus()

    def eventFilter(self, obj, event):
        if obj is self._chat_input and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self._composer.set_focused(event.type() == QEvent.Type.FocusIn)
        return super().eventFilter(obj, event)

    def _ptt_pressed(self):
        if self._state != AppState.THINKING:
            self._ptt_active = True
            self.on_push_to_talk_pressed.emit()

    def _ptt_released(self):
        if self._ptt_active:
            self._ptt_active = False
            self.on_push_to_talk_released.emit()

    def _populate_models(self):
        self._set_models_for(cfg.llm_provider())

    def _emit_model(self, *_):
        value = self._model_combo.currentData() or self._model_combo.currentText()
        self._model_combo.setToolTip(value)
        if value:
            self._fade(self._model_combo, start=.55)
            self.on_model_changed.emit(value)

    def _set_models_for(self, provider):
        same_provider = getattr(self, '_models_provider', None) == provider
        previous = self._model_combo.currentData() if same_provider else None
        saved = cfg.selected_model(provider) if not DEMO else ''
        self._models_provider = provider
        self._model_combo.blockSignals(True)
        try:
            self._model_combo.clear()
            if DEMO:
                choices = [('demo-fast', 'Preview · Fast'), ('demo-detailed', 'Preview · Detailed')]
            elif provider == 'copilot':
                choices = _copilot_model_choices()
            elif provider in ('claude', 'openai', 'gemini'):
                try:
                    from ai.model_registry import cached_models
                    choices = [(m['id'], m['id'] + ('' if m.get('vision') else '  (no vision)')) for m in cached_models(provider)]
                except (ImportError, KeyError, TypeError):
                    choices = []
                choices = choices or [('default', 'Default model')]
            else:
                choices = [(cfg.ollama_model, cfg.ollama_model)]
            # Cached lists can lag behind the user's available models. Retain
            # the explicit choice instead of silently replacing it on refresh.
            if saved and saved not in {mid for mid, _ in choices}:
                choices.insert(0, (saved, saved))
            for mid, label in choices:
                self._model_combo.addItem(label, mid)
            index = self._model_combo.findData(previous or saved)
            if index < 0 and provider == 'gemini':
                index = self._model_combo.findData('gemini-3-flash-preview')
            self._model_combo.setCurrentIndex(max(0, index))
        finally:
            self._model_combo.blockSignals(False)
        self._emit_model()

    def refresh_for_provider(self, provider):
        self._badge.set_provider(provider)
        self._fade(self._badge)
        self._set_models_for(provider)

    def _position_bottom_right(self):
        screen = QApplication.screenAt(self.cursor().pos()) or QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.move(max(area.left(), area.right() - self.width() - 23), max(area.top(), area.bottom() - self.height() - 23))

    def set_state(self, state):
        self._state = state
        self._status_dot.setStyleSheet('color: ' + STATE_COLORS[state].name() + '; font-size: 10px;')
        self.set_status_text(STATE_LABELS[state])
        self._fade(self._status_dot, start=.25)
        self._activity.set_active(state == AppState.THINKING)
        self._waveform.setVisible(state == AppState.LISTENING)
        if state == AppState.LISTENING:
            self._waveform.start()
            self._fade(self._waveform)
        else:
            self._waveform.stop()
        self._ptt_btn.setEnabled(state != AppState.THINKING)
        self._model_combo.setEnabled(state != AppState.THINKING)
        self._tts_btn.setEnabled(state == AppState.IDLE or self._tts_enabled)
        self._sync_send()

    def set_status_text(self, text):
        changed = self._status_label.text() != text
        self._status_label.setText(text)
        if changed:
            self._fade(self._status_label, start=.35)

    def set_audio_level(self, rms):
        self._waveform.set_level(rms)

    def _sync_send(self, *_):
        busy = self._state != AppState.IDLE
        self._send_btn.icon_name = 'stop' if busy else 'send'
        self._send_btn.setToolTip('Stop response' if busy else 'Send message')
        self._send_btn.setAccessibleName('Stop response' if busy else 'Send message')
        self._send_btn.setEnabled(busy or bool(self._chat_input.text().strip()))
        self._send_btn.update()

    def _on_send_clicked(self):
        if self._state != AppState.IDLE:
            self._send_btn.setEnabled(False)
            self.set_status_text('Stopping…')
            self.on_stop_requested.emit()
            return
        text = self._chat_input.text().strip()
        if not text:
            return
        self.clear_response()
        self._last_user_query = text
        self._chat_input.clear()
        self.set_state(AppState.THINKING)
        self._render_conversation()
        QTimer.singleShot(0, self._scroll_to_bottom)
        self._fade(self._query_label, duration=280)
        self.on_text_submitted.emit(text)

    def update_response(self, text):
        if not text.startswith(self._visible_text):
            self._visible_text = ''
        self._response_text = text
        self._queue_reveal()

    def append_response_chunk(self, chunk):
        self._response_text += chunk
        self._queue_reveal()

    def _queue_reveal(self):
        self._copy_btn.setEnabled(bool(self._response_text))
        if REDUCED_MOTION or not self.isVisible() or self._visible_text == self._response_text:
            self._reveal.stop()
            self._visible_text = self._response_text
            self._render_conversation()
        elif not self._reveal.isActive():
            self._reveal.start()

    def _advance_reveal(self):
        remaining = len(self._response_text) - len(self._visible_text)
        if remaining <= 0:
            self._reveal.stop()
            return
        # Adaptive catch-up avoids adding a long artificial delay to fast streams.
        first = not self._visible_text
        count = remaining  # network streaming supplies the cadence; avoid a second typing delay
        self._visible_text = self._response_text[:len(self._visible_text) + count]
        self._render_conversation()
        if first:
            self._fade(self._answer_heading)
            self._fade(self._response_label, duration=260)

    def clear_response(self):
        self._reveal.stop()
        self._copy_timer.stop()
        self._reset_copy()
        self._last_user_query = self._response_text = self._visible_text = ''
        self._copy_btn.setEnabled(False)
        self._render_conversation()

    def _render_conversation(self, status=''):
        sb = self._scroll.verticalScrollBar()
        follow = sb.maximum() - sb.value() < 28
        self._empty.setVisible(not (self._last_user_query or self._response_text or status))
        self._query_label.setVisible(bool(self._last_user_query))
        self._query_label.setText(self._last_user_query)
        self._answer_heading.setVisible(bool(self._visible_text or status))
        self._response_label.setVisible(bool(self._visible_text or status))
        self._response_label.setText(self._visible_text or status)
        if follow:
            QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _copy_response(self):
        if self._response_text:
            QApplication.clipboard().setText(self._response_text)
            self._copy_btn.icon_name = 'check'
            self._copy_btn.setToolTip('Copied')
            self._copy_btn.update()
            self._fade(self._copy_btn, start=.45)
            self._copy_timer.start(1600)

    def _reset_copy(self):
        self._copy_btn.icon_name = 'copy'
        self._copy_btn.setToolTip('Copy response')
        self._copy_btn.update()

    def _toggle_tts(self):
        if self._state != AppState.IDLE and not self._tts_enabled:
            self._tts_btn.setChecked(False)
            return
        self._tts_enabled = not self._tts_enabled
        self._tts_btn.setChecked(self._tts_enabled)
        self._tts_btn.icon_name = 'sound' if self._tts_enabled else 'muted'
        label = 'Mute spoken responses' if self._tts_enabled else 'Enable spoken responses'
        self._tts_btn.setToolTip(label)
        self._tts_btn.setAccessibleName(label)
        self._tts_btn.update()
        self._fade(self._tts_btn, start=.6)
        self.on_tts_toggled.emit(self._tts_enabled)
        self._tts_btn.setEnabled(self._state == AppState.IDLE or self._tts_enabled)

    def show_transcript(self, text):
        self.clear_response()
        self._last_user_query = text
        self._render_conversation()

    def show_copilot_code(self, user_code, verification_uri):
        self._sig_copilot_code.emit(user_code, verification_uri)

    def show_copilot_error(self, error):
        self._sig_copilot_error.emit(error)

    def _on_copilot_code(self, user_code, verification_uri):
        self.show()
        self.raise_()
        self.clear_response()
        self._show_immediate(f'GitHub Copilot sign-in\n\n1. Open: {verification_uri}\n\n2. Enter code: {user_code}\n\n3. Click Authorize in GitHub.\n\nLashy will sign in automatically once you authorize.')
        self.set_status_text('Waiting for Copilot authorization…')

    def _on_copilot_error(self, error):
        self.clear_response()
        self._show_immediate(f'Copilot login failed:\n\n{error}')
        self.set_state(AppState.IDLE)

    def _show_immediate(self, text):
        self._reveal.stop()
        self._response_text = self._visible_text = text
        self._copy_btn.setEnabled(bool(text))
        self._render_conversation()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        for path in paths:
            self.on_document_dropped.emit(path)
        if paths:
            event.acceptProposedAction()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 80:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def _fade(self, widget, start=.05, duration=220, delay=0):
        # Effects are installed only on individual leaves (never nested effects).
        # Reuse one animation per widget, so repeated streams cannot accumulate.
        if not self.isVisible() or REDUCED_MOTION:
            return
        if widget not in self._fades:
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(1.)
            widget.setGraphicsEffect(effect)
            group = QSequentialAnimationGroup(self)
            pause = QPauseAnimation(0, group)
            animation = QPropertyAnimation(effect, b'opacity', group)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(pause)
            group.addAnimation(animation)
            self._fades[widget] = (effect, group, pause, animation)
        effect, group, pause, animation = self._fades[widget]
        group.stop()
        effect.setOpacity(start)
        pause.setDuration(delay)
        animation.setDuration(duration)
        animation.setStartValue(start)
        animation.setEndValue(1.)
        group.start()

    def showEvent(self, event):
        super().showEvent(event)
        for i, widget in enumerate(self._entrance_widgets):
            if not widget.isHidden():
                self._fade(widget, duration=280, delay=min(i * 18, 180))

    def hideEvent(self, event):
        for effect, group, _, _ in self._fades.values():
            group.stop()
            effect.setOpacity(1.)
        self._ptt_released()
        self._reveal.stop()
        self._visible_text = self._response_text
        self._render_conversation()
        self._light_timer.stop()
        super().hideEvent(event)

    @pyqtProperty(float)
    def glassHover(self):
        return self._glass_hover

    @glassHover.setter
    def glassHover(self, value):
        self._glass_hover = value
        self.update()

    def enterEvent(self, event):
        self._glass_anim.stop()
        self._glass_anim.setStartValue(self._glass_hover)
        self._glass_anim.setEndValue(1.)
        self._glass_anim.start()
        if not REDUCED_MOTION:
            self._light_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._glass_anim.stop()
        self._glass_anim.setStartValue(self._glass_hover)
        self._glass_anim.setEndValue(0.)
        self._glass_anim.start()
        self._light_timer.stop()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(7, 7, -7, -7)
        # Soft perimeter shadow lives inside the transparent window margins.
        p.setBrush(Qt.BrushStyle.NoBrush)
        for spread in range(6, 0, -1):
            p.setPen(QPen(QColor(4, 8, 20, 5 + (6 - spread) * 3), spread * 2))
            p.drawRoundedRect(r.translated(0, 2), PANEL_RADIUS, PANEL_RADIUS)
        shell = QPainterPath()
        shell.addRoundedRect(r, PANEL_RADIUS, PANEL_RADIUS)
        p.save()
        p.setClipPath(shell)
        base = QLinearGradient(r.topLeft(), r.bottomRight())
        base.setColorAt(0, QColor(39, 49, 69, 244))
        base.setColorAt(.45, QColor(20, 27, 42, 242))
        base.setColorAt(1, QColor(28, 28, 47, 246))
        p.fillPath(shell, base)
        # Diffuse lighting suggests depth without compromising text contrast.
        for center, radius, color in [
            (QPointF(55, 20), 310, QColor(150, 202, 255, 48)),
            (QPointF(self.width() + 30, 235), 250, QColor(166, 140, 240, 27)),
            (QPointF(70, self.height() + 50), 270, QColor(99, 166, 236, 24)),
        ]:
            light = QRadialGradient(center, radius)
            light.setColorAt(0, color)
            light.setColorAt(1, QColor(color.red(), color.green(), color.blue(), 0))
            p.fillPath(shell, light)
        # A broad, almost invisible reflection across the upper glass surface.
        sheen = QLinearGradient(0, 0, self.width() * .7, 200)
        sheen.setColorAt(0, QColor(255, 255, 255, 16))
        sheen.setColorAt(.6, QColor(255, 255, 255, 3))
        sheen.setColorAt(1, QColor(255, 255, 255, 0))
        p.fillPath(shell, sheen)
        if self._glass_hover > .001:
            pos = self.mapFromGlobal(QCursor.pos())
            hover_light = QRadialGradient(QPointF(pos), 190)
            hover_light.setColorAt(0, QColor(198, 224, 255, int(24 * self._glass_hover)))
            hover_light.setColorAt(1, QColor(198, 224, 255, 0))
            p.fillPath(shell, hover_light)
        p.restore()
        rim = QLinearGradient(r.topLeft(), r.bottomRight())
        rim.setColorAt(0, QColor(238, 249, 255, 155))
        rim.setColorAt(.22, QColor(202, 222, 255, 54))
        rim.setColorAt(.53, QColor(219, 224, 255, 20))
        rim.setColorAt(.8, QColor(206, 186, 252, 64))
        rim.setColorAt(1, QColor(230, 239, 255, 105))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(rim, 1.1))
        p.drawRoundedRect(r, PANEL_RADIUS, PANEL_RADIUS)
        p.setPen(QPen(QColor(235, 245, 255, 13), 1))
        p.drawRoundedRect(r.adjusted(2, 2, -2, -2), PANEL_RADIUS - 2, PANEL_RADIUS - 2)
        p.end()


if __name__ == '__main__':
    app = QApplication([arg for arg in sys.argv if arg != '--demo'])
    app.setFont(font(10))
    panel = CompanionPanel()
    if DEMO:
        panel.set_status_text('Preview · no AI connected')
        def demo_reply(text):
            answer = 'Here’s a calmer space to think.\n\n**The details make the difference.**\n\nSoft charcoal surfaces, quiet highlights, and controls with room to breathe.\n\nYour real backend can stream its answer straight into this panel.'
            chunks = [answer[i:i+7] for i in range(0, len(answer), 7)]
            panel._demo_timer = QTimer(panel)
            def step():
                if chunks:
                    panel.append_response_chunk(chunks.pop(0))
                else:
                    panel._demo_timer.stop()
                    panel.set_state(AppState.IDLE)
                    panel.set_status_text('Preview · no AI connected')
            panel._demo_timer.timeout.connect(step)
            QTimer.singleShot(900, lambda: panel._demo_timer.start(55))
        panel.on_text_submitted.connect(demo_reply)
        panel.on_push_to_talk_pressed.connect(lambda: panel.set_state(AppState.LISTENING))
        panel.on_push_to_talk_released.connect(lambda: panel.set_state(AppState.IDLE))
    panel.show()
    sys.exit(app.exec())
