#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'Inazuma Eleven GO Light / Shadow - ie_a.fa Randomizer\n=====================================================\nRead a Level-5 ARC0 archive and patch selected cfg.bin integer values without\nchanging file sizes or layouts. Uses only the Python 3.8+ standard library;\ntkinter is optional for the GUI. See README.md for usage and verification limits.\n\nQuick start:\n    python IEGO1Randomizer.py\n    python IEGO1Randomizer.py ie_a.fa --seed 1234\n    python IEGO1Randomizer.py ie_a.fa --characters shuffle\n    python IEGO1Randomizer.py ie_a.fa --dump-chars\n    python IEGO1Randomizer.py --help\n'
import argparse
import hashlib
import collections
import json
import math
import mmap
import os
import random
import shutil
import struct
import sys
import time
import zlib

VERSION = "1.2"

# =============================================================================
# 1. Level-5 compression (decompression only, as needed for
# ARC0 tables; game data files are stored uncompressed)
# =============================================================================

def l5_decompress(data):
    hdr = struct.unpack_from('<I', data, 0)[0]
    method, size = hdr & 7, hdr >> 3
    body = data[4:]
    if method == 0:
        return bytes(body[:size])
    if method == 1:
        return _lz10(body, size)
    if method in (2, 3):
        return _huffman(body, size, 4 if method == 2 else 8)
    if method == 4:
        return _rle(body, size)
    raise ValueError('Unknown Level-5 compression method: %d' % method)


def _lz10(src, size):
    out = bytearray()
    i, n = 0, len(src)
    while len(out) < size and i < n:
        flags = src[i]
        i += 1
        for bit in range(7, -1, -1):
            if len(out) >= size:
                break
            if flags & (1 << bit):
                b1, b2 = src[i], src[i + 1]
                i += 2
                length = (b1 >> 4) + 3
                disp = (((b1 & 0xF) << 8) | b2) + 1
                for _ in range(length):
                    out.append(out[-disp])
            else:
                out.append(src[i])
                i += 1
    return bytes(out[:size])


def _rle(src, size):
    out = bytearray()
    i = 0
    while len(out) < size:
        b = src[i]
        i += 1
        if b & 0x80:
            out.extend(bytes([src[i]]) * ((b & 0x7F) + 3))
            i += 1
        else:
            c = (b & 0x7F) + 1
            out.extend(src[i:i + c])
            i += c
    return bytes(out[:size])


