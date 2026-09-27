import os

import wx

from .. import theme


class Frame(wx.Frame):
    ICON_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'icons', 'icon.ico'))

    def __init__(self, parent, id=wx.ID_ANY, title="", pos=wx.DefaultPosition,
                 size=wx.DefaultSize, style=wx.DEFAULT_FRAME_STYLE, name='LRecallFrame'):
        super(Frame, self).__init__(parent, id, title, pos, size, style, name)
        # every size from the same logo, so the taskbar doesn't blow up a 32px one on scaled screens
        self.SetIcons(wx.IconBundle(self.ICON_PATH, wx.BITMAP_TYPE_ICO))
        theme.apply(self)


class CallbackFrame(Frame):
    def __init__(self, parent, id=wx.ID_ANY, title="", pos=wx.DefaultPosition,
                 size=wx.DefaultSize, style=wx.DEFAULT_FRAME_STYLE, name='LRecallFrame'):
        super(CallbackFrame, self).__init__(parent, id, title, pos, size, style, name)
        self.close_callback = lambda: None

    def on_close(self, cb):
        self.close_callback = cb

    def Destroy(self):
        self.close_callback()
        return super(CallbackFrame, self).Destroy()

    def Close(self, force=False):
        self.close_callback()
        return super(CallbackFrame, self).Close(force=force)
