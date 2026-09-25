import json
import os
import threading

import wx
import wx.html2

from ..icons import icon
from ..utils.frame import Frame
from ... import core
from ...config import Config
from ...exceptions import UserInputException
from ...parsers import GameParser

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web')


class InitialConfigFrame(Frame):
    def __init__(self, config: Config, on_complete, on_cancel):
        super().__init__(None, title='L-Recall - Setup',
                         size=(520, 420),
                         style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX))
        self.config = config
        self._on_complete = on_complete
        self._on_cancel = on_cancel

        self.webview = wx.html2.WebView.New(self)
        sizer = wx.BoxSizer()
        sizer.Add(self.webview, 1, wx.EXPAND)
        self.SetSizer(sizer)

        self.webview.Bind(wx.html2.EVT_WEBVIEW_TITLE_CHANGED, self._on_title)
        self.webview.Bind(wx.html2.EVT_WEBVIEW_NAVIGATING, self._on_nav)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        html_path = os.path.join(WEB_DIR, 'app.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()
        self.webview.SetPage(html, 'about:blank')

    def _on_nav(self, evt):
        url = evt.GetURL()
        if url and url != 'about:blank' and not url.startswith('data:'):
            evt.Veto()

    def _on_close(self, evt):
        self.Destroy()
        self._on_cancel()

    def _on_title(self, evt):
        title = evt.GetString()
        if not title or not title.startswith('\u200b'):
            return
        try:
            msg = json.loads(title[1:])
        except (json.JSONDecodeError, ValueError):
            return
        req_id = msg.get('id')
        action = msg.get('action')
        wx.CallAfter(self._ack)
        threading.Thread(target=self._dispatch, args=(req_id, action, msg),
                         daemon=True).start()

    def _exec(self, code):
        if hasattr(self.webview, 'RunScriptAsync'):
            self.webview.RunScriptAsync(code)
        else:
            self.webview.RunScript(code)

    def _ack(self):
        try:
            self._exec('window.__ack()')
        except Exception:
            pass

    def _respond(self, req_id, data=None, error=None):
        resp = {'id': req_id}
        if error:
            resp['error'] = str(error)
        else:
            resp['data'] = data
        js = f'window.__respond({json.dumps(resp)})'
        wx.CallAfter(self._run_js, js)

    def _run_js(self, code):
        try:
            self._exec(code)
        except Exception:
            pass

    def _dispatch(self, req_id, action, msg):
        try:
            if action == 'init':
                self._respond(req_id, {
                    'configured': False,
                    'config': dict(self.config),
                })
            elif action == 'detect_game':
                self._respond(req_id, core.detect_game_exe())
            elif action == 'pick_file':
                def _pick():
                    dlg = wx.FileDialog(self, msg.get('title', ''),
                                        wildcard=msg.get('wildcard', '*.*'), style=wx.FD_OPEN)
                    path = dlg.GetPath() if dlg.ShowModal() == wx.ID_OK else None
                    dlg.Destroy()
                    self._respond(req_id, path)
                wx.CallAfter(_pick)
            elif action == 'pick_directory':
                def _pick():
                    dlg = wx.DirDialog(self, msg.get('title', ''))
                    path = dlg.GetPath() if dlg.ShowModal() == wx.ID_OK else None
                    dlg.Destroy()
                    self._respond(req_id, path)
                wx.CallAfter(_pick)
            elif action == 'save_setup':
                self.config['game_paths'] = [msg['game_path']]
                self.config['repository'] = msg['repository']
                self.config['configured'] = True
                self.config.save()
                self._respond(req_id, True)
                wx.CallAfter(self._finish_setup)
            else:
                self._respond(req_id, error=f'Not available during setup')
        except Exception as e:
            self._respond(req_id, error=str(e))

    def _finish_setup(self):
        self.Destroy()
        self._on_complete()