def _huffman(src, size, bits):
    tree = src[:(src[0] + 1) * 2]
    pos = len(tree)
    out = bytearray()
    half = None
    need = size * (8 // bits)
    produced, node = 0, 1
    while produced < need:
        word = struct.unpack_from('<I', src, pos)[0]
        pos += 4
        for b in range(31, -1, -1):
            bit = (word >> b) & 1
            nd = tree[node]
            child = (node & ~1) + (nd & 0x3F) * 2 + 2 + bit
            if nd & (0x40 if bit else 0x80):
                val = tree[child]
                if bits == 8:
                    out.append(val)
                elif half is None:
                    half = val
                else:
                    out.append(half | (val << 4))
                    half = None
                produced += 1
                node = 1
                if produced >= need:
                    break
            else:
                node = child
    return bytes(out[:size])


# =============================================================================
# 2. ARC0 container (.fa)
# =============================================================================

class Arc0:
    'ARC0 reader with same-size, in-place file patching.'

    def __init__(self, path, writable=False):
        self.path = path
        self.f = open(path, 'r+b' if writable else 'rb')
        self.mm = None
        try:
            self.mm = mmap.mmap(self.f.fileno(), 0,
                                access=mmap.ACCESS_WRITE if writable else mmap.ACCESS_READ)
            h = struct.unpack_from('<4s5I2HI', self.mm, 0)
            if h[0] != b'ARC0':
                raise ValueError('%s is not an ARC0 archive (ie_a.fa)' % path)
            (_, dir_off, dirhash_off, file_off, name_off, self.data_off,
             self.n_dir, _n_dirhash, self.n_files) = h
            dirs = l5_decompress(self.mm[dir_off:dirhash_off])
            files = l5_decompress(self.mm[file_off:name_off])
            names = l5_decompress(self.mm[name_off:self.data_off])
            self._files = files
            self.entries = {}
            for i in range(self.n_dir):
                _h, _x, fc, fno, dno = struct.unpack_from('<5I', dirs, i * 20)
                dname = names[dno:names.index(b'\0', dno)].decode('utf-8', 'replace')
                if dname and not dname.endswith('/'):
                    continue
                cnt, first = fc >> 16, fc & 0xFFFF
                for k in range(first, first + cnt):
                    _fh, rel, _off, _size = struct.unpack_from('<4I', files, k * 16)
                    o = fno + rel
                    fname = names[o:names.index(b'\0', o)].decode('utf-8', 'replace')
                    self.entries[dname + fname] = k
        except BaseException:
            if self.mm is not None: self.mm.close()
            self.f.close()
            raise

    def info(self, path):
        _h, _rel, off, size = struct.unpack_from('<4I', self._files, self.entries[path] * 16)
        return self.data_off + off, size

    def read(self, path):
        off, size = self.info(path)
        return bytes(self.mm[off:off + size])

    def write(self, path, data):
        off, size = self.info(path)
        if len(data) != size:
            raise ValueError('Size mismatch while patching %s (%d != %d)' % (path, len(data), size))
        self.mm[off:off + size] = data

    def close(self):
        try:
            self.mm.flush()
        except Exception:
            pass
        self.mm.close()
        self.f.close()


# =============================================================================
# 3. Level-5 .cfg.bin (in-place integer patching)
# =============================================================================

class Entry:
    __slots__ = ('crc', 'n', 'types', 'off')

    def __init__(self, crc, n, types, off):
        self.crc, self.n, self.types, self.off = crc, n, types, off


class CfgBin:
    def __init__(self, data):
        self.data = bytearray(data)
        self.count, self.str_off, self.str_len, _ = struct.unpack_from('<4I', self.data, 0)
        self.entries = []
        pos = 0x10
        for _ in range(self.count):
            crc = struct.unpack_from('<I', self.data, pos)[0]
            n = self.data[pos + 4]
            types = [(self.data[pos + 5 + k // 4] >> ((k % 4) * 2)) & 3 for k in range(n)]
            hdr = (5 + (n + 3) // 4 + 3) & ~3
            self.entries.append(Entry(crc, n, types, pos + hdr))
            pos += hdr + 4 * n
        self.keys = self._read_keys()
        self._by_tag = None

    def _read_keys(self):
        kt = (self.str_off + self.str_len + 15) & ~15
        keys = {}
        try:
            _size, cnt, stroff, _strlen = struct.unpack_from('<4I', self.data, kt)
            for i in range(cnt):
                crc, o = struct.unpack_from('<2I', self.data, kt + 16 + 8 * i)
                s = kt + stroff + o
                keys[crc] = bytes(self.data[s:self.data.index(0, s)]).decode('ascii', 'replace')
        except Exception:
            pass
        return keys

    def by_tag(self, name):
        if self._by_tag is None:
            self._by_tag = collections.defaultdict(list)
            for e in self.entries:
                self._by_tag[self.keys.get(e.crc, '%08X' % e.crc)].append(e)
        return self._by_tag.get(name, [])

    def get(self, e, i):
        return struct.unpack_from('<I', self.data, e.off + 4 * i)[0]

    def set(self, e, i, v):
        struct.pack_into('<I', self.data, e.off + 4 * i, int(v) & 0xFFFFFFFF)

    def string(self, off):
        s = self.str_off + off
        return bytes(self.data[s:self.data.index(0, s)]).decode('utf-8', 'replace')

    def layout(self):
        'Structural signature (verify that only values changed).'
        return [(e.crc, e.n, tuple(e.types), e.off) for e in self.entries]

    def tobytes(self):
        return bytes(self.data)


# =============================================================================
# 4. Format constants (inferred from game data; see README)
# =============================================================================

P_KEY, P_AVATAR_ACTIVE, P_SKILLROW, P_AVATAR = 1, 16, 18, 41
P_ELEM, P_POS = 12, 13
P_STATS = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 15]
STAT_NAMES = ['GP', 'TP', 'Kick', 'Dribbling', 'Technique', 'Block', 'Speed',
              'Stamina', 'Catch', 'Luck', 'Freedom']
SI_CATCH, SI_FREEDOM = 8, 10
SI_MAIN7 = [2, 3, 4, 5, 6, 7, 9]           # Kick through Stamina plus Luck (excluding Catch)
P_GROWTH = [25, 26, 27, 28, 29, 30, 31, 32]  # 8 growth curves
P_GROWTH_HT = [23, 24]                        # GP / TP curves
B_KEY, B_GENDER = 0, 9
# CHARA_BASE_INFO columns defining character identity (name, model,
# face, gender...). The key (column 0) stays unchanged: chara_param and game scripts
# still reference it, so the protagonist slot retains its story role
# but takes another identity. Adjust these columns if testing reveals a mismatch
# (use --dump-chars to inspect raw columns).
B_IDENTITY = list(range(1, 12))
ELEM = {1: 'Wind', 2: 'Wood', 3: 'Fire', 4: 'Earth'}
POS = {1: 'GK', 2: 'FW', 3: 'MF', 4: 'DF'}
GEN = {1: 'M', 2: 'F'}
# SKILL_CONFIG_INFO
K_HASH, K_NAME, K_ELEM, K_TYPE, K_ORDER, K_SPECIAL, K_POWER = 0, 1, 3, 6, 8, 9, 16
SKTYPE = {1: 'Shoot', 2: 'Dribbling', 3: 'Block', 4: 'Catch', 15: 'Passive'}
# ITEM_AVATAR
A_HASH, A_NAME, A_ELEM = 0, 2, 17
# ITEM_EQUIPMENT / ITEM_CONSUME
EQ_CATS = (10, 20, 30)
EQ_BONUS = list(range(13, 21))
IT_CAT, IT_SELL, IT_BUY = 3, 8, 9

PATHS = {
    'base': 'data/res/character/chara_base.cfg.bin',
    'param': 'data/res/character/chara_param.cfg.bin',
    'stable': 'data/res/character/skill_table.cfg.bin',
    'sconf': 'data/res/skill/skill_config.cfg.bin',
    'items': 'data/res/item/item_config.cfg.bin',
}
SHOP_PREFIX = 'data/res/shop/shop_shp'


class RandomizerError(Exception):
    pass


# =============================================================================
# 5. Options
# =============================================================================

class Options:
    def __init__(self, **kw):
        self.seed = None
        self.lang = 'en'
        self.gender = 'random'        # off | random
        self.female_chance = 0.5
        self.element = 'random'       # off | random     (player element)
        self.position = 'off'         # off | random
        self.stats = 'balanced'       # off | shuffle | balanced | chaos
        self.growth = 'balanced'      # off | balanced | chaos
        self.skills = 'balanced'      # off | balanced | chaos
        self.spirits = 'random'       # off | random
        self.spirit_match_element = True
        self.equipment = 'random'     # off | random
        self.consumables = 'shuffle'  # off | shuffle
        self.prices = 'random'        # off | random
        self.shops = 'random'         # off | random
        self.spirit_stats = 'off'     # off | shuffle
        self.characters = 'off'       # off | shuffle   (character identities: Arion <-> Shawn...)
        self.char_keep_gender = False # shuffle identities only within the same gender
        for k, v in kw.items():
            if not hasattr(self, k):
                raise RandomizerError('Unknown option: %s' % k)
            setattr(self, k, v)

    def as_dict(self):
        return dict(self.__dict__)


# =============================================================================
# 6. Randomizer
# =============================================================================

class Randomizer:
    def __init__(self, arc, opt, log=print):
        self.arc, self.opt, self.log = arc, opt, log
        self.cfg = {}
        self.orig_layout = {}
        self.spoiler = []
        self.changes = collections.Counter()
        self._load()

    # ---------------------------------------------------------------- loading
    def rng(self, name):
        return random.Random("%s/%s" % (self.opt.seed, name))

    def _load(self):
        needed = dict(PATHS)
        for p in sorted(self.arc.entries):
            if p.startswith(SHOP_PREFIX):
                needed[p] = p
        for k, p in needed.items():
            if p not in self.arc.entries:
                raise RandomizerError('Missing %s inside the .fa archive (is this Light/Shadow data?)' % p)
            self.cfg[k] = CfgBin(self.arc.read(p))
            self.orig_layout[k] = self.cfg[k].layout()
        self.base = self.cfg['base']
        self.param = self.cfg['param']
        self.stable = self.cfg['stable']
        self.sconf = self.cfg['sconf']
        self.items = self.cfg['items']
        self.shop_keys = [k for k in self.cfg if k.startswith(SHOP_PREFIX)]

        B = self.base.by_tag('CHARA_BASE_INFO')
        P = self.param.by_tag('CHARA_PARAM_INFO')
        S = self.stable.by_tag('SKILL_TABLE')
        K = self.sconf.by_tag('SKILL_CONFIG_INFO')
        if not (B and P and S and K) or any(e.n != 12 for e in B) or any(e.n != 42 for e in P) \
                or any(e.n != 4 for e in S) or any(e.n != 21 for e in K):
            raise RandomizerError('Unexpected data structure: unsupported game version.')

        # names
        self.n_chara = self._nouns('chara')
        self.n_skill = self._nouns('skill')
        self.n_item = self._nouns('item')

        # players = chara_param entries with their matching chara_base entries
        bmap = {self.base.get(b, B_KEY): b for b in B}
        self.chars = []
        for idx, p in enumerate(P):
            b = bmap.get(self.param.get(p, P_KEY))
            if b is None:
                continue
            stats = [self.param.get(p, c) for c in P_STATS]
            self.chars.append({
                'idx': idx, 'p': p, 'b': b,
                'name': self.n_chara.get(self.base.get(b, 2), '#%d' % idx),
                'dummy': max(stats[:10]) <= 1,      # e.g. the placeholder Mark Evans entry
            })
        self.active = [c for c in self.chars if not c['dummy']]

        # skills
        self.skills = {}
        for k in K:
            h = self.sconf.get(k, K_HASH)
            self.skills[h] = {
                'hash': h, 'e': k,
                'name': self.n_skill.get(self.sconf.get(k, K_NAME), '%08X' % h),
                'elem': self.sconf.get(k, K_ELEM), 'type': self.sconf.get(k, K_TYPE),
                'order': self.sconf.get(k, K_ORDER), 'special': self.sconf.get(k, K_SPECIAL),
                'power': self.sconf.get(k, K_POWER),
            }
        used = {self.stable.get(r, 2) for r in S}
        self.skill_pool = [s for h, s in self.skills.items()
                           if h in used and s['special'] == 1 and s['order'] < 1000
                           and s['type'] in SKTYPE]
        self.stable_rows = S

        # Fighting Spirits (avatars)
        self.avatars = {}
        for a in self.items.by_tag('ITEM_AVATAR'):
            h = self.items.get(a, A_HASH)
            self.avatars[h] = {'hash': h, 'e': a, 'elem': self.items.get(a, A_ELEM),
                               'name': self.n_item.get(self.items.get(a, A_NAME), '%08X' % h)}

        # all items indexed by hash
        self.item_by_hash = {}
        for tag in ('ITEM_EQUIPMENT', 'ITEM_CONSUME'):
            for e in self.items.by_tag(tag):
                h = self.items.get(e, 0)
                self.item_by_hash[h] = {
                    'hash': h, 'e': e, 'tag': tag, 'cat': self.items.get(e, IT_CAT),
                    'sub': self.items.get(e, 4),
                    'name': self.n_item.get(self.items.get(e, 1), '%08X' % h)}

        self._snapshot_vanilla_stats()

    def _nouns(self, kind):
        for lang in (self.opt.lang, 'en'):
            path = 'data/res/text/%s_text_%s.cfg.bin' % (kind, lang)
            if path in self.arc.entries:
                t = CfgBin(self.arc.read(path))
                out = {}
                for e in t.by_tag('NOUN_INFO'):
                    for i in range(2, 10):
                        v = t.get(e, i)
                        if v != 0xFFFFFFFF:
                            try:
                                out[t.get(e, 0)] = t.string(v)
                            except ValueError:
                                pass
                            break
                return out
        return {}

    def _snapshot_vanilla_stats(self):
        # vanilla stat distribution by position (excluding placeholder players)
        self.van = {pos: [[] for _ in P_STATS] for pos in POS}
        self.van_all = [[] for _ in P_STATS]
        for c in self.active:
            pos = self.param.get(c['p'], P_POS)
            if pos not in POS:
                continue
            for i, col in enumerate(P_STATS):
                v = self.param.get(c['p'], col)
                self.van[pos][i].append(v)
                self.van_all[i].append(v)
        for pos in self.van:
            for i in range(len(P_STATS)):
                self.van[pos][i].sort()
        for i in range(len(P_STATS)):
            self.van_all[i].sort()
        self.growth_vals = {col: sorted(self.param.get(c['p'], col) for c in self.active)
                            for col in P_GROWTH + P_GROWTH_HT}
        self.positions_vanilla = [self.param.get(c['p'], P_POS) for c in self.active]

    @staticmethod
    def _pct(sorted_vals, q):
        if not sorted_vals:
            return 1
        k = (len(sorted_vals) - 1) * q / 100.0
        lo, hi = int(math.floor(k)), int(math.ceil(k))
        return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)

    # ------------------------------------------------------------- players
    def char_line(self, c):
        p, b = c['p'], c['b']
        return {
            'who': self.n_chara.get(self.base.get(b, 2), '#%d' % c['idx']),
            'gender': self.base.get(b, B_GENDER), 'elem': self.param.get(p, P_ELEM),
            'pos': self.param.get(p, P_POS),
            'stats': [self.param.get(p, col) for col in P_STATS],
            'growth': [self.param.get(p, col) for col in P_GROWTH_HT + P_GROWTH],
            'skills': self._char_skills(c),
            'spirit': self._spirit_name(self.param.get(p, P_AVATAR)),
        }

    def _char_rows(self, c):
        first = self.param.get(c['p'], P_SKILLROW)
        return self.stable_rows[first:first + 4]

    def _char_skills(self, c):
        return [(self.stable.get(r, 1), self.skills.get(self.stable.get(r, 2), {}).get('name', '?'))
                for r in self._char_rows(c)]

    def _spirit_name(self, h):
        return self.avatars[h]['name'] if h in self.avatars else ('-' if h == 0 else '%08X' % h)

    def randomize_characters(self):
        o = self.opt
        before = {c['idx']: self.char_line(c) for c in self.active}

        if o.characters == 'shuffle':
            self._shuffle_identities(self.rng('characters'))
            self.log('  · character identities shuffled')

        if o.gender == 'random':
            r = self.rng('gender')
            for c in self.active:
                if self.base.get(c['b'], B_GENDER) in (1, 2):
                    self.base.set(c['b'], B_GENDER, 2 if r.random() < o.female_chance else 1)
            self.log('  · gender randomized')

        if o.position == 'random':
            r = self.rng('position')
            for c in self.active:
                self.param.set(c['p'], P_POS, r.choice(self.positions_vanilla))
            self.log('  · positions randomized')

        if o.element == 'random':
            r = self.rng('element')
            for c in self.active:
                self.param.set(c['p'], P_ELEM, r.randint(1, 4))
            self.log('  · elements randomized')

        if o.stats != 'off':
            r = self.rng('stats')
            for c in self.active:
                self._rand_stats(c, r, before[c['idx']])
            self.log('  · stats randomized (%s)' % o.stats)

        if o.growth != 'off':
            r = self.rng('growth')
            for c in self.active:
                self._rand_growth(c, r)
            self.log('  · growth curves randomized (%s)' % o.growth)

        if o.skills != 'off':
            r = self.rng('skills')
            done = set()
            for c in self.active:
                first = self.param.get(c['p'], P_SKILLROW)
                if first in done:
                    continue
                done.add(first)
                self._rand_skills(c, r)
            self.log('  · skills randomized (%s) — %d moves in the pool'
                     % (o.skills, len(self.skill_pool)))

        if o.spirits == 'random':
            r = self.rng('spirits')
            self._rand_spirits(r)
            self.log('  · Fighting Spirits reassigned')

        after = {c['idx']: self.char_line(c) for c in self.active}
        self._log_characters(before, after)

    # --- character identities
    def _shuffle_identities(self, r):
        'Shuffle identities (name, model and related fields) between character slots.\n\n        Each slot keeps its key, chara_param (stats, moves and curves), and story\n        role. Only the identity occupying that slot changes.\n        '
        pool, seen = [], set()
        for c in self.active:
            if c['b'].off in seen:      # two parameter entries sharing one base: process it once
                continue
            seen.add(c['b'].off)
            pool.append(c)
        groups = [pool]
        if self.opt.char_keep_gender:
            by_g = collections.defaultdict(list)
            for c in pool:
                by_g[self.base.get(c['b'], B_GENDER)].append(c)
            groups = list(by_g.values())
        lines = []
        for g in groups:
            idents = [[self.base.get(c['b'], j) for j in B_IDENTITY] for c in g]
            order = list(range(len(g)))
            r.shuffle(order)
            for c, k in zip(g, order):
                for j, v in zip(B_IDENTITY, idents[k]):
                    self.base.set(c['b'], j, v)
                new = self.n_chara.get(idents[k][B_IDENTITY.index(2)], '?')
                if new != c['name']:
                    lines.append("  %-28s -> %s" % (c['name'], new))
        self.spoiler.append(('CHARACTER IDENTITIES (original slot -> replacement)', lines))
        self.changes['personajes'] = len(lines)

    # --- stats
    def _class_sample(self, si, pos, lo=3, hi=97, r=None):
        vals = self.van.get(pos, self.van_all)[si] or self.van_all[si]
        a, b = self._pct(vals, lo), self._pct(vals, hi)
        return int(round(r.uniform(a, b)))

    def _rand_stats(self, c, r, before):
        p = c['p']
        old = before['stats']
        pos_old = before['pos']
        pos = self.param.get(p, P_POS)
        if pos not in POS:
            return
        new = list(old)
        mode = self.opt.stats
        if mode == 'chaos':
            for i in range(len(P_STATS)):
                new[i] = self._class_sample(i, pos, 3, 97, r)
        elif mode == 'shuffle':
            vals = [old[i] for i in SI_MAIN7]
            r.shuffle(vals)
            for i, v in zip(SI_MAIN7, vals):
                new[i] = v
            if pos != pos_old:
                new[SI_CATCH] = self._class_sample(SI_CATCH, pos, 10, 90, r)
        else:  # balanced: redistribute the same total stat budget
            total = sum(old[i] for i in SI_MAIN7)
            prof = [self._mean(self.van[pos][i]) for i in SI_MAIN7]
            w = [pf * r.uniform(0.35, 1.65) for pf in prof]
            lo = [max(1, self._pct(self.van_all[i], 2)) for i in SI_MAIN7]
            hi = [self._pct(self.van_all[i], 99.5) for i in SI_MAIN7]
            vals = self._distribute(total, w, lo, hi)
            for i, v in zip(SI_MAIN7, vals):
                new[i] = v
            new[SI_CATCH] = self._class_sample(SI_CATCH, pos, 10, 90, r)
            for i in (0, 1):  # GP / TP
                v = int(round(old[i] * r.uniform(0.75, 1.25)))
                new[i] = int(min(max(v, self._pct(self.van_all[i], 2)), self._pct(self.van_all[i], 99)))
            new[SI_FREEDOM] = self._class_sample(SI_FREEDOM, pos, 5, 95, r)
        for i, col in enumerate(P_STATS):
            self.param.set(p, col, max(1, new[i]))

    @staticmethod
    def _mean(v):
        return sum(v) / float(len(v)) if v else 1.0

    @staticmethod
    def _distribute(total, weights, lo, hi):
        n = len(weights)
        vals = [0.0] * n
        fixed = [False] * n
        for _ in range(8):
            free = [i for i in range(n) if not fixed[i]]
            rest = total - sum(vals[i] for i in range(n) if fixed[i])
            sw = sum(weights[i] for i in free) or 1.0
            clamped = False
            for i in free:
                v = rest * weights[i] / sw
                if v < lo[i]:
                    vals[i], fixed[i], clamped = lo[i], True, True
                elif v > hi[i]:
                    vals[i], fixed[i], clamped = hi[i], True, True
                else:
                    vals[i] = v
            if not clamped:
                break
        out = [int(round(v)) for v in vals]
        diff = int(total) - sum(out)          # correct rounding
        order = sorted(range(n), key=lambda i: -weights[i])
        for i in order:
            if diff == 0:
                break
            step = 1 if diff > 0 else -1
            if lo[i] <= out[i] + step <= hi[i]:
                out[i] += step
                diff -= step
        return out

    def _rand_growth(self, c, r):
        p = c['p']
        cols = P_GROWTH
        if self.opt.growth == 'chaos':
            for col in cols + P_GROWTH_HT:
                self.param.set(p, col, r.choice(self.growth_vals[col]))
        else:
            vals = [self.param.get(p, col) for col in cols]
            r.shuffle(vals)
            for col, v in zip(cols, vals):
                self.param.set(p, col, v)
            a, b = (self.param.get(p, col) for col in P_GROWTH_HT)
            if r.random() < 0.5:
                a, b = b, a
            self.param.set(p, P_GROWTH_HT[0], a)
            self.param.set(p, P_GROWTH_HT[1], b)

    # --- skills
    @staticmethod
    def _compat(s, pos):
        gk_skill = s['type'] == 4 or (s['type'] == 15 and s['elem'] == 1)
        return gk_skill if pos == 1 else not gk_skill

    def _rand_skills(self, c, r):
        pos = self.param.get(c['p'], P_POS)
        rows = self._char_rows(c)
        if len(rows) != 4:
            return
        taken = set()
        orig = [self.skills.get(self.stable.get(row, 2)) for row in rows]
        for row, os_ in zip(rows, orig):
            compat = [s for s in self.skill_pool if self._compat(s, pos) and s['hash'] not in taken]
            if not compat:
                compat = [s for s in self.skill_pool if s['hash'] not in taken]
            if self.opt.skills == 'balanced' and os_:
                same = [s for s in compat if s['type'] == os_['type'] and
                        (s['type'] != 15 or (s['elem'] == os_['elem'] and
                                             (s['order'] >= 10) == (os_['order'] >= 10)))]
                cand = same or compat
                w = [math.exp(-abs(s['power'] - os_['power']) / 40.0) for s in cand]
                pick = r.choices(cand, weights=w)[0]
            else:
                pick = r.choice(compat)
            taken.add(pick['hash'])
            self.stable.set(row, 2, pick['hash'])

    # --- Fighting Spirits
    def _rand_spirits(self, r):
        pool = list(self.avatars.values())
        for c in self.active:
            p = c['p']
            cur = self.param.get(p, P_AVATAR)
            if cur == 0:
                continue                      # only replace existing Fighting Spirits
            elem = self.param.get(p, P_ELEM)
            cand = [a for a in pool if a['elem'] == elem] if self.opt.spirit_match_element else pool
            pick = r.choice(cand or pool)
            active = self.param.get(p, P_AVATAR_ACTIVE)
            self.param.set(p, P_AVATAR, pick['hash'])
            if active != 0:
                self.param.set(p, P_AVATAR_ACTIVE, pick['hash'])

    # ------------------------------------------------------------------ items
    def randomize_items(self):
        o = self.opt
        if o.equipment == 'random':
            self._rand_equipment(self.rng('equipment'))
        if o.consumables == 'shuffle':
            self._shuffle_consumables(self.rng('consumables'))
        if o.prices == 'random':
            self._rand_prices(self.rng('prices'))
        if o.spirit_stats == 'shuffle':
            self._shuffle_avatar_stats(self.rng('avatarstats'))
        if o.shops == 'random':
            self._rand_shops(self.rng('shops'))

    def _equipment(self):
        return [e for e in self.items.by_tag('ITEM_EQUIPMENT') if self.items.get(e, IT_CAT) in EQ_CATS]

    def _rand_equipment(self, r):
        it = self.items
        eq = self._equipment()
        colmax = {j: max([it.get(e, j) for e in eq] + [1]) for j in EQ_BONUS}
        lines = []
        for e in eq:
            nz = [(j, it.get(e, j)) for j in EQ_BONUS if it.get(e, j)]
            if not nz:
                continue
            new_cols = r.sample(EQ_BONUS, len(nz))
            vals = {}
            for (j, v), nj in zip(nz, new_cols):
                nv = int(round(v / float(colmax[j]) * colmax[nj] / 2.0)) * 2
                vals[nj] = max(2, nv)
            old = {j: v for j, v in nz}
            for j in EQ_BONUS:
                it.set(e, j, vals.get(j, 0))
            lines.append("  %-32s %s  ->  %s" % (self.n_item.get(it.get(e, 1), '?'),
                                                  self._fmt_bonus(old), self._fmt_bonus(vals)))
        self.spoiler.append(('EQUIPMENT (bonuses #1-#8 = stat bonus columns)', lines))
        self.changes['equipamiento'] = len(lines)
        self.log('  · equipment bonuses reassigned (%d items)' % len(lines))

    @staticmethod
    def _fmt_bonus(d):
        return ' '.join('#%d:+%d' % (j - 12, v) for j, v in sorted(d.items())) or '-'

    def _shuffle_consumables(self, r):
        it = self.items
        groups = collections.defaultdict(list)
        for e in it.by_tag('ITEM_CONSUME'):
            if it.get(e, IT_CAT) == 50 and it.get(e, 12) <= 999:
                groups[(it.get(e, 4), it.get(e, 11))].append(e)
        lines = []
        for g, es in groups.items():
            tuples = [(it.get(e, 12), it.get(e, 13)) for e in es]
            r.shuffle(tuples)
            for e, (a, b) in zip(es, tuples):
                old = (it.get(e, 12), it.get(e, 13))
                it.set(e, 12, a)
                it.set(e, 13, b)
                if old != (a, b):
                    lines.append("  %-32s %s -> %s" % (self.n_item.get(it.get(e, 1), '?'), old, (a, b)))
        self.spoiler.append(('CONSUMABLES (amount restored)', lines))
        self.changes['consumibles'] = len(lines)
        self.log('  · food/drink effects shuffled (%d items)' % len(lines))

    def _rand_prices(self, r):
        it = self.items
        targets = self._equipment() + [e for e in it.by_tag('ITEM_CONSUME') if it.get(e, IT_CAT) in (40, 50)]
        lines = []
        for e in targets:
            buy, sell = it.get(e, IT_BUY), it.get(e, IT_SELL)
            if buy == 0:
                continue
            f = math.exp(r.uniform(math.log(0.4), math.log(2.5)))
            nb = max(10, int(round(buy * f / 10.0)) * 10)
            ns = int(round(nb * (sell / float(buy)) / 10.0)) * 10 if sell else 0
            it.set(e, IT_BUY, nb)
            it.set(e, IT_SELL, ns)
            lines.append('  %-32s buy %6d -> %6d   sell %5d -> %5d' %
                         (self.n_item.get(it.get(e, 1), '?'), buy, nb, sell, ns))
        self.spoiler.append(('PRICES', lines))
        self.changes['precios'] = len(lines)
        self.log('  · prices randomized (%d items)' % len(lines))

    def _shuffle_avatar_stats(self, r):
        it = self.items
        av = [a['e'] for a in self.avatars.values()]
        pairs = [(it.get(e, 12), it.get(e, 13)) for e in av]
        r.shuffle(pairs)
        lines = []
        for a, (x, y) in zip(self.avatars.values(), pairs):
            e = a['e']
            lines.append("  %-20s %s -> %s" % (a['name'], (it.get(e, 12), it.get(e, 13)), (x, y)))
            it.set(e, 12, x)
            it.set(e, 13, y)
        self.spoiler.append(('FIGHTING SPIRIT STATS (columns 12/13)', lines))
        self.log('  · Fighting Spirit stats shuffled')

    def _shop_group(self, h):
        d = self.item_by_hash.get(h)
        if not d:
            return None
        if d['tag'] == 'ITEM_EQUIPMENT' and d['cat'] in EQ_CATS:
            return ('EQ', d['cat'])
        if d['tag'] == 'ITEM_CONSUME' and d['cat'] == 50:
            return ('CO', 50)
        if d['tag'] == 'ITEM_CONSUME' and d['cat'] == 40:
            return ('SC', d['sub'])
        return None            # leave keys, PalPacks, cards and similar items unchanged

    def _rand_shops(self, r):
        groups = collections.defaultdict(list)
        for h, d in self.item_by_hash.items():
            g = self._shop_group(h)
            if g:
                groups[g].append(h)
        lines = []
        for key in sorted(self.shop_keys):
            cfg = self.cfg[key]
            ents = cfg.by_tag('SHOP_CONFIG_INFO')
            in_shop = {cfg.get(e, 0) for e in ents}
            for e in ents:
                h = cfg.get(e, 0)
                g = self._shop_group(h)
                if not g:
                    continue
                cand = [x for x in groups[g] if x not in in_shop]
                if not cand:
                    continue
                nh = r.choice(cand)
                in_shop.discard(h)
                in_shop.add(nh)
                cfg.set(e, 0, nh)
                lines.append("  %-14s %-30s -> %s" % (os.path.basename(key).split('.')[0][5:],
                                                     self.item_by_hash[h]['name'],
                                                     self.item_by_hash[nh]['name']))
        self.spoiler.append(('SHOPS', lines))
        self.changes['tiendas'] = len(lines)
        self.log('  · shop inventories randomized (%d slots)' % len(lines))

    # ------------------------------------------------------------------ log
    def _log_characters(self, before, after):
        lines = []
        for c in self.active:
            b, a = before[c['idx']], after[c['idx']]
            ch = []
            if b['who'] != a['who']:
                ch.append('character %s→%s' % (b['who'], a['who']))
            if b['gender'] != a['gender']:
                ch.append('gender %s→%s' % (GEN.get(b['gender']), GEN.get(a['gender'])))
            if b['elem'] != a['elem']:
                ch.append('element %s→%s' % (ELEM.get(b['elem']), ELEM.get(a['elem'])))
            if b['pos'] != a['pos']:
                ch.append('position %s→%s' % (POS.get(b['pos']), POS.get(a['pos'])))
            if b['spirit'] != a['spirit']:
                ch.append('Fighting Spirit %s→%s' % (b['spirit'], a['spirit']))
            head = "%-28s [%s · %s · %s]" % (c['name'], GEN.get(a['gender']), ELEM.get(a['elem']),
                                              POS.get(a['pos']))
            lines.append(head)
            if ch:
                lines.append('     changes: ' + ', '.join(ch))
            if b['stats'] != a['stats']:
                lines.append("     stats  : " + ' '.join('%s %d→%d' % (n[:3], x, y) for n, x, y in
                                                        zip(STAT_NAMES, b['stats'], a['stats'])))
            if b['skills'] != a['skills']:
                lines.append('     moves: ' + ' | '.join('Lv%d %s' % (lv, nm) for lv, nm in a['skills']))
                lines.append('     before: ' + ' | '.join('Lv%d %s' % (lv, nm) for lv, nm in b['skills']))
        self.spoiler.insert(0, ('PLAYERS (%d)' % len(self.active), lines))

    def write_spoiler(self, path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write('Inazuma Eleven GO Light/Shadow - Randomizer v%s\n' % VERSION)
            f.write('Seed: %s\n' % self.opt.seed)
            f.write('Options: %s\n\n' % json.dumps(self.opt.as_dict(), ensure_ascii=False))
            for title, lines in self.spoiler:
                f.write("=" * 78 + "\n%s\n" % title + "=" * 78 + "\n")
                f.write("\n".join(lines) + "\n\n")

    # --------------------------------------------------------------- writing
    def run(self):
        self.log('Randomizing with seed %s ...' % self.opt.seed)
        self.randomize_characters()
        self.randomize_items()

    def commit(self):
        'Write modified cfg.bin files back to ARC0 without changing their sizes.'
        n = 0
        for k, cfg in self.cfg.items():
            path = PATHS.get(k, k)
            new = cfg.tobytes()
            if new != self.arc.read(path):
                self.arc.write(path, new)
                n += 1
        return n

    def verify(self):
        'Read back from .fa and verify that only values changed, not the structure.'
        for k in self.cfg:
            path = PATHS.get(k, k)
            cur = CfgBin(self.arc.read(path))
            if cur.layout() != self.orig_layout[k]:
                raise RandomizerError('Verification failed: the structure of %s changed' % path)
        # semantic consistency
        P = self.param.by_tag('CHARA_PARAM_INFO')
        cp = CfgBin(self.arc.read(PATHS['param']))
        for e in cp.by_tag('CHARA_PARAM_INFO'):
            if cp.get(e, P_ELEM) not in ELEM or cp.get(e, P_POS) not in POS:
                raise RandomizerError('Verification failed: invalid element/position')
        return True


# =============================================================================
# Story script repair (Squirrel 2, 32-bit little endian; no RNG)
# =============================================================================

class NutScript:
    """Bounded Squirrel stream reader. Only literal operands are edited.

    Changed functions borrow bytes from debug line metadata to append literals.
    Function lengths, instruction counts, branch offsets and ARC0 sizes stay fixed.
    Unknown layouts fail closed, before any output is published.
    """
    def __init__(self, data):
        self.data, self.pos, self.functions = bytes(data), 0, []
        if self.take(10) != b'\xfa\xfaRIQS\x01\x00\x00\x00':
            raise RandomizerError('Unsupported Squirrel header.')
        self.root = self.function(0)
        if self.take(4) != b'LIAT' or self.pos != len(self.data):
            raise RandomizerError('Unsupported Squirrel trailer.')

    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise RandomizerError('Truncated Squirrel stream.')
        value = self.data[self.pos:self.pos+n]
        self.pos += n
        return value

    def uint(self):
        return struct.unpack('<I', self.take(4))[0]

    def tag(self):
        if self.take(4) != b'TRAP':
            raise RandomizerError('Invalid Squirrel section marker.')

    def obj(self):
        kind = self.uint()
        if kind == 0x08000010:
            value = self.take(self.uint())
        elif kind in (0x05000002, 0x05000008, 0x01000004):
            value = self.take(4)
        elif kind == 0x01000001:
            value = b''
        else:
            raise RandomizerError('Unsupported Squirrel literal type %08X.' % kind)
        return kind, value

    @staticmethod
    def string(value):
        return (0x08000010, value.encode('ascii'))

    @staticmethod
    def text(obj):
        return obj[1].decode('ascii', 'replace') if obj[0] == 0x08000010 else None

    @staticmethod
    def pack_obj(obj):
        kind, value = obj
        return struct.pack('<I', kind) + (struct.pack('<I', len(value)) if kind == 0x08000010 else b'') + value

    def function(self, depth):
        if depth > 64:
            raise RandomizerError('Squirrel nesting limit exceeded.')
        start = self.pos
        self.tag(); source, name = self.obj(), self.obj(); self.tag()
        counts = [self.uint() for _ in range(8)]
        if any(n > len(self.data) // 4 for n in counts):
            raise RandomizerError('Invalid Squirrel section counts.')
        nl, np, no, nv, nn, nd, ni, nf = counts
        self.tag(); literals = [self.obj() for _ in range(nl)]
        self.tag(); parameters = [self.obj() for _ in range(np)]
        self.tag(); outer_start = self.pos
        for _ in range(no): self.take(4); self.obj(); self.obj()
        outers = self.data[outer_start:self.pos]
        self.tag(); local_start = self.pos
        for _ in range(nv): self.obj(); self.take(12)
        locals_ = self.data[local_start:self.pos]
        self.tag(); lines = self.take(nn * 8)
        self.tag(); defaults = self.take(nd * 4)
        self.tag(); instructions = [list(struct.unpack('<i4B', self.take(8))) for _ in range(ni)]
        self.tag()
        f = dict(source=source, name=name, counts=counts, literals=literals,
                 parameters=parameters, outers=outers, locals=locals_, lines=lines,
                 defaults=defaults, instructions=instructions, dirty=False)
        self.functions.append(f)
        f['children'] = [self.function(depth+1) for _ in range(nf)]
        f['tail'] = self.take(6)
        f['raw'] = self.data[start:self.pos]
        return f

    def encode_function(self, f):
        children = [self.encode_function(c) for c in f['children']]
        if not f['dirty'] and all(b == c['raw'] for b, c in zip(children, f['children'])):
            return f['raw']
        counts = list(f['counts']); counts[0] = len(f['literals'])
        def encode(source, lines):
            counts[4] = len(lines) // 8
            return (b'TRAP' + self.pack_obj(source) + self.pack_obj(f['name']) + b'TRAP' +
                    struct.pack('<8I', *counts) + b'TRAP' + b''.join(map(self.pack_obj, f['literals'])) +
                    b'TRAP' + b''.join(map(self.pack_obj, f['parameters'])) +
                    b'TRAP' + f['outers'] + b'TRAP' + f['locals'] + b'TRAP' + lines +
                    b'TRAP' + f['defaults'] + b'TRAP' +
                    b''.join(struct.pack('<i4B', *i) for i in f['instructions']) +
                    b'TRAP' + b''.join(children) + f['tail'])
        lines = f['lines']; source = f['source']
        result = encode(source, lines)
        extra = len(result) - len(f['raw'])
        if extra > 0:
            remove = (extra + 7) // 8
            if remove >= len(lines) // 8 or source[0] != 0x08000010:
                raise RandomizerError('Insufficient debug metadata space in %s.' % self.text(f['name']))
            lines = lines[:len(lines)-remove*8]
            source = (source[0], source[1] + b' ' * (remove*8-extra))
            result = encode(source, lines)
        if len(result) != len(f['raw']):
            raise RandomizerError('Script size changed unexpectedly.')
        return result

    def tobytes(self):
        result = self.data[:10] + self.encode_function(self.root) + b'LIAT'
        NutScript(result)  # Reparse all section boundaries before accepting output.
        return result

    @staticmethod
    def calls(f):
        """Resolve only straight-line literal arguments immediately before CALL.

        Clearing at each call/control-flow boundary deliberately avoids guessing
        register values from another branch or interpreting dynamic arguments.
        """
        registers, pending = {}, {}
        for pc, (a1, op, a0, a2, a3) in enumerate(f['instructions']):
            if op in (0,):
                continue
            if op in (1, 4):
                if not 0 <= a1 < len(f['literals']):
                    raise RandomizerError('Invalid Squirrel literal index.')
                registers[a0] = (NutScript.text(f['literals'][a1]), pc, 0)
                if op == 4:
                    if a3 >= len(f['literals']):
                        raise RandomizerError('Invalid Squirrel double-load index.')
                    registers[a2] = (NutScript.text(f['literals'][a3]), pc, 4)
            elif op == 8:
                if not 0 <= a1 < len(f['literals']):
                    raise RandomizerError('Invalid Squirrel call index.')
                pending[a0] = (NutScript.text(f['literals'][a1]), pc)
                registers.pop(a3, None)
            elif op in (5, 6):
                name, prep = pending.get(a1, (None, None))
                args = [registers.get(r) for r in range(a2+1, a2+a3)]
                yield pc, name, prep, args
                registers.clear(); pending.clear()
            else:
                # Constants may intervene between string loads and a call.
                if op in (2, 3, 22): registers.pop(a0, None)
                else: registers.clear(); pending.clear()

    def literal_operand(self, f, pc, field, value):
        instruction = f['instructions'][pc]
        if (field == 0 and instruction[1] not in (1, 4, 8)) or (field == 4 and instruction[1] != 4):
            raise RandomizerError('Unsupported script argument producer.')
        obj = self.string(value)
        if f['literals'][instruction[field]] == obj:
            return
        index = next((i for i, v in enumerate(f['literals']) if v == obj), len(f['literals']))
        if field == 4 and index > 255:
            raise RandomizerError('Double-load literal index exceeds byte range.')
        if index == len(f['literals']): f['literals'].append(obj)
        instruction[field] = index
        f['dirty'] = True


# Explicit call-site manifest for the observed Light/Shadow script layout.
# (script, function, instruction, character, slot). Battle learning is deliberately
# listed separately from persistent unlocks, rather than globally rewritten.
STORY_SKILLS = [
    ('ev42033100', 'ev42033100_10', 764, 'para_cp0008', 0),
    ('ev44050120', 'Event', 778, 'para_cp0008', 1),
    ('ev42061300', 'ev42061300_40', 452, 'para_cp0008', 2),
    ('ev42051800', 'Event', 2418, 'para_cp0005', 0),
    ('ev42101400', 'Event', 4436, 'para_cp0004', 2),
    ('ev44120040', 'Event', 687, 'para_cp0010', 2),
    ('btl0308', 'ScBtlResult', 16, 'para_cp0008', 2),
    ('btl0308', 'RunEv44060040', 10, 'para_cp0008', 2),
]
STORY_BATTLE_SKILLS = [
    ('btl0006', 'RunEv44050120', 51, 'para_cp0008', 1),
    ('btl0016', 'RunEv44120040', 73, 'para_cp0010', 2),
]
# (script, function, instruction, character, original spirit, command)
STORY_SPIRITS = [
    ('btl0014', 'CallPurposeMarker', 63, 'para_cp0008', 'ck0001', 'CMND_BTL_RESET_AVATER'),
    ('btl0014', 'CallPurposeMarker', 99, 'para_cp0290', 'ck0004', 'CMND_BTL_SET_AVATER'),
    ('btl0014', 'CallPurposeMarker', 104, 'para_cp0008', 'ck0033', 'CMND_BTL_SET_AVATER'),
    ('btl0014', 'CallMatchGoalEnd', 23, 'para_cp0009', 'ck0003', 'CMND_BTL_SET_AVATER'),
    ('btl0014', 'CallMatchGoalEnd', 28, 'para_cp0010', 'ck0002', 'CMND_BTL_SET_AVATER'),
    ('btl0014', 'CallMatchGoalEnd', 53, 'para_cp0290', 'ck0004', 'CMND_BTL_SET_AVATER'),
    ('btl0014', 'CallMatchGoalEnd', 58, 'para_cp0008', 'ck0030', 'CMND_BTL_SET_AVATER'),
    ('btl0014', 'RunEv44110010', 31, 'para_cp0290', 'ck0004', 'CMND_BTL_SET_AVATER'),
    ('btl0007', 'RunEv44060050', 56, 'para_cp0150', 'ck0013', 'CMND_BTL_SET_AVATER'),
    ('btl0007', 'RunEv44060060', 14, 'para_cp0149', 'ck0009', 'CMND_BTL_SET_AVATER'),
    ('btl0007', 'RunEv44060060', 60, 'para_cp0008', 'ck0001', 'CMND_BTL_SET_AVATER'),
    ('btl0011', 'RunEv44090030', 41, 'para_cp0230', 'ck0019', 'CMND_BTL_SET_AVATER'),
    ('btl0011', 'RunEv44090050', 72, 'para_cp0016', 'ck0005', 'CMND_BTL_SET_AVATER'),
    ('btl0016', 'RunEv44120030', 13, 'para_cp0321', 'ck0023', 'CMND_BTL_SET_AVATER'),
    ('btl0002', 'RunEv44010101', 19, 'para_cp0010', 'ck0002', 'CMND_BTL_SET_AVATER'),
    ('btl0010', 'RunEv44080052', 14, 'para_cp0210', 'ck0008', 'CMND_BTL_SET_AVATER'),
    ('btl0019', 'ev44130070_Run', 17, 'para_cp0370', 'ck0024', 'CMND_BTL_SET_AVATER'),
    ('btl0019', 'ev44130070_Run', 23, 'para_cp0350', 'ck0024', 'CMND_BTL_SET_AVATER'),
    ('btl0004', 'RunEv44030018', 112, 'para_cp0086', 'ck0012', 'CMND_BTL_SET_AVATER'),
    ('btl0004', 'RunEv44030040', 17, 'para_cp0009', 'ck0003', 'CMND_BTL_SET_AVATER'),
    ('btl0004', 'RunEv44030040', 22, 'para_cp0086', 'ck0012', 'CMND_BTL_SET_AVATER'),
    ('ev44030040', 'Event', 1625, 'para_cp0009', 'ck0003', 'RunAttachAvatarOnlyMsg'),
    ('ev44110050', 'Event', 1607, 'para_cp0008', 'ck0030', 'RunAttachAvatarOnlyMsg'),
    ('ev44060060', 'Event', 1270, 'para_cp0008', 'ck0001', 'RunAttachAvatar'),
    ('ev42111300', 'Event', 2761, 'para_cp0005', 'ck0025', 'RunAttachAvatar'),
    ('ev44090050', 'Event', 420, 'para_cp0016', 'ck0005', 'RunAttachAvatar'),
    ('ev44110040', 'Event', 739, 'para_cp0008', 'ck0033', 'RunEvolutionAvatar'),
]


def _identifier_map(prefixes, hashes):
    """Reverse only known identifier families and reject unknown/colliding IDs."""
    found = {}
    for prefix in prefixes:
        for number in range(10000):
            name = '%s%04d' % (prefix, number)
            key = zlib.crc32(name.encode('ascii')) & 0xFFFFFFFF
            if key in hashes:
                if key in found and found[key] != name:
                    raise RandomizerError('Ambiguous game identifier hash.')
                found[key] = name
    missing = hashes - found.keys()
    if missing:
        raise RandomizerError('Unsupported game identifiers: %s' % ', '.join('%08X' % h for h in sorted(missing)))
    return found


def plan_story_repair(arc, cfg=None):
    """Read authoritative tables and build an all-or-nothing script patch plan.

    cfg can be the freshly randomized in-memory tables. No Randomizer is created
    in repair mode and no table, random generator or saved game is written.
    """
    cfg = cfg or {}
    param = cfg.get('param') or CfgBin(arc.read(PATHS['param']))
    stable = cfg.get('stable') or CfgBin(arc.read(PATHS['stable']))
    rows = stable.by_tag('SKILL_TABLE')
    people = param.by_tag('CHARA_PARAM_INFO')
    if not people or not rows or any(e.n != 42 for e in people) or any(e.n != 4 for e in rows):
        raise RandomizerError('Unsupported story table structure.')
    characters = {}
    for e in people:
        key = param.get(e, 0)  # record identifier, NOT chara_base key (column 1)
        if key in characters:
            raise RandomizerError('Duplicate character parameter record.')
        characters[key] = e
    def person(name):
        key = zlib.crc32(name.encode('ascii')) & 0xFFFFFFFF
        if key not in characters:
            raise RandomizerError('Missing story character %s.' % name)
        return characters[key]
    skill_targets = {}
    for script, function, pc, character, slot in STORY_SKILLS + STORY_BATTLE_SKILLS:
        row = param.get(person(character), 18) + slot
        if not 0 <= row < len(rows):
            raise RandomizerError('Story skill row is out of bounds.')
        skill_targets[(script, function, pc)] = stable.get(rows[row], 2)
    skill_ids = _identifier_map(['wh'+c for c in 'abcdefghijklmnopqrstuvwxyz'], set(skill_targets.values()))
    spirit_hashes = {param.get(person(c), 41) for _, _, _, c, _, _ in STORY_SPIRITS}
    spirit_ids = _identifier_map(['ck'], spirit_hashes - {0})
    sites = collections.defaultdict(list)
    for script, fn, pc, character, slot in STORY_SKILLS:
        sites[script].append((fn, pc, character, skill_ids[skill_targets[(script, fn, pc)]], 'unlock'))
    for script, fn, pc, character, slot in STORY_BATTLE_SKILLS:
        sites[script].append((fn, pc, character, skill_ids[skill_targets[(script, fn, pc)]], 'learning'))
    for script, fn, pc, character, original, command in STORY_SPIRITS:
        chosen = param.get(person(character), 41)
        if not chosen:
            # No table spirit: do not invent one or erase a scripted encounter.
            target = original
        else:
            target = spirit_ids[chosen]
            # Preserve vanilla Arion evolution/fusion when the table is Pegasus.
            if character == 'para_cp0008' and target == 'ck0001': target = original
        sites[script].append((fn, pc, character, target, command))
    paths = {}
    for path in arc.entries:
        if path.endswith('.nutb'):
            basename = path.rsplit('/', 1)[-1][:-5]
            if basename in sites:
                if basename in paths: raise RandomizerError('Ambiguous story script %s.' % basename)
                paths[basename] = path
    missing = sites.keys() - paths.keys()
    if missing: raise RandomizerError('Missing story scripts: %s' % ', '.join(sorted(missing)))
    patches, report = {}, []
    for script, requested in sorted(sites.items()):
        path = paths[script]; old = arc.read(path); nut = NutScript(old)
        functions = {}
        for f in nut.functions:
            name = nut.text(f['name'])
            if name not in {item[0] for item in requested}: continue
            if name in functions: raise RandomizerError('Ambiguous Squirrel function %s.' % name)
            functions[name] = f
        for fn, pc, character, target, kind in requested:
            f = functions.get(fn)
            if f is None: raise RandomizerError('Missing function %s in %s.' % (fn, script))
            matched = [c for c in nut.calls(f) if c[0] == pc]
            if len(matched) != 1:
                raise RandomizerError('Unsupported call site %s/%s:%d.' % (script, fn, pc))
            _, command, prep, args = matched[0]
            if kind == 'unlock': allowed, ci, ti = ('RunUnlockSkill', 'RunUnlockSkillUseMsg', 'CMND_SKILL_UNLOCK'), 0, 1
            elif kind == 'learning': allowed, ci, ti = ('CMND_BTL_LEARNING_SKILL',), 1, 2
            else:
                allowed, ci, ti = (kind,), 0, 1
                # These repaired persistent assignments replace evolution/message wrappers.
                if kind in ('RunEvolutionAvatar', 'RunEvolutionAvatarOnlyMsg', 'RunAttachAvatarOnlyMsg'):
                    allowed += ('RunAttachAvatar',)
            if command not in allowed or len(args) <= ti or args[ci] is None or args[ci][0] != character or args[ti] is None:
                raise RandomizerError('Unrecognized story call %s/%s:%d.' % (script, fn, pc))
            old_target, load_pc, field = args[ti]
            if not isinstance(old_target, str) or not old_target.startswith(('wh', 'ck')) or pc - load_pc > 3:
                raise RandomizerError('Unsupported dynamic story argument.')
            # Direct argument loads must not feed any intervening operation.
            if any(i[1] not in (0, 1, 2, 3, 4, 22) for i in f['instructions'][load_pc+1:pc]):
                raise RandomizerError('Story argument has a nonlocal producer.')
            new_command = command
            if kind.startswith('Run') and kind != 'RunAttachAvatar':
                original = next(o for s, n, p, c, o, k in STORY_SPIRITS if (s, n, p) == (script, fn, pc))
                if target != original or command == 'RunAttachAvatar':
                    new_command = 'RunAttachAvatar'
            if old_target != target or command != new_command:
                nut.literal_operand(f, load_pc, field, target)
                if command != new_command: nut.literal_operand(f, prep, 0, new_command)
                report.append('%s/%s:%d %s(%s, %s) -> %s(%s, %s)' %
                              (script, fn, pc, command, character, old_target, new_command, character, target))
        new = nut.tobytes()
        if new != old: patches[path] = new
    return patches, report


def repair_archive(src, dst, log=print):
    """Repair a verified separate copy; never rewrite cfg.bin or overwrite output."""
    import tempfile
    src, dst = os.path.abspath(src), os.path.abspath(dst)
    if os.path.realpath(src) == os.path.realpath(dst) or (os.path.exists(dst) and os.path.samefile(src, dst)):
        raise RandomizerError('Repair requires a separate output file.')
    if os.path.lexists(dst):
        raise RandomizerError('Repair output already exists; choose a new filename.')
    if not os.path.isfile(src): raise RandomizerError('Input archive does not exist.')
    arc = Arc0(src)
    try:
        patches, report = plan_story_repair(arc)
        # All cfg.bin payloads, including unrelated tables, must remain identical.
        tables = {p: hashlib.sha256(arc.read(p)).digest() for p in arc.entries if p.endswith('.cfg.bin')}
    finally:
        arc.close()
    parent = os.path.dirname(dst)
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.iego-repair-', suffix='.fa', dir=parent)
    os.close(fd)
    try:
        shutil.copyfile(src, temporary)
        out = Arc0(temporary, writable=True)
        try:
            for path, data in patches.items(): out.write(path, data)
            out.mm.flush()
            for path, checksum in tables.items():
                if hashlib.sha256(out.read(path)).digest() != checksum:
                    raise RandomizerError('Repair changed a table: %s' % path)
            again, _ = plan_story_repair(out)
            if again: raise RandomizerError('Repair verification failed: not idempotent.')
        finally:
            out.close()
        # Exclusive publication prevents races from replacing existing user files.
        with open(dst, 'xb') as result, open(temporary, 'rb') as source:
            try: shutil.copyfileobj(source, result)
            except BaseException:
                result.close(); os.unlink(dst); raise
        for line in report: log(line)
        log('Repair complete: %d scripts changed; %d cfg.bin tables preserved. Output: %s' % (len(patches), len(tables), dst))
        log('Saved games are unchanged; completed events are not replayed. Gameplay has not been verified.')
        return dst, report
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


# =============================================================================
# 7. Shared entry point (CLI + GUI)
# =============================================================================

def randomize_file(src, dst, opt, log=print, dry_run=False, inplace=False, write_log=True):
    src = os.path.abspath(src)
    if opt.seed is None or opt.seed == '':
        opt.seed = random.SystemRandom().randint(1, 2 ** 31 - 1)
    if inplace:
        dst = src
    dst = os.path.abspath(dst)
    if not dry_run:
        if inplace:
            bak = src + '.bak'
            if not os.path.exists(bak):
                log('Backup -> %s' % bak)
                shutil.copyfile(src, bak)
        elif dst != src:
            os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
            log('Copying %s -> %s (this may take a few seconds)...' % (os.path.basename(src), dst))
            shutil.copyfile(src, dst)
    work = src if dry_run else dst
    t0 = time.time()
    arc = Arc0(work, writable=not dry_run)
    try:
        rz = Randomizer(arc, opt, log)
        log('Archive loaded: %d files, %d players with data.' % (len(arc.entries), len(rz.active)))
        rz.run()
        script_patches, script_report = plan_story_repair(arc, rz.cfg)
        rz.spoiler.append(('Story script repair', script_report or ['Scripts already match the tables.']))
        log('Story repair: %d calls in %d scripts%s.' % (len(script_report), len(script_patches), ' (preview)' if dry_run else ''))
        if dry_run:
            log('Dry run complete (the game archive was not modified).')
        else:
            n = rz.commit()
            arc.mm.flush()
            rz.verify()
            for path, data in script_patches.items(): arc.write(path, data)
            arc.mm.flush()
            if plan_story_repair(arc)[0]:
                raise RandomizerError('Automatic story repair is not idempotent.')
            log('Wrote %d data files; verification OK.' % n)
        spoiler_path = None
        if write_log:
            spoiler_path = (dst if not dry_run else src) + '.spoiler_%s.txt' % opt.seed
            rz.write_spoiler(spoiler_path)
            log('Full change log: %s' % spoiler_path)
        log('Done in %.1f s. Seed: %s' % (time.time() - t0, opt.seed))
        return dst, spoiler_path
    finally:
        arc.close()


def dump_characters(path, lang='en'):
    'Dump raw CHARA_BASE_INFO columns (for adjusting B_IDENTITY).'
    arc = Arc0(path)
    try:
        rz = Randomizer(arc, Options(lang=lang), lambda *_: None)
        print("idx  %-28s %s" % ("nombre", ' '.join('c%-7d' % j for j in range(12))))
        for c in rz.chars:
            vals = [rz.base.get(c['b'], j) for j in range(12)]
            print("%3d  %-28s %s" % (c['idx'], c['name'], ' '.join('%08X' % v for v in vals)))
    finally:
        arc.close()


def build_parser():
    o = Options()
    ap = argparse.ArgumentParser(
        description='Inazuma Eleven GO Light/Shadow randomizer (ie_a.fa). '
                    'Launches the GUI when no arguments are supplied.')
    ap.add_argument('input', nargs='?', help='Original ie_a.fa')
    ap.add_argument('-o', '--output', help='Output ie_a.fa (default: ./randomized/ie_a.fa)')
    ap.add_argument('--seed', help='Seed (same input, seed and options produce the same result)')
    ap.add_argument('--lang', default=o.lang, help='Language of game names in logs (en, es, de, fr, it)')
    ap.add_argument('--gender', choices=['off', 'random'], default=o.gender)
    ap.add_argument('--female-chance', type=float, default=o.female_chance,
                    help='Probability of assigning female gender to a player (0-1)')
    ap.add_argument('--element', choices=['off', 'random'], default=o.element,
                    help='Element: Wind, Wood, Fire, Earth')
    ap.add_argument('--position', choices=['off', 'random'], default=o.position)
    ap.add_argument('--stats', choices=['off', 'shuffle', 'balanced', 'chaos'], default=o.stats)
    ap.add_argument('--growth', choices=['off', 'balanced', 'chaos'], default=o.growth)
    ap.add_argument('--skills', choices=['off', 'balanced', 'chaos'], default=o.skills)
    ap.add_argument('--spirits', choices=['off', 'random'], default=o.spirits)
    ap.add_argument('--spirit-any-element', action='store_true',
                    help='Allow Fighting Spirits with a different element from the player')
    ap.add_argument('--equipment', choices=['off', 'random'], default=o.equipment)
    ap.add_argument('--consumables', choices=['off', 'shuffle'], default=o.consumables)
    ap.add_argument('--prices', choices=['off', 'random'], default=o.prices)
    ap.add_argument('--shops', choices=['off', 'random'], default=o.shops)
    ap.add_argument('--spirit-stats', choices=['off', 'shuffle'], default=o.spirit_stats)
    ap.add_argument('--characters', choices=['off', 'shuffle'], default=o.characters,
                    help='Shuffle identities: another character can appear in the protagonist slot')
    ap.add_argument('--chars-keep-gender', action='store_true',
                    help='With --characters, shuffle identities only within the same gender')
    ap.add_argument('--dump-chars', action='store_true',
                    help='Print raw chara_base columns and exit')
    ap.add_argument('--dry-run', action='store_true', help='Simulate and generate a change log without modifying the .fa archive')
    ap.add_argument('--inplace', action='store_true', help='Modify the original file in place (creates a .bak backup)')
    ap.add_argument('--no-log', action='store_true', help='Do not generate a change log')
    ap.add_argument('--repair', action='store_true', help='Repair story scripts in an existing archive; requires a separate output and no seed')
    ap.add_argument('--gui', action='store_true', help='Open the GUI')
    return ap


def opts_from_args(a):
    return Options(seed=a.seed, lang=a.lang, gender=a.gender, female_chance=a.female_chance,
                   element=a.element, position=a.position, stats=a.stats, growth=a.growth,
                   skills=a.skills, spirits=a.spirits, spirit_match_element=not a.spirit_any_element,
                   equipment=a.equipment, consumables=a.consumables, prices=a.prices,
                   shops=a.shops, spirit_stats=a.spirit_stats,
                   characters=a.characters, char_keep_gender=a.chars_keep_gender)


# =============================================================================
# 8. Graphical interface (tkinter)
# =============================================================================

def run_gui():
    import threading
    import queue
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title('IE GO Light/Shadow · Randomizer v%s' % VERSION)
    root.geometry("760x840")
    o = Options()
    ui_queue = queue.Queue()

    def drain_ui_queue():
        try:
            while True:
                ui_queue.get_nowait()()
        except queue.Empty:
            pass
        root.after(50, drain_ui_queue)

    root.after(50, drain_ui_queue)

    src_v, dst_v, seed_v = tk.StringVar(), tk.StringVar(), tk.StringVar()
    vars_ = {}

    def row(parent, label, r):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky='w', padx=4, pady=2)

    notebook = ttk.Notebook(root)
    notebook.pack(fill='both', expand=True)
    frm = ttk.Frame(notebook, padding=8)
    notebook.add(frm, text='Randomize')

    ttk.Label(frm, text='Original ie_a.fa:').grid(row=0, column=0, sticky='w')
    ttk.Entry(frm, textvariable=src_v, width=60).grid(row=0, column=1, sticky='we')

    def pick_src():
        p = filedialog.askopenfilename(title='Select ie_a.fa', filetypes=[("ie_a.fa", "*.fa"), ('All files', "*")])
        if p:
            src_v.set(p)
            if not dst_v.get():
                dst_v.set(os.path.join(os.path.dirname(p), 'randomized', 'ie_a.fa'))
    ttk.Button(frm, text='Browse…', command=pick_src).grid(row=0, column=2, padx=4)

    ttk.Label(frm, text='Save as:').grid(row=1, column=0, sticky='w')
    ttk.Entry(frm, textvariable=dst_v, width=60).grid(row=1, column=1, sticky='we')
    ttk.Button(frm, text='Browse…', command=lambda: dst_v.set(
        filedialog.asksaveasfilename(defaultextension=".fa", initialfile="ie_a.fa") or dst_v.get())
    ).grid(row=1, column=2, padx=4)

    ttk.Label(frm, text='Seed (empty = random):').grid(row=2, column=0, sticky='w')
    ttk.Entry(frm, textvariable=seed_v, width=20).grid(row=2, column=1, sticky='w')

    box = ttk.LabelFrame(frm, text='What to randomize', padding=8)
    box.grid(row=3, column=0, columnspan=3, sticky='we', pady=8)

    def combo(label, key, values, r, default):
        ttk.Label(box, text=label).grid(row=r, column=0, sticky='w', pady=2)
        v = tk.StringVar(value=default)
        ttk.Combobox(box, textvariable=v, values=values, state='readonly', width=12).grid(row=r, column=1, sticky='w')
        vars_[key] = v

    combo('Gender', 'gender', ['off', 'random'], 0, o.gender)
    combo('Element', 'element', ['off', 'random'], 1, o.element)
    combo('Position', 'position', ['off', 'random'], 2, o.position)
    combo('Stats', 'stats', ['off', 'shuffle', 'balanced', 'chaos'], 3, o.stats)
    combo('Growth', 'growth', ['off', 'balanced', 'chaos'], 4, o.growth)
    combo('Skills', 'skills', ['off', 'balanced', 'chaos'], 5, o.skills)
    combo('Fighting Spirits', 'spirits', ['off', 'random'], 6, o.spirits)
    combo('Equipment', 'equipment', ['off', 'random'], 7, o.equipment)
    combo('Food / drink', 'consumables', ['off', 'shuffle'], 8, o.consumables)
    combo('Prices', 'prices', ['off', 'random'], 9, o.prices)
    combo('Shops', 'shops', ['off', 'random'], 10, o.shops)
    combo('Fighting Spirit stats', 'spirit_stats', ['off', 'shuffle'], 11, o.spirit_stats)
    combo('Character identities', 'characters', ['off', 'shuffle'], 12, o.characters)
    keep_v = tk.BooleanVar(value=False)
    ttk.Checkbutton(box, text='Shuffle character identities only within the same gender',
                    variable=keep_v).grid(row=13, column=0, columnspan=2, sticky='w', pady=2)
    match_v = tk.BooleanVar(value=True)
    ttk.Checkbutton(box, text='Match Fighting Spirit element to the player',
                    variable=match_v).grid(row=14, column=0, columnspan=2, sticky='w', pady=2)
    ttk.Label(box, text='% female:').grid(row=0, column=2, padx=(20, 4))
    fem_v = tk.StringVar(value="50")
    ttk.Entry(box, textvariable=fem_v, width=5).grid(row=0, column=3)

    logbox = tk.Text(frm, height=14, wrap='word')
    logbox.grid(row=5, column=0, columnspan=3, sticky='nsew', pady=6)
    frm.rowconfigure(5, weight=1)
    frm.columnconfigure(1, weight=1)

    def log(msg):
        ui_queue.put(lambda: (logbox.insert('end', msg + "\n"), logbox.see('end')))

    btn = ttk.Button(frm, text='Randomize')
    btn.grid(row=4, column=0, columnspan=3, pady=4)

    def work(dry, source, destination, opt):
        try:
            randomize_file(source, destination, opt, log, dry_run=dry)
            ui_queue.put(lambda: messagebox.showinfo('Done', 'Process complete.\nSeed: %s' % opt.seed))
        except Exception as ex:  # noqa
            log('ERROR: %s' % ex)
        finally:
            ui_queue.put(lambda: btn.config(state='normal'))

    def go(dry=False):
        # Tk variables belong to the UI thread, including on Tcl/Tk 9.
        try:
            source = src_v.get().strip()
            if not source or not os.path.isfile(source):
                raise RandomizerError('Select the original ie_a.fa.')
            destination = dst_v.get().strip() or os.path.join(os.path.dirname(source), 'randomized', 'ie_a.fa')
            opt = Options(seed=(seed_v.get().strip() or None),
                          spirit_match_element=match_v.get(), char_keep_gender=keep_v.get(),
                          female_chance=max(0.0, min(1.0, float(fem_v.get() or 50) / 100.0)),
                          **{k: v.get() for k, v in vars_.items()})
        except Exception as ex:
            messagebox.showerror('Randomizer', str(ex))
            return
        btn.config(state='disabled')
        logbox.delete('1.0', 'end')
        threading.Thread(target=work, args=(dry, source, destination, opt), daemon=True).start()

    btn.config(command=go)
    ttk.Button(frm, text='Dry run (log only)', command=lambda: go(True)).grid(row=6, column=0, columnspan=3)
    repair_frame = ttk.Frame(notebook, padding=8)
    notebook.add(repair_frame, text='Repair existing archive')
    repair_src, repair_dst = tk.StringVar(), tk.StringVar()
    ttk.Label(repair_frame, text='Repair story scripts using the existing tables. No seed or re-randomization.',
              wraplength=700).grid(row=0, column=0, columnspan=3, sticky='w', pady=8)
    ttk.Label(repair_frame, text='Existing ie_a.fa:').grid(row=1, column=0, sticky='w')
    ttk.Entry(repair_frame, textvariable=repair_src, width=60).grid(row=1, column=1, sticky='we')

    def pick_repair_src():
        path = filedialog.askopenfilename(title='Select existing ie_a.fa', filetypes=[('Game archive', '*.fa'), ('All files', '*')])
        if path:
            repair_src.set(path)
            repair_dst.set(os.path.join(os.path.dirname(path), 'repaired', 'ie_a.fa'))

    ttk.Button(repair_frame, text='Browse…', command=pick_repair_src).grid(row=1, column=2, padx=4)
    ttk.Label(repair_frame, text='Separate output:').grid(row=2, column=0, sticky='w')
    ttk.Entry(repair_frame, textvariable=repair_dst, width=60).grid(row=2, column=1, sticky='we')
    ttk.Button(repair_frame, text='Browse…', command=lambda: repair_dst.set(
        filedialog.asksaveasfilename(defaultextension='.fa', initialfile='ie_a.fa') or repair_dst.get())
    ).grid(row=2, column=2, padx=4)
    ttk.Label(repair_frame, text='Creates a separate copy and preserves every .cfg.bin table. Existing output files are refused. '
              'Saved games are not modified and completed events are not replayed. Gameplay and cutscene compatibility remain unverified.',
              wraplength=700).grid(row=3, column=0, columnspan=3, sticky='w', pady=8)
    repair_log = tk.Text(repair_frame, height=24, wrap='word')
    repair_log.grid(row=5, column=0, columnspan=3, sticky='nsew', pady=8)
    repair_frame.rowconfigure(5, weight=1)
    repair_frame.columnconfigure(1, weight=1)

    def repair_log_line(message):
        ui_queue.put(lambda text=message: (repair_log.insert('end', text + '\n'), repair_log.see('end')))

    def repair_go():
        # Read Tk variables on the main thread before starting file work.
        source, destination = repair_src.get().strip(), repair_dst.get().strip()
        if not source or not destination:
            messagebox.showerror('Repair', 'Select an input and a separate output file.')
            return
        repair_button.config(state='disabled')
        repair_log.delete('1.0', 'end')

        def repair_work():
            try:
                repair_archive(source, destination, repair_log_line)
                ui_queue.put(lambda: messagebox.showinfo('Repair complete', 'Repaired copy created. All cfg.bin tables were preserved.'))
            except Exception as ex:
                repair_log_line('ERROR: %s' % ex)
            finally:
                ui_queue.put(lambda: repair_button.config(state='normal'))
        threading.Thread(target=repair_work, daemon=True).start()

    repair_button = ttk.Button(repair_frame, text='Repair scripts into a separate copy', command=repair_go)
    repair_button.grid(row=4, column=0, columnspan=3, pady=8)
    root.mainloop()


# =============================================================================
#  9. main
# =============================================================================

def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.repair:
        if not a.input or not a.output:
            ap.error('--repair requires an input archive and --output')
        supplied = argv if argv is not None else sys.argv[1:]
        for token in supplied:
            if token.startswith('--') and token.split('=')[0] not in ('--repair', '--output'):
                ap.error('--repair does not accept randomization, seed, GUI, or in-place options')
        try:
            repair_archive(a.input, a.output)
        except (RandomizerError, OSError, ValueError, struct.error) as ex:
            print('ERROR:', ex)
            return 2
        return 0
    if a.gui or not a.input:
        try:
            run_gui()
            return 0
        except ImportError:
            ap.print_help()
            print('\n(tkinter is unavailable: use the command line.)')
            return 1
    if a.dump_chars:
        dump_characters(a.input, a.lang)
        return 0
    try:
        out = a.output or os.path.join(os.path.dirname(os.path.abspath(a.input)), 'randomized', 'ie_a.fa')
        randomize_file(a.input, out, opts_from_args(a), dry_run=a.dry_run, inplace=a.inplace,
                       write_log=not a.no_log)
    except (RandomizerError, OSError, ValueError, struct.error) as ex:
        print("ERROR:", ex)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
