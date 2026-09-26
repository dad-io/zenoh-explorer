#!/usr/bin/env python3
"""T5 WCAG 2.x contrast calculator for Zenoh Explorer (current) vs Snow White (proposed).

Formula (WCAG 2.x, relative luminance, sRGB):
  c = channel/255;  c_lin = c/12.92 if c <= 0.04045 else ((c+0.055)/1.055)**2.4
  (WCAG 2.x text says 0.03928; no 8-bit value lies between the two thresholds, so results are identical)
  L = 0.2126 R_lin + 0.7152 G_lin + 0.0722 B_lin
  ratio = (L_lighter + 0.05) / (L_darker + 0.05)

Compositing. egui_glow 0.29.1 blends with (ONE, ONE_MINUS_SRC_ALPHA) and FRAMEBUFFER_SRGB disabled
(egui_glow/src/painter.rs:319-333); the fragment shader outputs vertex colour in gamma space
(egui_glow/src/shader/fragment.glsl:60-68). So blending happens on gamma-encoded u8 values, and the 8-bit
target clamps:
  from_rgba_premultiplied(r,g,b,a) over dst:  out = min(255, src + dst*(1 - a/255))
    (ExplorerColors' SELECTED_BACKGROUND values have rgb > a, i.e. they are NOT valid premultiplied
     colours; the formula then adds light and clamps. Verified against the T1 captures, see pixels.txt.)
  from_rgba_unmultiplied(r,g,b,a) first premultiplies in LINEAR light (ecolor-0.29.1/src/color32.rs:102-126):
    p = gamma_u8_from_linear_f32(linear_f32_from_gamma_u8(c) * a/255)   (ecolor/src/lib.rs:63-101)
  and is then blended like any premultiplied colour:  out = p + dst*(1 - a/255)
Usage:  python3 contrast.py   (no dependencies)
"""
def lin(c):
    c = c / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
