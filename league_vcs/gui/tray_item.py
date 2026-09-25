import os

import wx
import wx.adv


def create_menu_item(menu, label, func=None):
    item = wx.MenuItem(menu, -1, label)
    if func is None:
        item.Enable(False)
    else:
        menu.Bind(wx.EVT_MENU, func, id=item.GetId())
    menu.Append(item)
    return item


class TrayItem(wx.adv.TaskBarIcon):
    ICON_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), 'icons', 'small-icon.png'))
    TOOLTIP = 'L-Recall'

    def __init__(self, menu_options, on_click=None):
        wx.adv.TaskBarIcon.__init__(self)
        self.set_icon()

        self.menu_options = menu_options
        self.on_click = on_click

        # used to run whatever was first in the menu, which turned into "download update" once there was one
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, self.on_left_up)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self.on_left_up)

    def CreatePopupMenu(self):
        menu = wx.Menu()

        create_menu_item(menu, self.TOOLTIP, None)
        menu.AppendSeparator()

        for item in self.menu_options:
            if item is None:
                menu.AppendSeparator()
            else:
                label, func = item
                create_menu_item(menu, label, func)

        return menu

    def set_icon(self):
        icon = wx.Icon(wx.Bitmap(self.ICON_PATH, type=wx.BITMAP_TYPE_PNG))
        self.SetIcon(icon, self.TOOLTIP)

    def on_left_up(self, _):
        if self.on_click:
            self.on_click()
