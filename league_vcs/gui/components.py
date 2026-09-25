import wx

from . import theme
from .icons import icon


def make_header_container(parent, subtitle=None):
    header_container = wx.BoxSizer()

    logo_img = wx.Image(icon('icon.png'), wx.BITMAP_TYPE_PNG).Scale(48, 48)
    logo = wx.StaticBitmap(parent, bitmap=logo_img.ConvertToBitmap())
    header_container.Add(logo, 0, wx.RIGHT, border=12)

    title_box = wx.BoxSizer(wx.VERTICAL)

    title = wx.StaticText(parent, label='L-Recall')
    title.SetFont(wx.Font(20, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
    title.SetForegroundColour(theme.TEXT_BRIGHT)
    title_box.Add(title, 0)

    sub_label = subtitle or 'Replay Version Manager'
    sub = wx.StaticText(parent, label=sub_label)
    sub.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
    sub.SetForegroundColour(theme.TEXT_DIM)
    title_box.Add(sub, 0, wx.TOP, 2)

    header_container.Add(title_box, 1, wx.CENTER)

    return header_container


def confirm(parent, message, caption, style=0):
    dlg = wx.MessageDialog(parent, message, caption, style=wx.YES_NO | wx.NO_DEFAULT | style)
    return dlg.ShowModal() == wx.ID_YES


def file_picker(title, wildcard):
    modal = wx.FileDialog(None, title, wildcard=wildcard, style=wx.FD_OPEN)
    if modal.ShowModal() == wx.ID_CANCEL:
        return None
    return modal.GetPath()


def directory_picker(title):
    modal = wx.DirDialog(None, title)
    if modal.ShowModal() == wx.ID_CANCEL:
        return None
    return modal.GetPath()