def L(rgb):
    r, g, b = (lin(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
def ratio(a, b):
    la, lb = sorted((L(a), L(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)
def hx(s):
    s = s.lstrip('#'); return tuple(int(s[i:i+2], 16) for i in (0, 2, 4))
def premul_over(src, a, dst):
    return tuple(min(255, round(s + d * (1 - a / 255))) for s, d in zip(src, dst))
def _lin_from_gamma_u8(v):   # ecolor lib.rs:63
    return v / 3294.6 if v <= 10 else ((v + 14.025) / 269.025) ** 2.4
def _gamma_u8_from_lin(l):    # ecolor lib.rs:80, fast_round = floor(x + 0.5)
    if l <= 0: return 0
    if l <= 0.0031308: return int(3294.6 * l + 0.5)
    if l <= 1: return int(269.025 * l ** (1 / 2.4) - 14.025 + 0.5)
    return 255
def egui_unmultiplied(src, a):  # Color32::from_rgba_unmultiplied -> premultiplied rgb
    if a == 255: return tuple(src)
    return tuple(_gamma_u8_from_lin(_lin_from_gamma_u8(v) * a / 255) for v in src)
def unmul_over(src, a, dst):
    return premul_over(egui_unmultiplied(src, a), a, dst)
def fmt(c): return '#%02x%02x%02x' % c
def verdict(r, large=False, ui=False):
    if ui: return 'pass 3:1' if r >= 3 else 'FAIL 3:1'
    if r >= 7: return 'AAA'
    if r >= 4.5: return 'AA'
    if r >= 3: return 'AA-large only'
    return 'FAIL'

# ---- current constants (src/colors.rs) and hard-coded values (src/app.rs) ----
C = dict(
 BACKGROUND=(248,248,248), CARD_BACKGROUND=(255,255,255), SIDEBAR=(242,242,247),
 PRIMARY=(0,122,255), PRIMARY_HOVER=(0,102,217), SUCCESS=(52,199,89), WARNING=(255,149,0),
 ERROR=(255,59,48), TEXT_PRIMARY=(28,28,30), TEXT_SECONDARY=(60,60,67), TEXT_TERTIARY=(99,99,102),
 SURFACE=(250,250,250),
 DARK_BACKGROUND=(45,45,45), DARK_CARD_BACKGROUND=(75,75,75), DARK_SIDEBAR=(55,55,55),
 DARK_PRIMARY=(10,132,255), DARK_PRIMARY_HOVER=(64,156,255), DARK_SUCCESS=(48,209,88),
 DARK_WARNING=(255,159,10), DARK_ERROR=(255,69,58), DARK_TEXT_PRIMARY=(255,255,255),
 DARK_TEXT_SECONDARY=(200,200,200), DARK_TEXT_TERTIARY=(180,180,180), DARK_SURFACE=(60,60,60),
 WHITE=(255,255,255), CODE_L=(240,240,240), CODE_D=(30,30,30),
)
SEL_L = premul_over(C['PRIMARY'], 25, C['CARD_BACKGROUND'])          # SELECTED_BACKGROUND (0,122,255,25)
SEL_D = premul_over(C['DARK_PRIMARY'], 40, C['DARK_CARD_BACKGROUND']) # DARK_SELECTED_BACKGROUND (10,132,255,40)
PROG_L = premul_over(C['PRIMARY'], 25, C['SURFACE'])                  # progress fill over extreme_bg
PROG_D = premul_over(C['DARK_PRIMARY'], 40, C['DARK_SURFACE'])
SEP_L = premul_over((0,0,0), 26, C['CARD_BACKGROUND'])                # SEPARATOR (unused)
SEP_D = premul_over((255,255,255), 30, C['DARK_CARD_BACKGROUND'])    # DARK_SEPARATOR (unused)
PULSE_L = unmul_over(C['ERROR'], int(255*0.7), C['BACKGROUND'])     # worker pulse trough (alpha 0.70)
PULSE_D = unmul_over(C['ERROR'], int(255*0.7), C['DARK_BACKGROUND'])   # alpha = (255.0*0.70) as u8 = 178
LEAD_L = (unmul_over(C['TEXT_TERTIARY'], 64, C['CARD_BACKGROUND']), unmul_over(C['TEXT_TERTIARY'], 100, C['CARD_BACKGROUND']))
LEAD_D = (unmul_over(C['DARK_TEXT_TERTIARY'], 64, C['DARK_CARD_BACKGROUND']), unmul_over(C['DARK_TEXT_TERTIARY'], 100, C['DARK_CARD_BACKGROUND']))

print('## Composited premultiplied / alpha colours')
for n, v in [('SELECTED_BACKGROUND over CARD_BACKGROUND', SEL_L), ('DARK_SELECTED_BACKGROUND over DARK_CARD_BACKGROUND', SEL_D),
             ('progress fill (SELECTED_BACKGROUND over SURFACE)', PROG_L), ('progress fill dark (over DARK_SURFACE)', PROG_D),
             ('SEPARATOR over CARD_BACKGROUND (unused)', SEP_L), ('DARK_SEPARATOR over DARK_CARD_BACKGROUND (unused)', SEP_D),
             ('ERROR a=178 over BACKGROUND (pulse trough)', PULSE_L), ('ERROR a=178 over DARK_BACKGROUND', PULSE_D),
             ('leader line TEXT_TERTIARY a=64 / a=100 over CARD_BACKGROUND', LEAD_L),
             ('leader line DARK_TEXT_TERTIARY a=64 / a=100 over DARK_CARD_BACKGROUND', LEAD_D)]:
    print(f'- {n}: ' + (' / '.join(f'{x} {fmt(x)}' for x in v) if isinstance(v[0], tuple) else f'{v} {fmt(v)}'))

cur = [
 # (theme, pair, fg, bg, kind)
 ('light','Title / body text: TEXT_PRIMARY on BACKGROUND (header row)', C['TEXT_PRIMARY'], C['BACKGROUND'], 't'),
 ('light','Body text: TEXT_PRIMARY on CARD_BACKGROUND (tree, detail, toolbar)', C['TEXT_PRIMARY'], C['CARD_BACKGROUND'], 't'),
 ('light','TEXT_SECONDARY on CARD_BACKGROUND', C['TEXT_SECONDARY'], C['CARD_BACKGROUND'], 't'),
 ('light','TEXT_TERTIARY on CARD_BACKGROUND (counts, empty states)', C['TEXT_TERTIARY'], C['CARD_BACKGROUND'], 't'),
 ('light','TEXT_TERTIARY on BACKGROUND (peer count "(1P)")', C['TEXT_TERTIARY'], C['BACKGROUND'], 't'),
 ('light','Button label: TEXT_PRIMARY (override) on PRIMARY fill', C['TEXT_PRIMARY'], C['PRIMARY'], 't'),
 ('light','Button label hovered: TEXT_PRIMARY on PRIMARY_HOVER', C['TEXT_PRIMARY'], C['PRIMARY_HOVER'], 't'),
 ('light','Save File: WHITE on PRIMARY (topic_tree.rs:345)', C['WHITE'], C['PRIMARY'], 't'),
 ('light','Pause label: TEXT_SECONDARY on PRIMARY', C['TEXT_SECONDARY'], C['PRIMARY'], 't'),
 ('light','Resume label: WARNING on PRIMARY', C['WARNING'], C['PRIMARY'], 't'),
 ('light','Status: SUCCESS on BACKGROUND ("Connected", memory <70%)', C['SUCCESS'], C['BACKGROUND'], 't'),
 ('light','Status: WARNING on BACKGROUND (connecting, memory 70-90%, drops)', C['WARNING'], C['BACKGROUND'], 't'),
 ('light','Status: ERROR on BACKGROUND (disconnected, memory >90%)', C['ERROR'], C['BACKGROUND'], 't'),
 ('light','Worker Unresponsive: ERROR a=178 (pulse trough) on BACKGROUND', PULSE_L, C['BACKGROUND'], 't'),
 ('light','Banner/inline: SUCCESS on CARD_BACKGROUND', C['SUCCESS'], C['CARD_BACKGROUND'], 't'),
 ('light','Banner/inline: WARNING on CARD_BACKGROUND', C['WARNING'], C['CARD_BACKGROUND'], 't'),
 ('light','Inline: ERROR on CARD_BACKGROUND ("Not connected")', C['ERROR'], C['CARD_BACKGROUND'], 't'),
 ('light','Badge SUB: WHITE on PRIMARY', C['WHITE'], C['PRIMARY'], 't'),
 ('light','Badge PUT: WHITE on SUCCESS', C['WHITE'], C['SUCCESS'], 't'),
 ('light','Badge GET: WHITE on WARNING (variant never constructed)', C['WHITE'], C['WARNING'], 't'),
 ('light','Badge REPLY: WHITE on ERROR', C['WHITE'], C['ERROR'], 't'),
 ('light','Code/payload: TEXT_PRIMARY on code_bg gray(240)', C['TEXT_PRIMARY'], C['CODE_L'], 't'),
 ('light','Text edit: TEXT_PRIMARY on SURFACE', C['TEXT_PRIMARY'], C['SURFACE'], 't'),
 ('light','Selected row text: TEXT_PRIMARY on composited selection', C['TEXT_PRIMARY'], SEL_L, 't'),
 ('light','Selected row fill vs CARD_BACKGROUND (non-text, 1.4.11)', SEL_L, C['CARD_BACKGROUND'], 'ui'),
 ('light','Progress fill vs track SURFACE (non-text, 1.4.11)', PROG_L, C['SURFACE'], 'ui'),
 ('light','TextEdit focus ring: selection.stroke WHITE vs CARD_BACKGROUND (non-text)', C['WHITE'], C['CARD_BACKGROUND'], 'ui'),
 ('light','TextEdit focus ring: selection.stroke WHITE vs field SURFACE (non-text)', C['WHITE'], C['SURFACE'], 'ui'),
 ('light','Button focus (widgets.active): bg_stroke PRIMARY vs CARD_BACKGROUND (non-text)', C['PRIMARY'], C['CARD_BACKGROUND'], 'ui'),
 ('light','Button focus (widgets.active): face PRIMARY_HOVER vs unfocused PRIMARY (non-text)', C['PRIMARY_HOVER'], C['PRIMARY'], 'ui'),
 ('light','Leader line a=64 vs CARD_BACKGROUND (non-text, decorative)', LEAD_L[0], C['CARD_BACKGROUND'], 'ui'),
 ('dark','Title / body text: DARK_TEXT_PRIMARY on DARK_BACKGROUND', C['DARK_TEXT_PRIMARY'], C['DARK_BACKGROUND'], 't'),
 ('dark','Body text: DARK_TEXT_PRIMARY on DARK_CARD_BACKGROUND', C['DARK_TEXT_PRIMARY'], C['DARK_CARD_BACKGROUND'], 't'),
 ('dark','DARK_TEXT_SECONDARY on DARK_CARD_BACKGROUND', C['DARK_TEXT_SECONDARY'], C['DARK_CARD_BACKGROUND'], 't'),
 ('dark','DARK_TEXT_TERTIARY on DARK_CARD_BACKGROUND', C['DARK_TEXT_TERTIARY'], C['DARK_CARD_BACKGROUND'], 't'),
 ('dark','DARK_TEXT_TERTIARY on DARK_BACKGROUND ("(1P)")', C['DARK_TEXT_TERTIARY'], C['DARK_BACKGROUND'], 't'),
 ('dark','Button label: DARK_TEXT_PRIMARY on DARK_PRIMARY', C['DARK_TEXT_PRIMARY'], C['DARK_PRIMARY'], 't'),
 ('dark','Button label hovered: DARK_TEXT_PRIMARY on DARK_PRIMARY_HOVER', C['DARK_TEXT_PRIMARY'], C['DARK_PRIMARY_HOVER'], 't'),
 ('dark','Save File: WHITE on DARK_PRIMARY', C['WHITE'], C['DARK_PRIMARY'], 't'),
 ('dark','Pause label: DARK_TEXT_SECONDARY on DARK_PRIMARY', C['DARK_TEXT_SECONDARY'], C['DARK_PRIMARY'], 't'),
 ('dark','Resume label: WARNING on DARK_PRIMARY', C['WARNING'], C['DARK_PRIMARY'], 't'),
 ('dark','Status: SUCCESS (light const) on DARK_BACKGROUND', C['SUCCESS'], C['DARK_BACKGROUND'], 't'),
 ('dark','Status: WARNING (light const) on DARK_BACKGROUND', C['WARNING'], C['DARK_BACKGROUND'], 't'),
 ('dark','Status: ERROR (light const) on DARK_BACKGROUND', C['ERROR'], C['DARK_BACKGROUND'], 't'),
 ('dark','Worker Unresponsive: ERROR a=178 (pulse trough) on DARK_BACKGROUND', PULSE_D, C['DARK_BACKGROUND'], 't'),
 ('dark','Banner success: DARK_SUCCESS on DARK_CARD_BACKGROUND', C['DARK_SUCCESS'], C['DARK_CARD_BACKGROUND'], 't'),
 ('dark','Banner/inline: WARNING (light const) on DARK_CARD_BACKGROUND', C['WARNING'], C['DARK_CARD_BACKGROUND'], 't'),
 ('dark','Inline: ERROR (light const) on DARK_CARD_BACKGROUND', C['ERROR'], C['DARK_CARD_BACKGROUND'], 't'),
 ('dark','Inline: SUCCESS (light const) on DARK_CARD_BACKGROUND (chunk labels)', C['SUCCESS'], C['DARK_CARD_BACKGROUND'], 't'),
 ('dark','Badge SUB: WHITE on PRIMARY (not theme-aware)', C['WHITE'], C['PRIMARY'], 't'),
 ('dark','Badge PUT: WHITE on SUCCESS', C['WHITE'], C['SUCCESS'], 't'),
 ('dark','Badge REPLY: WHITE on ERROR', C['WHITE'], C['ERROR'], 't'),
 ('dark','Code/payload: DARK_TEXT_PRIMARY on code_bg gray(30)', C['DARK_TEXT_PRIMARY'], C['CODE_D'], 't'),
 ('dark','Text edit: DARK_TEXT_PRIMARY on DARK_SURFACE', C['DARK_TEXT_PRIMARY'], C['DARK_SURFACE'], 't'),
 ('dark','Selected row text: DARK_TEXT_PRIMARY on composited selection', C['DARK_TEXT_PRIMARY'], SEL_D, 't'),
 ('dark','Selected row fill vs DARK_CARD_BACKGROUND (non-text)', SEL_D, C['DARK_CARD_BACKGROUND'], 'ui'),
 ('dark','Progress fill vs track DARK_SURFACE (non-text)', PROG_D, C['DARK_SURFACE'], 'ui'),
 ('dark','TextEdit focus ring: selection.stroke DARK_TEXT_PRIMARY vs DARK_CARD_BACKGROUND (non-text)', C['DARK_TEXT_PRIMARY'], C['DARK_CARD_BACKGROUND'], 'ui'),
 ('dark','TextEdit focus ring: selection.stroke DARK_TEXT_PRIMARY vs field DARK_SURFACE (non-text)', C['DARK_TEXT_PRIMARY'], C['DARK_SURFACE'], 'ui'),
 ('dark','Button focus (widgets.active): bg_stroke DARK_PRIMARY vs DARK_CARD_BACKGROUND (non-text)', C['DARK_PRIMARY'], C['DARK_CARD_BACKGROUND'], 'ui'),
 ('dark','Button focus (widgets.active): face DARK_PRIMARY_HOVER vs unfocused DARK_PRIMARY (non-text)', C['DARK_PRIMARY_HOVER'], C['DARK_PRIMARY'], 'ui'),
 ('dark','Leader line a=64 vs DARK_CARD_BACKGROUND (non-text, decorative)', LEAD_D[0], C['DARK_CARD_BACKGROUND'], 'ui'),
]

S = {k: hx(v) for k, v in dict(
 chassis='#e9e6dc', panel='#f3f0e7', textPrimary='#333a35', textSecondary='#626b5a', seam='#bbbdb1',
 olive='#495e4e', oliveKeyLight='#64735b', oliveKeyDark='#4e5c46', selectedKeyLight='#ba7754',
 selectedKeyDark='#a65e41', playKeyLight='#c18757', playKeyDark='#af6f43', keyTextLight='#fff4df',
 focus='#ae5339', statusGlass='#172b24', statusText='#cee2b4', contentGlass='#090f38',
 contentText='#fff1da', contentSecondary='#bfc2e9',
 # colors.vgaAccents[0..4] (tokens.json gives no names; indices used below)
 vga0='#445dcc', vga1='#7b53ad', vga2='#ee9b99', vga3='#92d48d', vga4='#ffd07f',
 # ---- derived, NOT in tokens.json (marked in the section) ----
 field='#fbf9f3',            # light text-edit recess: panel lifted toward white
 okInk='#3f6a3c', warnInk='#855412', errInk='#9e3b2b',   # light semantic ink on panel
 gChassis='#262b28', gPanel='#333a35', gField='#1f2421',  # dark (graphite) enclosure
 actionRust='#94532f',       # playKeyDark darkened until keyTextLight reaches AA
 selectedInk='#9c5539',      # selectedKeyDark darkened until keyTextLight reaches AA
 textSecondaryAA='#5a6353',  # textSecondary darkened so it reaches AA on chassis too
 edgeBase='#d6d1c2',
 gSeam='#4b534d',            # dark decorative separator         # = responseEdgeBase token, reused as dark-theme selection rim
).items()}

prop = [
 ('light','Title/body: textPrimary on chassis (header row)', S['textPrimary'], S['chassis'], 't'),
 ('light','Body: textPrimary on panel', S['textPrimary'], S['panel'], 't'),
 ('light','Secondary: textSecondary on panel', S['textSecondary'], S['panel'], 't'),
 ('light','  (rejected) textSecondary token on chassis', S['textSecondary'], S['chassis'], 't'),
 ('light','Secondary/tertiary: textSecondaryAA on chassis [derived]', S['textSecondaryAA'], S['chassis'], 't'),
 ('light','Secondary/tertiary: textSecondaryAA on panel [derived]', S['textSecondaryAA'], S['panel'], 't'),
 ('light','Key label: keyTextLight on olive', S['keyTextLight'], S['olive'], 't'),
 ('light','Key label hovered: keyTextLight on oliveKeyLight', S['keyTextLight'], S['oliveKeyLight'], 't'),
 ('light','Key label pressed: keyTextLight on oliveKeyDark', S['keyTextLight'], S['oliveKeyDark'], 't'),
 ('light','Primary action (Save File): keyTextLight on actionRust [derived]', S['keyTextLight'], S['actionRust'], 't'),
 ('light','  (rejected) keyTextLight on playKeyDark', S['keyTextLight'], S['playKeyDark'], 't'),
 ('light','  (rejected) keyTextLight on playKeyLight', S['keyTextLight'], S['playKeyLight'], 't'),
 ('light','Selected row: keyTextLight on selectedInk [derived]', S['keyTextLight'], S['selectedInk'], 't'),
 ('light','  (rejected) keyTextLight on selectedKeyDark', S['keyTextLight'], S['selectedKeyDark'], 't'),
 ('light','  (rejected) keyTextLight on selectedKeyLight', S['keyTextLight'], S['selectedKeyLight'], 't'),
 ('light','  (rejected) textPrimary on selectedKeyLight', S['textPrimary'], S['selectedKeyLight'], 't'),
 ('light','Selected fill selectedInk vs panel (non-text)', S['selectedInk'], S['panel'], 'ui'),
 ('light','Focus ring (selection.stroke + widgets.active.bg_stroke): focus vs panel (non-text)', S['focus'], S['panel'], 'ui'),
 ('light','Focus ring: focus vs field [derived] (non-text)', S['focus'], S['field'], 'ui'),
 ('light','Focus ring: focus vs chassis (non-text; header-row controls)', S['focus'], S['chassis'], 'ui'),
 ('light','  (rejected) selection.stroke = keyTextLight vs panel (non-text)', S['keyTextLight'], S['panel'], 'ui'),
 ('light','Status readout: statusText on statusGlass (Connected / OK)', S['statusText'], S['statusGlass'], 't'),
 ('light','Status readout: vgaAccents[4] on statusGlass (warning)', S['vga4'], S['statusGlass'], 't'),
 ('light','Status readout: vgaAccents[2] on statusGlass (error)', S['vga2'], S['statusGlass'], 't'),
 ('light','Inline success: okInk on panel [derived]', S['okInk'], S['panel'], 't'),
 ('light','Inline warning: warnInk on panel [derived]', S['warnInk'], S['panel'], 't'),
 ('light','Inline error: errInk on panel [derived]', S['errInk'], S['panel'], 't'),
 ('light','Badge SUB: keyTextLight on olive', S['keyTextLight'], S['olive'], 't'),
 ('light','Badge PUT: keyTextLight on actionRust [derived]', S['keyTextLight'], S['actionRust'], 't'),
 ('light','Badge GET: keyTextLight on vgaAccents[0]', S['keyTextLight'], S['vga0'], 't'),
 ('light','Badge REPLY: keyTextLight on vgaAccents[1]', S['keyTextLight'], S['vga1'], 't'),
 ('light','Payload display: contentText on contentGlass', S['contentText'], S['contentGlass'], 't'),
 ('light','Payload secondary: contentSecondary on contentGlass', S['contentSecondary'], S['contentGlass'], 't'),
 ('light','Text edit: textPrimary on field [derived]', S['textPrimary'], S['field'], 't'),
 ('light','Seam vs panel (separator, non-text; decorative)', S['seam'], S['panel'], 'ui'),
 ('dark','Title/body: panel-ivory on gChassis [derived]', S['panel'], S['gChassis'], 't'),
 ('dark','Body: panel-ivory on gPanel (= textPrimary graphite)', S['panel'], S['gPanel'], 't'),
 ('dark','Secondary: seam on gPanel', S['seam'], S['gPanel'], 't'),
 ('dark','Secondary: seam on gChassis', S['seam'], S['gChassis'], 't'),
 ('dark','Key label: keyTextLight on olive', S['keyTextLight'], S['olive'], 't'),
 ('dark','Key label hovered: keyTextLight on oliveKeyLight', S['keyTextLight'], S['oliveKeyLight'], 't'),
 ('dark','  (rejected alone) olive key face vs gPanel (non-text)', S['olive'], S['gPanel'], 'ui'),
 ('dark','Key rim: seam stroke vs gPanel (non-text)', S['seam'], S['gPanel'], 'ui'),
 ('dark','Primary action: keyTextLight on actionRust [derived]', S['keyTextLight'], S['actionRust'], 't'),
 ('dark','Selected row: keyTextLight on selectedInk [derived]', S['keyTextLight'], S['selectedInk'], 't'),
 ('dark','  (rejected alone) selectedInk fill vs gPanel (non-text)', S['selectedInk'], S['gPanel'], 'ui'),
 ('dark','Selected rim edgeBase vs gPanel (non-text)', S['edgeBase'], S['gPanel'], 'ui'),
 ('dark','  (rejected) focus token vs gPanel (non-text)', S['focus'], S['gPanel'], 'ui'),
 ('dark','Focus ring: playKeyLight vs gPanel (non-text)', S['playKeyLight'], S['gPanel'], 'ui'),
 ('dark','Focus ring: playKeyLight vs gField [derived] (non-text)', S['playKeyLight'], S['gField'], 'ui'),
 ('dark','Focus ring: playKeyLight vs gChassis [derived] (non-text)', S['playKeyLight'], S['gChassis'], 'ui'),
 ('dark','Separator: gSeam #4b534d vs gPanel [derived] (non-text, decorative)', S['gSeam'], S['gPanel'], 'ui'),
 ('dark','Status readout: statusText on statusGlass', S['statusText'], S['statusGlass'], 't'),
 ('dark','Status readout: vgaAccents[4] on statusGlass', S['vga4'], S['statusGlass'], 't'),
 ('dark','Status readout: vgaAccents[2] on statusGlass', S['vga2'], S['statusGlass'], 't'),
 ('dark','Inline success: vgaAccents[3] on gPanel', S['vga3'], S['gPanel'], 't'),
 ('dark','Inline warning: vgaAccents[4] on gPanel', S['vga4'], S['gPanel'], 't'),
 ('dark','Inline error: vgaAccents[2] on gPanel', S['vga2'], S['gPanel'], 't'),
 ('dark','Badge SUB: keyTextLight on olive', S['keyTextLight'], S['olive'], 't'),
 ('dark','Badge PUT: keyTextLight on actionRust [derived]', S['keyTextLight'], S['actionRust'], 't'),
 ('dark','Badge GET: keyTextLight on vgaAccents[0]', S['keyTextLight'], S['vga0'], 't'),
 ('dark','Badge REPLY: keyTextLight on vgaAccents[1]', S['keyTextLight'], S['vga1'], 't'),
 ('dark','Payload display: contentText on contentGlass', S['contentText'], S['contentGlass'], 't'),
 ('dark','Text edit: panel-ivory on gField [derived]', S['panel'], S['gField'], 't'),
]

def table(title, rows):
    print(f'\n## {title}\n\n| Theme | Pair | fg | bg | Ratio | WCAG 2.x |\n|---|---|---|---|---|---|')
    for th, n, fg, bg, k in rows:
        r = ratio(fg, bg)
        print(f'| {th} | {n} | `{fmt(fg)}` | `{fmt(bg)}` | {r:.2f}:1 | {verdict(r, ui=(k=="ui"))} |')
table('Current (HEAD)', cur)
table('Proposed (Snow White tokens.json; [derived] = not in tokens)', prop)
