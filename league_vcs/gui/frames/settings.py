import json
import os
import pathlib
import shutil
import urllib.parse
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

import wx
import wx.html2

from ..icons import icon
from ..utils.frame import CallbackFrame
from ..utils.output import capture
from large_vcs.progress import reporting

from ... import __version__, assets, core, selfupdate, uninstall, updates, vanguard, winproc
from ...config import Config
from ...notes import Notes
from ...exceptions import UserInputException
from ...parsers import GameParser
from ...parsers.rofl import scan_replays

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web')


def _version_key(v):
    return [int(p) if p.isdigit() else 0 for p in v.split('.')]


def _dark_title_bar(frame):
    try:
        import ctypes
        hwnd = ctypes.c_void_p(frame.GetHandle())
        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)
        caption = ctypes.c_int(0x00140F0B)  # COLORREF is BGR, this is #0b0f14
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), 4)
        # make windows repaint the title bar
        ctypes.windll.user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
    except Exception:
        pass


def _file_url(path):
    return pathlib.Path(os.path.abspath(path)).as_uri()


# install path has spaces so the url comes back %20-encoded. compare decoded or we block our own page
def _same_url(a, b):
    return urllib.parse.unquote(a.split('#')[0]).lower() == urllib.parse.unquote(b.split('#')[0]).lower()


