#!/usr/bin/env python3
"""T6 evidence: which non-ASCII chars used in UI string literals are covered by
egui 0.29.1's default fonts, in the fallback order of each FontFamily
(epaint-0.29.1/src/text/fonts.rs:298-314). Pure-stdlib cmap parser (formats 4, 12).
Usage: python3 glyph_coverage.py <epaint_default_fonts/fonts dir> <src files...>"""
import struct, sys, re, unicodedata, glob, os

def cmap(path):
    d = open(path, 'rb').read()
    n = struct.unpack('>H', d[4:6])[0]
    tables = {}
    for i in range(n):
        tag, _, off, ln = struct.unpack('>4sIII', d[12+16*i:28+16*i])
        tables[tag] = off
    off = tables[b'cmap']
    nsub = struct.unpack('>H', d[off+2:off+4])[0]
    cps = set()
    for i in range(nsub):
        pid, eid, so = struct.unpack('>HHI', d[off+4+8*i:off+12+8*i])
        s = off + so
        fmt = struct.unpack('>H', d[s:s+2])[0]
        if fmt == 4:
            segx2 = struct.unpack('>H', d[s+6:s+8])[0]; seg = segx2 // 2
            ends = struct.unpack('>%dH' % seg, d[s+14:s+14+segx2])
            starts = struct.unpack('>%dH' % seg, d[s+16+segx2:s+16+2*segx2])
            deltas = struct.unpack('>%dh' % seg, d[s+16+2*segx2:s+16+3*segx2])
            ro_off = s+16+3*segx2
            ros = struct.unpack('>%dH' % seg, d[ro_off:ro_off+segx2])
            for k in range(seg):
                for c in range(starts[k], ends[k]+1):
                    if c == 0xFFFF: continue
                    if ros[k] == 0:
                        g = (c + deltas[k]) & 0xFFFF
                    else:
                        a = ro_off + 2*k + ros[k] + 2*(c - starts[k])
                        g = struct.unpack('>H', d[a:a+2])[0]
                        if g: g = (g + deltas[k]) & 0xFFFF
                    if g: cps.add(c)
        elif fmt == 12:
            ng = struct.unpack('>I', d[s+12:s+16])[0]
            for k in range(ng):
                a, b, g0 = struct.unpack('>III', d[s+16+12*k:s+28+12*k])
                for c in range(a, b+1):
                    if g0 + (c - a): cps.add(c)
    return cps

fontdir = sys.argv[1]
fonts = {n: cmap(os.path.join(fontdir, f)) for n, f in [
    ('Ubuntu-Light', 'Ubuntu-Light.ttf'), ('Hack', 'Hack-Regular.ttf'),
    ('NotoEmoji-Regular', 'NotoEmoji-Regular.ttf'), ('emoji-icon-font', 'emoji-icon-font.ttf')]}
families = {'Proportional': ['Ubuntu-Light', 'NotoEmoji-Regular', 'emoji-icon-font'],
            'Monospace': ['Hack', 'Ubuntu-Light', 'NotoEmoji-Regular', 'emoji-icon-font']}
uses = {}
for f in sys.argv[2:]:
    for ln, line in enumerate(open(f, encoding='utf-8'), 1):
        s = line.split('//')[0] if '"' not in line else line
        for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', s):
            for ch in m.group(1):
                if ord(ch) > 0x7F:
                    uses.setdefault(ch, []).append(f"{os.path.basename(f)}:{ln}")
        for m in re.finditer(r"'(.)'", s):
            ch = m.group(1)
            if ord(ch) > 0x7F:
                uses.setdefault(ch, []).append(f"{os.path.basename(f)}:{ln}")
print("| Char | Code point | Name | Ubuntu-Light | Hack | NotoEmoji | emoji-icon | Proportional resolves to | Call sites |")
print("|---|---|---|---|---|---|---|---|---|")
for ch in sorted(uses, key=ord):
    cov = {n: (ord(ch) in c) for n, c in fonts.items()}
    prop = next((n for n in families['Proportional'] if cov[n]), 'NONE (tofu box)')
    y = lambda b: 'yes' if b else '—'
    print(f"| {ch} | U+{ord(ch):04X} | {unicodedata.name(ch, '?')} | {y(cov['Ubuntu-Light'])} | {y(cov['Hack'])} | {y(cov['NotoEmoji-Regular'])} | {y(cov['emoji-icon-font'])} | {prop} | {', '.join(sorted(set(uses[ch])))} |")
