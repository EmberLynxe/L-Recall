import sys
import threading
import traceback

import wx

from .. import theme
from ..icons import icon
from ..utils import Frame
from ..utils.output import capture


class CallbackStringIO:
    def __init__(self, callback):
        self.callback = callback

    def write(self, s: str):
        sys.__stdout__.write(s)
        self.callback(s)

    def flush(self):
        pass


class TextConsoleFrame(Frame):
    title = 'Text Console'

    def __init__(self, *args, **kwargs):
        self.is_done = False
        self.complete_callback = lambda: ...
        super(TextConsoleFrame, self).__init__(parent=None,
                                               title=self.title,
                                               style=wx.CAPTION | wx.CLOSE_BOX)

        container = wx.BoxSizer()
        hbox = wx.BoxSizer()
        container.Add(hbox, 0, wx.ALL, 20)

        logo_img = wx.Image(icon('icon.png'), wx.BITMAP_TYPE_PNG).Scale(48, 48)
        logo = wx.StaticBitmap(self, bitmap=logo_img.ConvertToBitmap())
        hbox.Add(logo, 0, wx.ALIGN_TOP | wx.RIGHT, border=16)

        self.text_box = text_box = wx.BoxSizer(wx.VERTICAL)
        hbox.Add(text_box)

        title_label = theme.styled_text(self, self.title, theme.TEXT_BRIGHT, size=14, bold=True)
        text_box.Add(title_label, 0, wx.BOTTOM, 8)

        text = self.text = wx.TextCtrl(self, value='', size=(700, 220),
                                       style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        text.SetBackgroundColour(theme.BG_INPUT)
        text.SetForegroundColour(theme.TEXT)
        text.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        text_box.Add(text, 0, wx.BOTTOM, 12)

        self.close_btn = wx.Button(self, label='Close')
        self.close_btn.Show(False)
        self.text_box.Add(self.close_btn, 0, wx.ALIGN_RIGHT)
        self.close_btn.Bind(wx.EVT_BUTTON, lambda *_: self.Destroy())

        self.SetSizerAndFit(container)

        self.Bind(wx.EVT_CLOSE, self.on_close)

        self.Show()
        threading.Thread(target=self.capture, args=args, kwargs=kwargs, daemon=True).start()

    def _update(self, text):
        self.text.AppendText(text)

    def update(self, text):
        wx.CallAfter(self._update, text)

    def on_close(self, _):
        if self.is_done:
            self.Destroy()
        return self.is_done

    def on_complete(self, cb):
        if self.is_done:
            cb()
        else:
            self.complete_callback = cb

    def run(self, *args, **kwargs):
        pass

    def _done(self):
        self.close_btn.Show(True)
        self.complete_callback()
        self.is_done = True
        self.Fit()

    def done(self):
        wx.CallAfter(self._done)

    def capture(self, *args, **kwargs):
        output = CallbackStringIO(self.update)
        with capture(output):
            try:
                self.run(*args, **kwargs)
            except Exception:
                print('An unexpected error occurred! More details follow:')
                print(traceback.format_exc())
        self.done()
