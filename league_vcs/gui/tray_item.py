import os

import wx
import wx.adv

from . import theme

# windows' own icon fonts. fluent on 11, mdl2 on 10, same codepoints for the ones we use
ICON_FONTS = ('Segoe Fluent Icons', 'Segoe MDL2 Assets')
ICONS = {'update': '', 'replays': '', 'watch': '', 'scan': '',
         'settings': '', 'exit': ''}


class TrayMenu(wx.PopupTransientWindow):
    """the right-click menu, drawn to match the app instead of windows' grey one.
    items are (label, func, icon, accent) or None for a divider. func None = greyed out"""

    def __init__(self, parent, items, title, subtitle, status):
        super().__init__(parent, wx.BORDER_NONE)
        self.items, self.title, self.subtitle, self.status = items, title, subtitle, status
        self.hover = -1
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        d = self.FromDIP
        self.row_h, self.sep_h, self.head_h = d(30), d(9), d(54 if status else 36)
        self.width = d(250)
        self.rows = []  # (top, index)
        y = self.head_h + d(4)
        for i, item in enumerate(items):
            self.rows.append((y, i))
            y += self.sep_h if item is None else self.row_h
        self.SetSize(self.width, y + d(5))
        base = wx.Font(wx.FontInfo(9.5).FaceName('Segoe UI'))
        self.font, self.font_bold = base, base.Bold()
        self.font_title = wx.Font(wx.FontInfo(10.5).FaceName('Segoe UI').Bold())
        self.font_small = wx.Font(wx.FontInfo(8.5).FaceName('Segoe UI'))
        face = next((f for f in ICON_FONTS if wx.FontEnumerator.IsValidFacename(f)), None)
        self.font_icon = wx.Font(wx.FontInfo(10).FaceName(face)) if face else None
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_MOTION, self._motion)
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda _: self._set_hover(-1))
        self.Bind(wx.EVT_LEFT_UP, self._click)
        self.Bind(wx.EVT_CHAR_HOOK, self._key)

    def _row_at(self, y):
        for top, i in self.rows:
            item = self.items[i]
            h = self.sep_h if item is None else self.row_h
            if top <= y < top + h:
                return i if item is not None and item[1] is not None else -1
        return -1

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.Refresh()

    def _motion(self, evt):
        self._set_hover(self._row_at(evt.GetY()))

    def _click(self, evt):
        i = self._row_at(evt.GetY())
        if i >= 0:
            self._run(i)

    def _run(self, i):
        func = self.items[i][1]
        self.Dismiss()
        wx.CallAfter(func)

    def _key(self, evt):
        code = evt.GetKeyCode()
        live = [i for i, it in enumerate(self.items) if it is not None and it[1] is not None]
        if code == wx.WXK_ESCAPE:
            self.Dismiss()
        elif code in (wx.WXK_DOWN, wx.WXK_UP) and live:
            pos = live.index(self.hover) if self.hover in live else -1
            pos = (pos + (1 if code == wx.WXK_DOWN else -1)) % len(live)
            self._set_hover(live[pos])
        elif code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self.hover >= 0:
            self._run(self.hover)
        else:
            evt.Skip()

    def _paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        d = self.FromDIP
        gc.SetBrush(wx.Brush(theme.BG_SURFACE))
        gc.SetPen(wx.Pen(theme.BORDER))
        gc.DrawRectangle(0, 0, w - 1, h - 1)
        # header: name, version, and what it's up to
        gc.SetFont(self.font_title, theme.TEXT_BRIGHT)
        gc.DrawText(self.title, d(14), d(10))
        tw = gc.GetTextExtent(self.title)[0]
        gc.SetFont(self.font_small, theme.TEXT_DIM)
        gc.DrawText(self.subtitle, d(14) + tw + d(6), d(12))
        if self.status:
            gc.DrawText(self.status, d(14), d(31))
        gc.SetPen(wx.Pen(theme.LINE))
        gc.StrokeLine(0, self.head_h, w, self.head_h)
        for top, i in self.rows:
            item = self.items[i]
            if item is None:
                gc.StrokeLine(d(10), top + self.sep_h // 2, w - d(10), top + self.sep_h // 2)
                continue
            label, func, icon, accent = item
            if i == self.hover:
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.SetBrush(wx.Brush(theme.BG_ELEVATED))
                gc.DrawRectangle(d(4), top + d(1), w - d(8), self.row_h - d(2))
                gc.SetBrush(wx.Brush(theme.ACCENT))
                gc.DrawRectangle(d(4), top + d(1), d(2), self.row_h - d(2))
                gc.SetPen(wx.Pen(theme.LINE))
            colour = theme.TEXT_DIM if func is None else theme.ACCENT if accent else theme.TEXT_BRIGHT
            if self.font_icon and icon:
                gc.SetFont(self.font_icon, theme.ACCENT if accent else theme.TEXT_DIM)
                ih = gc.GetTextExtent(ICONS[icon])[1]
                gc.DrawText(ICONS[icon], d(16), top + (self.row_h - ih) / 2)
            gc.SetFont(self.font_bold if accent else self.font, colour)
            th = gc.GetTextExtent(label)[1]
            gc.DrawText(label, d(44) if self.font_icon else d(16), top + (self.row_h - th) / 2)


class TrayItem(wx.adv.TaskBarIcon):
    ICON_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), 'icons', 'small-icon.png'))
    TOOLTIP = 'L-Recall'

    def __init__(self, menu_options, on_click=None, subtitle='', status=None):
        wx.adv.TaskBarIcon.__init__(self)
        self.set_icon()

        self.menu_options = menu_options
        self.on_click = on_click
        self.subtitle = subtitle
        self.status = status  # callable -> short line under the title, or None
        # the popup needs a window to hang off. never shown
        self._anchor = wx.Frame(None, style=wx.FRAME_NO_TASKBAR)

        # used to run whatever was first in the menu, which turned into "download update" once there was one
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, self.on_left_up)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self.on_left_up)
        self.Bind(wx.adv.EVT_TASKBAR_RIGHT_UP, self.show_menu)

    def CreatePopupMenu(self):
        return None  # ours is drawn by hand, see show_menu

    def show_menu(self, _=None):
        items = [None if o is None else (o[0], o[1], o[2] if len(o) > 2 else None, len(o) > 3 and o[3])
                 for o in self.menu_options]
        status = self.status() if self.status else None
        menu = TrayMenu(self._anchor, items, self.TOOLTIP, self.subtitle, status)
        menu.Position(wx.GetMousePosition(), (0, 0))
        # the taskbar owns focus right now. without this the menu never loses focus and won't close
        self._anchor.Raise()
        menu.Popup()
        menu.SetFocus()

    def RemoveIcon(self):
        try:
            self._anchor.Destroy()
        except RuntimeError:
            pass
        return super().RemoveIcon()

    def set_icon(self):
        icon = wx.Icon(wx.Bitmap(self.ICON_PATH, type=wx.BITMAP_TYPE_PNG))
        self.SetIcon(icon, self.TOOLTIP)

    def on_left_up(self, _):
        if self.on_click:
            self.on_click()
