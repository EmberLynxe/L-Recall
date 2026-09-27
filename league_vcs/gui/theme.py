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


BORDER = '#29333f'


def apply(window):
    window.SetBackgroundColour(BG)
    window.SetForegroundColour(TEXT)


def styled_text(parent, label, color=TEXT, size=0, bold=False):
    st = wx.StaticText(parent, label=label)
    weight = wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL
    if size:
        st.SetFont(wx.Font(size, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, weight))
    elif bold:
        st.SetFont(st.GetFont().Bold())
    st.SetForegroundColour(color)
    return st


