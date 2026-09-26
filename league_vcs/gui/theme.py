import wx

# same colours as the web page (app.html :root), so the native bits don't look like a different app
BG = '#0b0f14'
BG_SURFACE = '#11161d'
BG_ELEVATED = '#1d2631'
BG_INPUT = '#0b0f14'
LINE = '#1c242e'

TEXT = '#a3abb5'
TEXT_DIM = '#8a94a0'
TEXT_BRIGHT = '#e6e8eb'

ACCENT = '#f0883e'
ACCENT_HOVER = '#f59d5c'

GREEN = '#4caf7a'
RED = '#ef6670'
YELLOW = '#f2a93b'

BORDER = '#29333f'


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
        btn.SetForegroundColour(BG)
    else:
        btn.SetBackgroundColour(BG_ELEVATED)
        btn.SetForegroundColour(TEXT)
