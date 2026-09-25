"""fake game files so the tests don't need a real install"""
import json
import os
import random
import struct

from league_vcs.parsers.wad import ENTRY_STRUCT, HEADER_SIZE, SIGNATURE_SIZE


def make_wad(path, assets, seed=0, gap=True):
    """fake wad v3.4. zero padding + a duplicate entry because riot's files have both"""
    rng = random.Random(seed)
    count = len(assets) + 1
    offset = HEADER_SIZE + count * 32
    body, table = bytearray(), bytearray()
    first = None
    for i, data in enumerate(assets):
        pad = rng.randint(0, 15) if gap else 0
        body += bytes(pad)
        offset += pad
        entry = (rng.getrandbits(64), offset, len(data), len(data), 3, 0, 0, rng.getrandbits(64))
        first = first or entry
        table += ENTRY_STRUCT.pack(*entry)
        body += data
        offset += len(data)
    table += ENTRY_STRUCT.pack(rng.getrandbits(64), first[1], first[2], first[3], 3, 1, 0, first[7])
    with open(path, 'wb') as f:
        f.write(b'RW' + bytes([3, 4]) + os.urandom(SIGNATURE_SIZE) + struct.pack('<QI', 0, count))
        f.write(table)
        f.write(body)


def random_assets(n, seed, size=(40, 3000)):
    rng = random.Random(seed)
    return [rng.randbytes(rng.randint(*size)) for _ in range(n)]


def make_rofl(path, version='16.19.821.7343', length_ms=1_800_000, players=None):
    """tiny fake rofl v2"""
    players = players or [
        {'SKIN': 'Ahri', 'RIOT_ID_GAME_NAME': 'Tester', 'RIOT_ID_TAG_LINE': 'EUW', 'TEAM': '100',
         'WIN': 'Win', 'CHAMPIONS_KILLED': '7', 'NUM_DEATHS': '2', 'ASSISTS': '9', 'PERK0': '8112',
         'PERK_PRIMARY_STYLE': '8100', 'PHYSICAL_DAMAGE_DEALT_TO_CHAMPIONS': '1234'},
        {'SKIN': 'Zed', 'RIOT_ID_GAME_NAME': 'Other', 'RIOT_ID_TAG_LINE': 'EUW', 'TEAM': '200', 'WIN': 'Fail'},
    ]
    meta = json.dumps({'gameLength': length_ms, 'statsJson': json.dumps(players)}).encode()
    head = b'RIOT' + struct.pack('<H', 2) + bytes(8) + bytes([len(version)]) + version.encode()
    with open(path, 'wb') as f:
        f.write(head + b'\x01\x00\x00\x00' + os.urandom(64) + meta + struct.pack('<i', len(meta)))
