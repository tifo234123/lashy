"""Lashy: smoked liquid glass, luminous edges and shared widget styling."""
from PyQt6.QtGui import QColor, QFont

BLUE_GLOW = QColor('#abbfff')
BLUE_GLOW_DIM = QColor(171, 191, 255, 80)
SURFACE = QColor('#16181d')
SURFACE_RAISED = QColor('#23262d')
BORDER = QColor('#343840')
TEXT_PRIMARY = QColor('#edeef2')
TEXT_SECONDARY = QColor('#a4a9b5')
TEXT_MUTED = QColor('#737a88')
SUCCESS = QColor('#9ad6bb')
WARNING = QColor('#e5c18b')
ERROR = QColor('#f0a4ac')
STATE_IDLE = TEXT_MUTED
STATE_LISTENING = SUCCESS
STATE_THINKING = BLUE_GLOW
STATE_SPEAKING = QColor('#c5b5e8')

def font(size=13, weight=QFont.Weight.Normal):
    f = QFont()
    f.setFamilies(['Inter', 'Segoe UI', 'Noto Sans', 'DejaVu Sans'])
    f.setPointSize(size)
    f.setWeight(weight)
    return f

FONT_LABEL = font(10)
FONT_STATUS = font(10)
FONT_TITLE = font(14, QFont.Weight.DemiBold)
FONT_RESPONSE = font(11)
PANEL_WIDTH, PANEL_HEIGHT, PANEL_RADIUS = 440, 740, 30
CURSOR_RADIUS = 18
ANIM_FAST_MS, ANIM_SLOW_MS, CURSOR_ANIM_MS = 160, 320, 600

PANEL_QSS = """
QWidget { color: #edeef2; font-family: 'Inter', 'Segoe UI', 'Noto Sans'; font-size: 13px; }
QLabel { background: transparent; border: none; }
QLabel#title { font-size: 21px; font-weight: 600; color: #f7f8ff; }
QLabel#campusTitle { font-size: 28px; font-weight: 700; color: #f7f8ff; }
QLabel#campusEdition { font-size: 15px; font-weight: 500; color: #b9c9e7; padding-top: 3px; }
QFrame#unicasLogoCard { background: #ffffff; border: 1px solid rgba(232,240,255,180); border-radius: 13px; }
QLabel#credit { color: #a0aec6; font-size: 10px; padding-top: 1px; }
QLabel#subtitle, QLabel#status { color: #a4a9b5; font-size: 12px; }
QLabel#eyebrow { color: #8991a0; font-size: 10px; font-weight: 600; }
QLabel#hero { color: #f7f8ff; font-size: 28px; font-weight: 500; }
QLabel#hint { color: #a4a9b5; font-size: 12px; }
QLabel#query { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(182,202,255,35),stop:1 rgba(155,174,225,12)); border: 1px solid rgba(220,232,255,42); border-radius: 18px; padding: 12px; color: #e3e6ee; }
QLabel#response { font-size: 14px; color: #dce0e8; padding: 2px; }
QLabel#badge { color: #dae7ff; background: rgba(161,189,255,22); border: 1px solid rgba(211,229,255,48); border-radius: 11px; padding: 5px 10px; font-size: 10px; }
QLineEdit { background: transparent; border: none; padding: 5px 1px; color: #f0f1f5; font-size: 13px; selection-background-color: #455674; }
QScrollArea, QWidget#conversation { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 5px; margin: 0px; }
QScrollBar::handle:vertical { background: rgba(211,225,255,65); min-height: 28px; border-radius: 2px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QComboBox { color: #d8e2f5; background: rgba(210,225,255,12); border: 1px solid rgba(220,233,255,35); border-radius: 11px; padding: 6px 10px; font-size: 11px; }
QComboBox:hover { background: rgba(210,225,255,24); }
QComboBox:focus { border-color: #8294be; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { color: #e5e8ef; background: #22262e; border: 1px solid #454d5d; selection-background-color: #3b475e; padding: 6px; }
QToolTip { color: #edeef2; background: #2c313c; border: 1px solid #454d5d; padding: 6px; }
"""
WAVEFORM_QSS = 'QWidget#waveform { background: transparent; }'
