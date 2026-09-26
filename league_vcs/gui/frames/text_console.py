import sys
import threading
import time
import traceback

import wx
from large_vcs.progress import reporting

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
        container.Add(hbox, 0, wx.ALL, self.FromDIP(20))

        logo_img = wx.Image(icon('icon.png'), wx.BITMAP_TYPE_PNG).Scale(self.FromDIP(48), self.FromDIP(48), wx.IMAGE_QUALITY_HIGH)
        logo = wx.StaticBitmap(self, bitmap=logo_img.ConvertToBitmap())
        hbox.Add(logo, 0, wx.ALIGN_TOP | wx.RIGHT, border=self.FromDIP(16))

        self.text_box = text_box = wx.BoxSizer(wx.VERTICAL)
        hbox.Add(text_box)

        title_label = theme.styled_text(self, self.title, theme.TEXT_BRIGHT, size=14, bold=True)
        text_box.Add(title_label, 0, wx.BOTTOM, self.FromDIP(8))

        text = self.text = wx.TextCtrl(self, value='', size=self.FromDIP(wx.Size(700, 220)),
                                       style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        text.SetBackgroundColour(theme.BG_INPUT)
        text.SetForegroundColour(theme.TEXT)
        text.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        text_box.Add(text, 0, wx.BOTTOM, self.FromDIP(8))

        # same clocks as the main window: running time, and time left once there's a rate to go on
        self.gauge = wx.Gauge(self, range=1000, size=self.FromDIP(wx.Size(700, 8)))
        text_box.Add(self.gauge, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(4))
        self.status = theme.styled_text(self, 'Working · 0:00', theme.TEXT)
        text_box.Add(self.status, 0, wx.BOTTOM, self.FromDIP(12))
        self._started = time.monotonic()
        self._prog = None  # (label, pct text, eta seconds, when)
        self._run_label = None
        self._ticker = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _: self._tick(), self._ticker)
        self._ticker.Start(1000)

        self.close_btn = wx.Button(self, label='Close')
        self.close_btn.Show(False)
        self.text_box.Add(self.close_btn, 0, wx.ALIGN_RIGHT)
        self.close_btn.Bind(wx.EVT_BUTTON, lambda *_: self.Destroy())

        self.SetSizerAndFit(container)

        self.Bind(wx.EVT_CLOSE, self.on_close)

        self.Show()
        threading.Thread(target=self.capture, args=args, kwargs=kwargs, daemon=True).start()

    @staticmethod
    def _clock(s):
        s = max(0, int(round(s)))
        return f'{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}' if s >= 3600 else f'{s // 60}:{s % 60:02d}'

    def _progress(self, done, total, label):
        """any thread"""
        wx.CallAfter(self._set_progress, done, total, label, time.monotonic())

    def _set_progress(self, done, total, label, now):
        if self._run_label != label:
            self._run_label, self._run = label, (now, done)
        start, first = self._run
        pct = min(100.0, done / total * 100) if total else 0.0
        self.gauge.SetValue(int(pct * 10))
        secs, did = now - start, done - first
        eta = secs * (total - done) / did if secs > 2 and did > 0 and done < total else None
        num = f'{int(pct)}%' if total > 1e6 else f'{done:,} / {total:,}'
        self._prog = (label, num, eta, now)
        self._tick()

    def _tick(self):
        if self.is_done:
            return
        now = time.monotonic()
        parts = [f'Working · {self._clock(now - self._started)}']
        if self._prog:
            label, num, eta, at = self._prog
            parts.append(f'{label} · {num}')
            if eta is not None:
                left = eta - (now - at)
                parts.append(f'{self._clock(left)} left' if left > 1 else 'almost done')
        self.status.SetLabel('   '.join(parts))

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
        self._ticker.Stop()
        self.status.SetLabel(f'Finished in {self._clock(time.monotonic() - self._started)}')
        self.close_btn.Show(True)
        self.complete_callback()
        self.is_done = True
        self.Fit()

    def done(self):
        wx.CallAfter(self._done)

    def capture(self, *args, **kwargs):
        output = CallbackStringIO(self.update)
        with capture(output), reporting(self._progress):
            try:
                self.run(*args, **kwargs)
            except Exception:
                print('An unexpected error occurred! More details follow:')
                print(traceback.format_exc())
        self.done()