class SettingsFrame(CallbackFrame):
    def __init__(self, config: Config, quit_app=None, notify=None):
        super().__init__(None, title='L-Recall')
        self.SetBackgroundColour(wx.Colour(11, 15, 20))
        self.config = config
        self._notify = notify
        self._icons_done = frozenset()
        self._icons_lock = threading.Lock()
        self._quit_app = quit_app
        self._replays = []
        self.notes = Notes(os.path.join(os.path.dirname(config._path), 'replay_notes.json'))

        # wx sizes are physical pixels here, not scaled
        area = wx.Display(max(wx.Display.GetFromWindow(self), 0)).GetClientArea()
        want = self.FromDIP(wx.Size(1320, 840))
        self.SetMinSize(self.FromDIP(wx.Size(960, 600)))
        self.SetSize(min(want.width, int(area.width * .92)), min(want.height, int(area.height * .92)))
        self.CentreOnScreen()
        _dark_title_bar(self)
        self.Bind(wx.EVT_SHOW, lambda e: (wx.CallAfter(_dark_title_bar, self), e.Skip()))

        edge = wx.html2.WebView.IsBackendAvailable(wx.html2.WebViewBackendEdge)
        print('WebView backend:', 'Edge' if edge else 'fallback (page will not render)')
        if not edge:
            wx.MessageBox('Microsoft Edge WebView2 could not be loaded, so the window will be blank.\n\n'
                          'Install the WebView2 Runtime from Microsoft, then reopen L-Recall.',
                          'L-Recall', wx.ICON_WARNING)
        self.webview = wx.html2.WebView.New(self, backend=wx.html2.WebViewBackendEdge if edge
                                            else wx.html2.WebViewBackendDefault)
        sizer = wx.BoxSizer()
        sizer.Add(self.webview, 1, wx.EXPAND)
        self.SetSizer(sizer)

        self.webview.Bind(wx.html2.EVT_WEBVIEW_TITLE_CHANGED, self._on_title)
        self.webview.Bind(wx.html2.EVT_WEBVIEW_NAVIGATING, self._on_nav)

        self._page_url = _file_url(os.path.join(WEB_DIR, 'app.html'))
        self.webview.LoadURL(self._page_url)
        if self.config.get('auto_download_assets', True):
            threading.Thread(target=assets.prefetch_latest, daemon=True).start()

    def _on_nav(self, evt):
        url = evt.GetURL()
        if url and url != 'about:blank' and not _same_url(url, self._page_url):
            evt.Veto()

    def _on_title(self, evt):
        title = evt.GetString()
        # the page talks to us through document.title. webview2 strips control chars out of it
        # without saying anything, so \x01 never showed up. fuck you. zero width space works.
        # to the machine god i pray.
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
        # has to be async, otherwise the page can't send its next message while we're mid-call
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

    def _push_console(self, text):
        js = f'window.__console_line({json.dumps(text)})'
        wx.CallAfter(self._run_js, js)

    def _push_progress(self, done, total, label):
        js = f'window.__console_progress({int(done)}, {int(total)}, {json.dumps(label)})'
        wx.CallAfter(self._run_js, js)

    def _console_done(self, ok=True):
        wx.CallAfter(self._run_js, f'window.__console_done({json.dumps(ok)})')

    def _handle_notify(self, req_id, msg):
        """page finished something while you weren't looking. tray popup + flash the taskbar"""
        title = str(msg.get('title', ''))[:60]
        text = 'Finished.' if msg.get('ok') else "Didn't finish. Open L-Recall to see why."
        if self._notify:
            self._notify(title, text)
        wx.CallAfter(self.RequestUserAttention)
        self._respond(req_id, True)

    def _run_js(self, code):
        try:
            self._exec(code)
        except Exception:
            pass

    def _dispatch(self, req_id, action, msg):
        started = time.perf_counter()
        try:
            handler = getattr(self, f'_handle_{action}', None)
            if handler:
                handler(req_id, msg)
            else:
                self._respond(req_id, error=f'Unknown action: {action}')
        except Exception as e:
            traceback.print_exc()
            self._respond(req_id, error=str(e))
        elapsed = time.perf_counter() - started
        if elapsed > 1:
            print(f'{action} took {elapsed:.1f}s')

    # handlers

    def _handle_init(self, req_id, _msg):
        self._respond(req_id, {
            'configured': bool(self.config.get('configured')),
            'config': dict(self.config),
        })

    def _handle_get_config(self, req_id, _msg):
        paths_info = []
        for p in self.config.get('game_paths', []):
            entry = {'path': p, 'version': '', 'error': False}
            try:
                game = GameParser(p)
                entry['version'] = game.version
            except Exception:
                entry['version'] = 'Error'
                entry['error'] = True
            paths_info.append(entry)
        self._respond(req_id, {
            'game_paths': paths_info,
            'replay_folders': self.config.get('replay_folders', []),
            'repository': self.config.get('repository', ''),
            'player_names': self.config.get('player_names', []),
            'auto_download_assets': self.config.get('auto_download_assets', True),
            'keep_prepared': self.config.get('keep_prepared', 2),
            'quick_start': self.config.get('quick_start', False),
        })

    def _handle_get_replays(self, req_id, _msg):
        repo = self._repo_or_none()
        stored = set(repo.list()) if repo else set()

        folders = self.config.get('replay_folders', [])
        self._replays = scan_replays(folders)
        self._cache_icons_for(self._replays)

        result = []
        for r in self._replays:
            available = r.version in stored
            result.append({
                'date': r.date_str,
                'ts': r.creation_date.timestamp() if r.creation_date else 0,
                'version': r.version,
                'patch': r.patch_short,
                'duration': r.duration_str,
                'length': r.game_length,
                'status': 'Ready' if available else 'Missing',
                'filename': r.filename,
                'path': r.path,
                'players': [{
                    'c': p['champion'], 'n': p['name'], 't': p['tag'],
                    'team': p['team'], 'k': p['kills'], 'd': p['deaths'],
                    'a': p['assists'], 'lvl': p['level'], 'cs': p['cs'], 'win': p['win'],
                } for p in r.players],
                'win': r.blue_win if r.players else None,
                **self.notes.get(r.filename),
            })

        self._respond(req_id, {
            'replays': result,
            'all_tags': self.notes.all_tags(),
            'folders': list(folders),
            'stored': sorted(stored, reverse=True),
            'repo_ok': repo is not None,
        })

    def _cache_icons_for(self, replays):
        """icons for every patch your replays are on, quietly, in the background. only what's missing.
        before this, older patches pulled every icon off the cdn each time you opened a game"""
        if not self.config.get('auto_download_assets', True):
            return
        patches = frozenset(r.patch_short for r in replays)
        if patches <= self._icons_done or not self._icons_lock.acquire(blocking=False):
            return

        def run():
            try:
                assets.download_for_replays(replays, log=lambda *_: None)
                self._icons_done |= patches
            except Exception:
                pass
            finally:
                self._icons_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def _repo_or_none(self):
        path = self.config.get('repository')
        if not path or not os.path.isdir(os.path.join(path, 'lvcs')):
            return None
        core.set_repo_path(path)
        return core.repo

    def _handle_get_storage(self, req_id, _msg):
        repo = self._repo_or_none()
        if repo is None:
            self._respond(req_id, {'path': self.config.get('repository') or '', 'exists': False})
            return
        report = repo.storage_report()
        self._respond(req_id, {
            'path': self.config.get('repository'),
            'exists': True,
            'total': repo.total_size(),
            'free': shutil.disk_usage(repo.files_dir).free,
            'report': report,
            'needs_optimize': any(r['whole'] for r in report),
        })

    def _handle_estimate_storage(self, req_id, _msg):
        repo = self._repo_or_none()
        self._respond(req_id, repo.estimate_optimized() if repo else None)

    def _handle_optimize_storage(self, req_id, _msg):
        self._run_console_op(req_id, self._do_optimize)

    def _do_optimize(self):
        repo = self._repo_or_none()
        if repo is None:
            raise UserInputException('Storage folder not found.')
        before = repo.total_size()
        repo.optimize()
        after = repo.total_size()
        print(f'Storage went from {before / 1024 ** 3:.1f} GB to {after / 1024 ** 3:.1f} GB.')

    def _handle_set_repository(self, req_id, msg):
        path = os.path.abspath(msg.get('path', ''))
        if os.path.basename(path).lower() == 'lvcs' and os.path.isdir(os.path.join(path, 'patches')):
            path = os.path.dirname(path)
        if not os.path.isdir(path):
            self._respond(req_id, error='That folder does not exist.')
            return
        self.config['repository'] = path
        self.config.save()
        core.set_repo_path(path)
        self._respond(req_id, {'path': path, 'patches': len(core.repo.list())})

    def _handle_set_note(self, req_id, msg):
        saved = self.notes.set(msg.get('filename', ''), msg.get('tags', []), msg.get('note', ''))
        self._respond(req_id, {'tags': saved.get('tags', []), 'note': saved.get('note', ''),
                               'all_tags': self.notes.all_tags()})

    def _handle_get_patch_costs(self, req_id, _msg):
        repo = self._repo_or_none()
        self._respond(req_id, repo.patch_costs() if repo else {})

    def _handle_set_player_names(self, req_id, msg):
        names = [n.strip() for n in msg.get('names', []) if n and n.strip()]
        self.config['player_names'] = names
        self.config.save()
        self._respond(req_id, True)

    def _handle_get_patches(self, req_id, _msg):
        result = []
        repo = self._repo_or_none()
        if repo:
            current, kept = repo.current(), set(repo.kept())
            for patch in sorted(repo.list(), key=_version_key, reverse=True):
                status = 'Active' if patch == current else 'Kept' if patch in kept else 'Stored'
                result.append({'version': patch, 'status': status})
        self._respond(req_id, result)

    def _handle_pick_file(self, req_id, msg):
        def _pick():
            dlg = wx.FileDialog(self, msg.get('title', 'Select file'),
                                wildcard=msg.get('wildcard', '*.*'), style=wx.FD_OPEN)
            path = dlg.GetPath() if dlg.ShowModal() == wx.ID_OK else None
            dlg.Destroy()
            self._respond(req_id, path)
        wx.CallAfter(_pick)

    def _handle_pick_directory(self, req_id, msg):
        def _pick():
            dlg = wx.DirDialog(self, msg.get('title', 'Select folder'))
            path = dlg.GetPath() if dlg.ShowModal() == wx.ID_OK else None
            dlg.Destroy()
            self._respond(req_id, path)
        wx.CallAfter(_pick)

    def _handle_save_setup(self, req_id, msg):
        self.config['game_paths'] = [msg['game_path']]
        self.config['repository'] = msg['repository']
        self.config['configured'] = True
        self.config.save()
        self._respond(req_id, True)

    def _handle_add_replay_folder(self, req_id, msg):
        path = msg.get('path', '')
        folders = self.config.get('replay_folders', [])
        if path and path not in folders:
            folders.append(path)
            self.config['replay_folders'] = folders
            self.config.save()
        self._respond(req_id, True)

    def _handle_remove_replay_folder(self, req_id, msg):
        idx = msg.get('index', -1)
        folders = self.config.get('replay_folders', [])
        if 0 <= idx < len(folders):
            folders.pop(idx)
            self.config['replay_folders'] = folders
            self.config.save()
        self._respond(req_id, True)

    def _handle_add_game_path(self, req_id, msg):
        path = msg.get('path', '')
        if not path:
            self._respond(req_id, True)
            return
        try:
            GameParser(path)
        except UserInputException as e:
            self._respond(req_id, error=str(e))
            return
        paths = self.config.get('game_paths', [])
        if path not in paths:
            paths.append(path)
            self.config['game_paths'] = paths
            self.config.save()
        self._respond(req_id, True)

    def _handle_detect_game(self, req_id, _msg):
        self._respond(req_id, core.detect_game_exe())

    def _handle_find_game_paths(self, req_id, _msg):
        paths = self.config.get('game_paths', [])
        have = {os.path.normcase(p) for p in paths}
        new = [p for p in core.detect_game_exes() if os.path.normcase(p) not in have]
        if new:
            self.config['game_paths'] = paths + new
            self.config.save()
        self._respond(req_id, len(new))

    def _handle_remove_game_path(self, req_id, msg):
        idx = msg.get('index', -1)
        paths = self.config.get('game_paths', [])
        if 0 <= idx < len(paths):
            paths.pop(idx)
            self.config['game_paths'] = paths
            self.config.save()
        self._respond(req_id, True)

    def _handle_get_replay_detail(self, req_id, msg):
        path = msg.get('path', '')
        for r in self._replays:
            if r.path == path:
                self._respond(req_id, {
                    'date': r.date_str,
                    'version': r.version,
                    'patch': r.patch_short,
                    'duration': r.duration_str,
                    'length': r.game_length,
                    'filename': r.filename,
                    'path': r.path,
                    'blue_team': r.blue_team,
                    'red_team': r.red_team,
                    'blue_win': r.blue_win if r.players else None,
                    'file_size': r.file_size,
                })
                return
        self._respond(req_id, error='Replay not found')

    def _handle_open_explorer(self, req_id, msg):
        path = os.path.normpath(msg.get('path', ''))
        if os.path.isfile(path):
            # explorer only respects /select if the path is in the same argument
            subprocess.Popen(f'explorer /select,"{path}"')
        elif os.path.isdir(path):
            os.startfile(path)
        elif os.path.isdir(os.path.dirname(path)):
            os.startfile(os.path.dirname(path))
        else:
            self._respond(req_id, error='File no longer exists')
            return
        self._respond(req_id, True)

    def _handle_get_assets(self, req_id, _msg):
        vs = assets.versions()
        self._respond(req_id, {
            'base': _file_url(assets.ROOT) + '/',
            'versions': vs[:400],
        })

    def _handle_check_update(self, req_id, msg):
        enabled = self.config.get('check_updates', True)
        force = bool(msg.get('force'))
        release = updates.newer_than_running(force) if (enabled or force) else None
        latest = updates.latest() if (enabled or force) else None
        self._respond(req_id, {'current': __version__, 'update': release, 'enabled': enabled,
                               'latest': latest['version'] if latest else None,
                               'can_install': bool(selfupdate.can_update() and release and release.get('zip')),
                               'dismissed': self.config.get('dismissed_update', '')})

    def _handle_install_update(self, req_id, _msg):
        self._run_console_op(req_id, self._do_update)

    def _do_update(self):
        if not selfupdate.can_update():
            raise UserInputException('Running from source, so there\'s nothing to update in place.')
        release = updates.newer_than_running(force=True)
        if not release:
            print('Already on the newest version.')
            return
        print(f"Downloading L-Recall {release['version']}...")
        new = selfupdate.download(release)
        print('Download matches its published hash.')
        repo = self._repo_or_none()
        if repo:
            # a patch getting stored right now finishes first. the lock is never let go on purpose,
            # it goes when we exit so nothing new starts in the meantime
            print('Waiting for patch storage to be idle...')
            repo.lock.__enter__()
        selfupdate.apply(new, os.path.dirname(sys.executable))
        print('L-Recall will close and open again on the new version in a few seconds.')
        if self._quit_app:
            wx.CallLater(2500, self._quit_app)

    def _handle_dismiss_update(self, req_id, msg):
        self.config['dismissed_update'] = msg.get('version', '')
        self.config.save()
        self._respond(req_id, True)

    def _handle_set_check_updates(self, req_id, msg):
        self.config['check_updates'] = bool(msg.get('on'))
        self.config.save()
        self._respond(req_id, True)

    def _handle_vanguard_status(self, req_id, _msg):
        self._respond(req_id, {**vanguard.status(),
                               'warn_old': self.config.get('vanguard_warn_old', True)})

    def _handle_set_vanguard_warn(self, req_id, msg):
        self.config['vanguard_warn_old'] = bool(msg.get('on'))
        self.config.save()
        self._respond(req_id, True)

    def _handle_replay_preflight(self, req_id, msg):
        path = msg.get('path', '')
        r = next((x for x in self._replays if x.path == path), None)
        if r is None:
            self._respond(req_id, {'level': None, 'message': None})
            return
        level, message = core.vanguard_preflight(r.version, self.config.get('vanguard_warn_old', True))
        self._respond(req_id, {'level': level, 'message': message})

    def _handle_get_runes(self, req_id, msg):
        table = assets.runes(msg.get('version') or '')
        ids = [int(i) for i in msg.get('ids', []) if str(i).isdigit()]
        assets.ensure_rune_icons(table, ids)
        self._respond(req_id, {str(i): table[i] for i in ids if i in table})

    def _handle_asset_cache(self, req_id, _msg):
        self._respond(req_id, assets.cache_info())

    def _handle_asset_download(self, req_id, _msg):
        if not self._replays:
            self._replays = scan_replays(self.config.get('replay_folders', []))
        self._run_console_op(req_id, assets.download_for_replays, self._replays)

    def _handle_asset_refresh(self, req_id, _msg):
        def _refresh():
            vs = assets.refresh()
            if not vs:
                print("Couldn't reach Data Dragon.")
                return
            print(f'Latest patch is {vs[0]}. Checking for missing icons...')
            print(f'{assets.prefetch_latest()} new files. Done.')
        self._run_console_op(req_id, _refresh)

    def _handle_repack_storage(self, req_id, _msg):
        self._run_console_op(req_id, self._do_repack)

    def _do_repack(self):
        repo = self._repo_or_none()
        if repo is None:
            raise UserInputException('Storage folder not found.')
        if winproc.game_running():
            raise UserInputException('League is running. Close the game (and any replay) first.')
        repo.repack()

    def _handle_set_replay_start(self, req_id, msg):
        if 'keep' in msg:
            core.keep_prepared = self.config['keep_prepared'] = max(1, min(4, int(msg['keep'])))
            if core.repo:
                core.repo.keep_prepared = core.keep_prepared
        if 'quick' in msg:
            core.quick_start = self.config['quick_start'] = bool(msg['quick'])
        self.config.save()
        self._respond(req_id, True)

    def _handle_set_auto_assets(self, req_id, msg):
        self.config['auto_download_assets'] = bool(msg.get('on'))
        self.config.save()
        self._respond(req_id, True)

    def _handle_asset_clear(self, req_id, _msg):
        self._run_console_op(req_id, assets.clear)

    def _handle_uninstall_info(self, req_id, _msg):
        repo = self.config.get('repository')
        self._respond(req_id, {'repository': repo or '', 'patch_bytes': uninstall.stored_patch_bytes(repo),
                               'frozen': bool(getattr(sys, 'frozen', False))})

    def _handle_uninstall(self, req_id, msg):
        self._run_console_op(req_id, self._do_uninstall, bool(msg.get('delete_patches')))

    def _handle_ensure_assets(self, req_id, msg):
        version = msg.get('version')
        if version:
            assets.ensure(version, msg.get('champions', []), msg.get('items', []), msg.get('spells', []))
        self._respond(req_id, True)

    def _handle_copy_clipboard(self, req_id, msg):
        def _copy():
            if wx.TheClipboard.Open():
                wx.TheClipboard.SetData(wx.TextDataObject(msg.get('text', '')))
                wx.TheClipboard.Close()
            self._respond(req_id, True)
        wx.CallAfter(_copy)

    def _handle_open_link(self, req_id, msg):
        # webbrowser.open is os.startfile on windows. hand it a file path and it opens the file
        url = urllib.parse.urlparse(str(msg.get('url', '')))
        if url.scheme in ('http', 'https') and url.netloc:
            webbrowser.open(url.geturl())
        self._respond(req_id, True)

    def _handle_watch_replay(self, req_id, msg):
        self._run_console_op(req_id, self._do_watch, msg.get('path', ''))

    def _handle_add_patch(self, req_id, msg):
        self._run_console_op(req_id, self._do_add_patch, msg.get('path', ''))

    def _handle_drop_patches(self, req_id, msg):
        self._run_console_op(req_id, self._do_drop_patches, msg.get('patches', []))

    def _handle_export_patches(self, req_id, msg):
        def _pick_then_export():
            dlg = wx.DirDialog(self, 'Where do you want to export?')
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                self._respond(req_id, True)
                self._console_done()
                return
            dest = dlg.GetPath()
            dlg.Destroy()
            threading.Thread(
                target=self._run_console_op_inner,
                args=(req_id, self._do_export_patches, msg.get('patches', []), dest),
                daemon=True
            ).start()
        wx.CallAfter(_pick_then_export)

    def _handle_restore_patch(self, req_id, msg):
        self._run_console_op(req_id, self._do_restore_patch, msg.get('patch', ''))

    # console stuff

    def _run_console_op(self, req_id, func, *args):
        self._run_console_op_inner(req_id, func, *args)

    def _run_console_op_inner(self, req_id, func, *args):
        output = _ConsoleWriter(self._push_console)
        ok = True
        with capture(output), reporting(self._push_progress):
            try:
                func(*args)
            except Exception as e:
                ok = False
                known = (UserInputException, ValueError, AssertionError)
                print(str(e) if isinstance(e, known) else traceback.format_exc())
        self._console_done(ok)
        self._respond(req_id, True)

    def _do_watch(self, path):
        core.set_repo_path(self.config['repository'])
        print(f'Loading replay: {path}')
        core.watch(path)
        print('Replay finished.')

    def _do_add_patch(self, path):
        core.set_repo_path(self.config['repository'])
        core.add(os.path.dirname(path))
        print('Done!')

    def _known_patches(self, patches):
        stored = set(core.repo.list())
        for patch in patches:
            if patch not in stored:
                raise ValueError(f"Patch {patch} isn't stored.")
        return patches

    def _do_drop_patches(self, patches):
        core.set_repo_path(self.config['repository'])
        for patch in self._known_patches(patches):
            print(f'Dropping {patch}...')
            core.repo.drop(patch, collect=False)
        # one cleanup pass for the lot, it has to read every remaining patch
        print('Removing files nothing uses any more...')
        print(f'{core.repo.gc():,} files removed. Done!')

    def _do_uninstall(self, delete_patches):
        if winproc.game_running():
            raise UserInputException('League is running. Close the game (and any replay) first.')
        uninstall.remove_data(self.config.get('repository'), delete_patches)
        app_dir = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else self.config._path)
        if uninstall.schedule_program_removal(app_dir):
            print(f'L-Recall will close in a few seconds and delete its files from {app_dir}.')
        else:
            print('Running from source, so the program files are left alone.')
        if self._quit_app:
            wx.CallLater(4000, self._quit_app)

    def _do_export_patches(self, patches, destination):
        core.set_repo_path(self.config['repository'])
        for patch in self._known_patches(patches):
            dest_path = os.path.join(destination, patch.replace('.', '-'))
            print(f'Exporting {patch} to {dest_path}...')
            if os.path.exists(dest_path):
                raise ValueError(f'Destination {dest_path} already exists!')
            os.makedirs(dest_path)
            core.repo.export(patch, dest_path)
        print('Done!')

    def _do_restore_patch(self, patch):
        core.set_repo_path(self.config['repository'])
        if winproc.game_running():
            raise UserInputException('League is running. Close the game (and any replay) first.')
        self._known_patches([patch])
        if core.repo.current() == patch:
            print(f'Deselecting patch {patch}...')
            core.repo.clean()
        else:
            print(f'Restoring patch {patch}...')
            core.repo.restore(patch)
        print('Done!')


class _ConsoleWriter:
    def __init__(self, callback):
        self._cb = callback

    def write(self, s):
        try:
            if sys.__stdout__ is not None:
                sys.__stdout__.write(s)
        except Exception:
            pass
        if s:
            self._cb(s)
        return len(s)

    def flush(self):
        pass
