import wx

BG = '#1e1e2e'
BG_SURFACE = '#282838'
BG_ELEVATED = '#333345'
BG_INPUT = '#1b1b2b'

TEXT = '#cdd6f4'
TEXT_DIM = '#6c7086'
TEXT_BRIGHT = '#f0e6d2'

ACCENT = '#c8aa6e'
ACCENT_HOVER = '#e0c585'

GREEN = '#a6e3a1'
RED = '#f38ba8'
YELLOW = '#f9e2af'

BORDER = '#45475a'


def apply(window):
    window.SetBackgroundColour(BG)
    window.SetForegroundColour(TEXT)


def apply_surface(control):
    control.SetBackgroundColour(BG_SURFACE)
    control.SetForegroundColour(TEXT)


def styled_text(parent, label, color=TEXT, size=0, bold=False):
    st = wx.StaticText(parent, label=label)
    weight = wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL
    if size:
        st.SetFont(wx.Font(size, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, weight))
    elif bold:
        st.SetFont(st.GetFont().Bold())
    st.SetForegroundColour(color)
    return st


def style_list(ctrl):
    ctrl.SetBackgroundColour(BG_INPUT)
    ctrl.SetForegroundColour(TEXT)
    try:
        ctrl.SetTextColour(TEXT)
    except AttributeError:
        pass


def style_button(btn, accent=False):
    if accent:
        btn.SetBackgroundColour(ACCENT)
        btn.SetForegroundColour('#1e1e2e')
    else:
        btn.SetBackgroundColour(BG_ELEVATED)
        btn.SetForegroundColour(TEXT)
