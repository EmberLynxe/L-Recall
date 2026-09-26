import json
import os
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from league_vcs.exceptions import UserInputException


def _int(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _extract_player(raw):
    if not isinstance(raw, dict):
        return None
    team_raw = str(raw.get('TEAM', ''))
    try:
        team_num = int(team_raw)
        team = 'Blue' if team_num == 100 else 'Red'
    except (ValueError, TypeError):
        team = team_raw

    return {
        'champion': str(raw.get('SKIN', '')),
        'name': str(raw.get('RIOT_ID_GAME_NAME', '') or raw.get('NAME', '') or ''),
        'tag': str(raw.get('RIOT_ID_TAG_LINE', '')),
        'team': team,
        'position': str(raw.get('TEAM_POSITION', '') or raw.get('INDIVIDUAL_POSITION', '') or ''),
        'kills': _int(raw.get('CHAMPIONS_KILLED', 0)),
        'deaths': _int(raw.get('NUM_DEATHS', 0)),
        'assists': _int(raw.get('ASSISTS', 0)),
        'level': _int(raw.get('LEVEL', 0)),
        'cs': _int(raw.get('MINIONS_KILLED', 0)) + _int(raw.get('NEUTRAL_MINIONS_KILLED', 0)),
        'gold': _int(raw.get('GOLD_EARNED', 0)),
        'items': [_int(raw.get(f'ITEM{i}', 0)) for i in range(7)],
        'damage_dealt': _int(raw.get('TOTAL_DAMAGE_DEALT_TO_CHAMPIONS', 0)),
        'damage_taken': _int(raw.get('TOTAL_DAMAGE_TAKEN', 0)),
        'vision_score': _int(raw.get('VISION_SCORE', 0)),
        'win': str(raw.get('WIN', '')).lower() == 'win',
        'summoner_spells': [
            _int(raw.get('SUMMONER_SPELL_1', 0)),
            _int(raw.get('SUMMONER_SPELL_2', 0)),
        ],
        'runes': {
            'primary': _int(raw.get('PERK_PRIMARY_STYLE')),
            'secondary': _int(raw.get('PERK_SUB_STYLE')),
            'perks': [_int(raw.get(f'PERK{i}')) for i in range(6)],
            'shards': [_int(raw.get(f'STAT_PERK_{i}')) for i in range(3)],
        },
        'stats': {key: _int(raw.get(src)) for key, src in _EXTRA_STATS},
    }


_EXTRA_STATS = [
    ('phys_dealt', 'PHYSICAL_DAMAGE_DEALT_TO_CHAMPIONS'),
    ('magic_dealt', 'MAGIC_DAMAGE_DEALT_TO_CHAMPIONS'),
    ('true_dealt', 'TRUE_DAMAGE_DEALT_TO_CHAMPIONS'),
    ('phys_taken', 'PHYSICAL_DAMAGE_TAKEN'),
    ('magic_taken', 'MAGIC_DAMAGE_TAKEN'),
    ('true_taken', 'TRUE_DAMAGE_TAKEN'),
    ('mitigated', 'TOTAL_DAMAGE_SELF_MITIGATED'),
    ('heal', 'TOTAL_HEAL'),
    ('heal_team', 'TOTAL_HEAL_ON_TEAMMATES'),
    ('shield_team', 'TOTAL_DAMAGE_SHIELDED_ON_TEAMMATES'),
    ('cc_time', 'TIME_CCING_OTHERS'),
    ('dmg_buildings', 'TOTAL_DAMAGE_DEALT_TO_BUILDINGS'),
    ('dmg_objectives', 'TOTAL_DAMAGE_DEALT_TO_OBJECTIVES'),
    ('turrets', 'TURRET_TAKEDOWNS'),
    # last hits only, so they add up to the towers a team actually took. takedowns double count
    ('turrets_killed', 'TURRETS_KILLED'),
    ('dragons', 'DRAGON_KILLS'),
    ('barons', 'BARON_KILLS'),
    ('heralds', 'RIFT_HERALD_KILLS'),
    ('grubs', 'HORDE_KILLS'),
    ('steals', 'OBJECTIVES_STOLEN'),
    ('wards_placed', 'WARD_PLACED'),
    ('wards_killed', 'WARD_KILLED'),
    ('control_wards', 'VISION_WARDS_BOUGHT_IN_GAME'),
    ('double', 'DOUBLE_KILLS'),
    ('triple', 'TRIPLE_KILLS'),
    ('quadra', 'QUADRA_KILLS'),
    ('penta', 'PENTA_KILLS'),
    ('spree', 'LARGEST_KILLING_SPREE'),
    ('largest_crit', 'LARGEST_CRITICAL_STRIKE'),
    ('time_dead', 'TOTAL_TIME_SPENT_DEAD'),
    ('longest_life', 'LONGEST_TIME_SPENT_LIVING'),
    ('gold_spent', 'GOLD_SPENT'),
    ('xp', 'EXP'),
    ('q_casts', 'SPELL1_CAST'),
    ('w_casts', 'SPELL2_CAST'),
    ('e_casts', 'SPELL3_CAST'),
    ('r_casts', 'SPELL4_CAST'),
    ('jungle_own', 'NEUTRAL_MINIONS_KILLED_YOUR_JUNGLE'),
    ('jungle_enemy', 'NEUTRAL_MINIONS_KILLED_ENEMY_JUNGLE'),
    ('pings', 'BASIC_PINGS'),
]


class ReplayInfo:
    __slots__ = ('path', 'filename', 'version', 'game_length',
                 'players', 'creation_date', 'file_size')

    def __init__(self):
        self.path = ''
        self.filename = ''
        self.version = ''
        self.game_length = 0
        self.players = []
        self.creation_date = None
        self.file_size = 0

    @property
    def duration_str(self):
        if self.game_length <= 0:
            return ''
        m, s = divmod(self.game_length, 60)
        return f'{m}:{s:02d}'

    @property
    def date_str(self):
        if not self.creation_date:
            return ''
        return self.creation_date.strftime('%Y-%m-%d %H:%M')

    @property
    def patch_short(self):
        parts = self.version.split('.')
        if len(parts) >= 2:
            return f'{parts[0]}.{parts[1]}'
        return self.version

    @property
    def blue_team(self):
        return [p for p in self.players if p['team'] == 'Blue']

    @property
    def red_team(self):
        return [p for p in self.players if p['team'] == 'Red']

    @property
    def blue_win(self):
        for p in self.players:
            if p['team'] == 'Blue':
                return p.get('win', False)
        return False


class ROFLParser:
    MAGIC = b'RIOT'

    def __init__(self, path):
        self.info = ReplayInfo()
        self.info.path = path
        self.info.filename = os.path.basename(path)

        try:
            self.info.file_size = os.path.getsize(path)
            self.info.creation_date = datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            pass

        try:
            with open(path, 'rb') as f:
                magic = f.read(4)
                if magic != self.MAGIC:
                    raise ValueError('Not a ROFL file')

                fmt_version = struct.unpack('<H', f.read(2))[0]

                if fmt_version >= 2:
                    self._parse_v2(f)
                else:
                    self._parse_v1(f)
        except UserInputException:
            raise
        except Exception:
            raise UserInputException(f'Invalid replay file at {path}!')

    @property
    def version(self):
        return self.info.version

    def _parse_v2(self, f):
        # version string starts at 0x0F, the byte before it is its length
        f.seek(0x0F)
        ver = b''
        for _ in range(64):
            b = f.read(1)
            if not b or b[0] < 0x20 or b[0] > 0x7e:
                break
            ver += b
        if ver:
            self.info.version = ver.decode('ascii')

        try:
            f.seek(-4, 2)
            meta_len = struct.unpack('<i', f.read(4))[0]
            if meta_len > 0 and meta_len < self.info.file_size:
                f.seek(-(meta_len + 4), 2)
                raw = f.read(meta_len)
                metadata = json.loads(raw)
                self._apply_metadata(metadata)
        except Exception:
            pass

    def _parse_v1(self, f):
        f.seek(262)
        buf = f.read(26)
        metadata_offset = int.from_bytes(buf[6:10], byteorder='little', signed=False)
        metadata_length = int.from_bytes(buf[10:14], byteorder='little', signed=False)
        f.seek(metadata_offset)
        metadata = json.loads(f.read(metadata_length))
        self._apply_metadata(metadata)

    def _apply_metadata(self, metadata):
        if not self.info.version:
            self.info.version = metadata.get('gameVersion', '')

        game_length_ms = metadata.get('gameLength', 0)
        if isinstance(game_length_ms, (int, float)) and game_length_ms > 0:
            self.info.game_length = int(game_length_ms) // 1000

        stats_raw = metadata.get('statsJson', '')
        if stats_raw:
            self._parse_stats(stats_raw)

    def _parse_stats(self, stats_raw):
        try:
            stats = json.loads(stats_raw) if isinstance(stats_raw, str) else stats_raw
            if not isinstance(stats, list):
                return
            for raw in stats:
                player = _extract_player(raw)
                if player:
                    self.info.players.append(player)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass


_cache = {}
_cache_lock = threading.Lock()


def _parse_cached(path, key):
    with _cache_lock:
        hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        info = ROFLParser(path).info
    except Exception:
        info = None
    with _cache_lock:
        _cache[path] = (key, info)
    return info


def scan_replays(folders):
    targets = {}
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for root, _, files in os.walk(folder):
            for fname in files:
                if not fname.lower().endswith('.rofl'):
                    continue
                path = os.path.join(root, fname)
                real = os.path.normcase(os.path.abspath(path))
                if real in targets:
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                targets[real] = (path, (st.st_size, st.st_mtime_ns))

    with ThreadPoolExecutor(max_workers=8) as executor:
        infos = list(executor.map(lambda t: _parse_cached(*t), targets.values()))

    with _cache_lock:
        for gone in set(_cache) - {p for p, _ in targets.values()}:
            del _cache[gone]

    replays = [i for i in infos if i is not None]
    replays.sort(key=lambda r: r.creation_date or datetime.min, reverse=True)
    return replays
