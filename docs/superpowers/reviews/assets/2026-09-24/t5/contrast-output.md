## Composited premultiplied / alpha colours
- SELECTED_BACKGROUND over CARD_BACKGROUND: (230, 255, 255) #e6ffff
- DARK_SELECTED_BACKGROUND over DARK_CARD_BACKGROUND: (73, 195, 255) #49c3ff
- progress fill (SELECTED_BACKGROUND over SURFACE): (225, 255, 255) #e1ffff
- progress fill dark (over DARK_SURFACE): (61, 183, 255) #3db7ff
- SEPARATOR over CARD_BACKGROUND (unused): (229, 229, 229) #e5e5e5
- DARK_SEPARATOR over DARK_CARD_BACKGROUND (unused): (255, 255, 255) #ffffff
- ERROR a=178 over BACKGROUND (pulse trough): (255, 124, 114) #ff7c72
- ERROR a=178 over DARK_BACKGROUND: (232, 63, 53) #e83f35
- leader line TEXT_TERTIARY a=64 / a=100 over CARD_BACKGROUND: (241, 241, 242) #f1f1f2 / (217, 217, 220) #d9d9dc
- leader line DARK_TEXT_TERTIARY a=64 / a=100 over DARK_CARD_BACKGROUND: (151, 151, 151) #979797 / (163, 163, 163) #a3a3a3

## Current (HEAD)

| Theme | Pair | fg | bg | Ratio | WCAG 2.x |
|---|---|---|---|---|---|
| light | Title / body text: TEXT_PRIMARY on BACKGROUND (header row) | `#1c1c1e` | `#f8f8f8` | 16.02:1 | AAA |
| light | Body text: TEXT_PRIMARY on CARD_BACKGROUND (tree, detail, toolbar) | `#1c1c1e` | `#ffffff` | 17.01:1 | AAA |
| light | TEXT_SECONDARY on CARD_BACKGROUND | `#3c3c43` | `#ffffff` | 10.94:1 | AAA |
| light | TEXT_TERTIARY on CARD_BACKGROUND (counts, empty states) | `#636366` | `#ffffff` | 5.99:1 | AA |
| light | TEXT_TERTIARY on BACKGROUND (peer count "(1P)") | `#636366` | `#f8f8f8` | 5.64:1 | AA |
| light | Button label: TEXT_PRIMARY (override) on PRIMARY fill | `#1c1c1e` | `#007aff` | 4.24:1 | AA-large only |
| light | Button label hovered: TEXT_PRIMARY on PRIMARY_HOVER | `#1c1c1e` | `#0066d9` | 3.16:1 | AA-large only |
| light | Save File: WHITE on PRIMARY (topic_tree.rs:345) | `#ffffff` | `#007aff` | 4.02:1 | AA-large only |
| light | Pause label: TEXT_SECONDARY on PRIMARY | `#3c3c43` | `#007aff` | 2.72:1 | FAIL |
| light | Resume label: WARNING on PRIMARY | `#ff9500` | `#007aff` | 1.83:1 | FAIL |
| light | Status: SUCCESS on BACKGROUND ("Connected", memory <70%) | `#34c759` | `#f8f8f8` | 2.09:1 | FAIL |
| light | Status: WARNING on BACKGROUND (connecting, memory 70-90%, drops) | `#ff9500` | `#f8f8f8` | 2.07:1 | FAIL |
| light | Status: ERROR on BACKGROUND (disconnected, memory >90%) | `#ff3b30` | `#f8f8f8` | 3.34:1 | AA-large only |
| light | Worker Unresponsive: ERROR a=178 (pulse trough) on BACKGROUND | `#ff7c72` | `#f8f8f8` | 2.36:1 | FAIL |
| light | Banner/inline: SUCCESS on CARD_BACKGROUND | `#34c759` | `#ffffff` | 2.22:1 | FAIL |
| light | Banner/inline: WARNING on CARD_BACKGROUND | `#ff9500` | `#ffffff` | 2.20:1 | FAIL |
| light | Inline: ERROR on CARD_BACKGROUND ("Not connected") | `#ff3b30` | `#ffffff` | 3.55:1 | AA-large only |
| light | Badge SUB: WHITE on PRIMARY | `#ffffff` | `#007aff` | 4.02:1 | AA-large only |
| light | Badge PUT: WHITE on SUCCESS | `#ffffff` | `#34c759` | 2.22:1 | FAIL |
| light | Badge GET: WHITE on WARNING (variant never constructed) | `#ffffff` | `#ff9500` | 2.20:1 | FAIL |
| light | Badge REPLY: WHITE on ERROR | `#ffffff` | `#ff3b30` | 3.55:1 | AA-large only |
| light | Code/payload: TEXT_PRIMARY on code_bg gray(240) | `#1c1c1e` | `#f0f0f0` | 14.93:1 | AAA |
| light | Text edit: TEXT_PRIMARY on SURFACE | `#1c1c1e` | `#fafafa` | 16.30:1 | AAA |
| light | Selected row text: TEXT_PRIMARY on composited selection | `#1c1c1e` | `#e6ffff` | 16.30:1 | AAA |
| light | Selected row fill vs CARD_BACKGROUND (non-text, 1.4.11) | `#e6ffff` | `#ffffff` | 1.04:1 | FAIL 3:1 |
| light | Progress fill vs track SURFACE (non-text, 1.4.11) | `#e1ffff` | `#fafafa` | 1.01:1 | FAIL 3:1 |
| light | TextEdit focus ring: selection.stroke WHITE vs CARD_BACKGROUND (non-text) | `#ffffff` | `#ffffff` | 1.00:1 | FAIL 3:1 |
| light | TextEdit focus ring: selection.stroke WHITE vs field SURFACE (non-text) | `#ffffff` | `#fafafa` | 1.04:1 | FAIL 3:1 |
| light | Button focus (widgets.active): bg_stroke PRIMARY vs CARD_BACKGROUND (non-text) | `#007aff` | `#ffffff` | 4.02:1 | pass 3:1 |
| light | Button focus (widgets.active): face PRIMARY_HOVER vs unfocused PRIMARY (non-text) | `#0066d9` | `#007aff` | 1.34:1 | FAIL 3:1 |
| light | Leader line a=64 vs CARD_BACKGROUND (non-text, decorative) | `#f1f1f2` | `#ffffff` | 1.13:1 | FAIL 3:1 |
| dark | Title / body text: DARK_TEXT_PRIMARY on DARK_BACKGROUND | `#ffffff` | `#2d2d2d` | 13.77:1 | AAA |
| dark | Body text: DARK_TEXT_PRIMARY on DARK_CARD_BACKGROUND | `#ffffff` | `#4b4b4b` | 8.72:1 | AAA |
| dark | DARK_TEXT_SECONDARY on DARK_CARD_BACKGROUND | `#c8c8c8` | `#4b4b4b` | 5.21:1 | AA |
| dark | DARK_TEXT_TERTIARY on DARK_CARD_BACKGROUND | `#b4b4b4` | `#4b4b4b` | 4.21:1 | AA-large only |
| dark | DARK_TEXT_TERTIARY on DARK_BACKGROUND ("(1P)") | `#b4b4b4` | `#2d2d2d` | 6.64:1 | AA |
| dark | Button label: DARK_TEXT_PRIMARY on DARK_PRIMARY | `#ffffff` | `#0a84ff` | 3.65:1 | AA-large only |
| dark | Button label hovered: DARK_TEXT_PRIMARY on DARK_PRIMARY_HOVER | `#ffffff` | `#409cff` | 2.83:1 | FAIL |
| dark | Save File: WHITE on DARK_PRIMARY | `#ffffff` | `#0a84ff` | 3.65:1 | AA-large only |
| dark | Pause label: DARK_TEXT_SECONDARY on DARK_PRIMARY | `#c8c8c8` | `#0a84ff` | 2.18:1 | FAIL |
| dark | Resume label: WARNING on DARK_PRIMARY | `#ff9500` | `#0a84ff` | 1.66:1 | FAIL |
| dark | Status: SUCCESS (light const) on DARK_BACKGROUND | `#34c759` | `#2d2d2d` | 6.20:1 | AA |
| dark | Status: WARNING (light const) on DARK_BACKGROUND | `#ff9500` | `#2d2d2d` | 6.26:1 | AA |
| dark | Status: ERROR (light const) on DARK_BACKGROUND | `#ff3b30` | `#2d2d2d` | 3.88:1 | AA-large only |
| dark | Worker Unresponsive: ERROR a=178 (pulse trough) on DARK_BACKGROUND | `#e83f35` | `#2d2d2d` | 3.41:1 | AA-large only |
| dark | Banner success: DARK_SUCCESS on DARK_CARD_BACKGROUND | `#30d158` | `#4b4b4b` | 4.31:1 | AA-large only |
| dark | Banner/inline: WARNING (light const) on DARK_CARD_BACKGROUND | `#ff9500` | `#4b4b4b` | 3.97:1 | AA-large only |
| dark | Inline: ERROR (light const) on DARK_CARD_BACKGROUND | `#ff3b30` | `#4b4b4b` | 2.46:1 | FAIL |
| dark | Inline: SUCCESS (light const) on DARK_CARD_BACKGROUND (chunk labels) | `#34c759` | `#4b4b4b` | 3.93:1 | AA-large only |
| dark | Badge SUB: WHITE on PRIMARY (not theme-aware) | `#ffffff` | `#007aff` | 4.02:1 | AA-large only |
| dark | Badge PUT: WHITE on SUCCESS | `#ffffff` | `#34c759` | 2.22:1 | FAIL |
| dark | Badge REPLY: WHITE on ERROR | `#ffffff` | `#ff3b30` | 3.55:1 | AA-large only |
| dark | Code/payload: DARK_TEXT_PRIMARY on code_bg gray(30) | `#ffffff` | `#1e1e1e` | 16.67:1 | AAA |
| dark | Text edit: DARK_TEXT_PRIMARY on DARK_SURFACE | `#ffffff` | `#3c3c3c` | 11.03:1 | AAA |
| dark | Selected row text: DARK_TEXT_PRIMARY on composited selection | `#ffffff` | `#49c3ff` | 1.99:1 | FAIL |
| dark | Selected row fill vs DARK_CARD_BACKGROUND (non-text) | `#49c3ff` | `#4b4b4b` | 4.38:1 | pass 3:1 |
| dark | Progress fill vs track DARK_SURFACE (non-text) | `#3db7ff` | `#3c3c3c` | 4.95:1 | pass 3:1 |
| dark | TextEdit focus ring: selection.stroke DARK_TEXT_PRIMARY vs DARK_CARD_BACKGROUND (non-text) | `#ffffff` | `#4b4b4b` | 8.72:1 | pass 3:1 |
| dark | TextEdit focus ring: selection.stroke DARK_TEXT_PRIMARY vs field DARK_SURFACE (non-text) | `#ffffff` | `#3c3c3c` | 11.03:1 | pass 3:1 |
| dark | Button focus (widgets.active): bg_stroke DARK_PRIMARY vs DARK_CARD_BACKGROUND (non-text) | `#0a84ff` | `#4b4b4b` | 2.39:1 | FAIL 3:1 |
| dark | Button focus (widgets.active): face DARK_PRIMARY_HOVER vs unfocused DARK_PRIMARY (non-text) | `#409cff` | `#0a84ff` | 1.29:1 | FAIL 3:1 |
| dark | Leader line a=64 vs DARK_CARD_BACKGROUND (non-text, decorative) | `#979797` | `#4b4b4b` | 2.99:1 | FAIL 3:1 |

## Proposed (Snow White tokens.json; [derived] = not in tokens)

| Theme | Pair | fg | bg | Ratio | WCAG 2.x |
|---|---|---|---|---|---|
| light | Title/body: textPrimary on chassis (header row) | `#333a35` | `#e9e6dc` | 9.36:1 | AAA |
| light | Body: textPrimary on panel | `#333a35` | `#f3f0e7` | 10.25:1 | AAA |
| light | Secondary: textSecondary on panel | `#626b5a` | `#f3f0e7` | 4.89:1 | AA |
| light |   (rejected) textSecondary token on chassis | `#626b5a` | `#e9e6dc` | 4.46:1 | AA-large only |
| light | Secondary/tertiary: textSecondaryAA on chassis [derived] | `#5a6353` | `#e9e6dc` | 5.03:1 | AA |
| light | Secondary/tertiary: textSecondaryAA on panel [derived] | `#5a6353` | `#f3f0e7` | 5.51:1 | AA |
| light | Key label: keyTextLight on olive | `#fff4df` | `#495e4e` | 6.43:1 | AA |
| light | Key label hovered: keyTextLight on oliveKeyLight | `#fff4df` | `#64735b` | 4.65:1 | AA |
| light | Key label pressed: keyTextLight on oliveKeyDark | `#fff4df` | `#4e5c46` | 6.54:1 | AA |
| light | Primary action (Save File): keyTextLight on actionRust [derived] | `#fff4df` | `#94532f` | 5.44:1 | AA |
| light |   (rejected) keyTextLight on playKeyDark | `#fff4df` | `#af6f43` | 3.72:1 | AA-large only |
| light |   (rejected) keyTextLight on playKeyLight | `#fff4df` | `#c18757` | 2.80:1 | FAIL |
| light | Selected row: keyTextLight on selectedInk [derived] | `#fff4df` | `#9c5539` | 5.11:1 | AA |
| light |   (rejected) keyTextLight on selectedKeyDark | `#fff4df` | `#a65e41` | 4.48:1 | AA-large only |
| light |   (rejected) keyTextLight on selectedKeyLight | `#fff4df` | `#ba7754` | 3.29:1 | AA-large only |
| light |   (rejected) textPrimary on selectedKeyLight | `#333a35` | `#ba7754` | 3.26:1 | AA-large only |
| light | Selected fill selectedInk vs panel (non-text) | `#9c5539` | `#f3f0e7` | 4.89:1 | pass 3:1 |
| light | Focus ring (selection.stroke + widgets.active.bg_stroke): focus vs panel (non-text) | `#ae5339` | `#f3f0e7` | 4.50:1 | pass 3:1 |
| light | Focus ring: focus vs field [derived] (non-text) | `#ae5339` | `#fbf9f3` | 4.87:1 | pass 3:1 |
| light | Focus ring: focus vs chassis (non-text; header-row controls) | `#ae5339` | `#e9e6dc` | 4.11:1 | pass 3:1 |
| light |   (rejected) selection.stroke = keyTextLight vs panel (non-text) | `#fff4df` | `#f3f0e7` | 1.04:1 | FAIL 3:1 |
| light | Status readout: statusText on statusGlass (Connected / OK) | `#cee2b4` | `#172b24` | 10.77:1 | AAA |
| light | Status readout: vgaAccents[4] on statusGlass (warning) | `#ffd07f` | `#172b24` | 10.36:1 | AAA |
| light | Status readout: vgaAccents[2] on statusGlass (error) | `#ee9b99` | `#172b24` | 6.95:1 | AA |
| light | Inline success: okInk on panel [derived] | `#3f6a3c` | `#f3f0e7` | 5.52:1 | AA |
| light | Inline warning: warnInk on panel [derived] | `#855412` | `#f3f0e7` | 5.63:1 | AA |
| light | Inline error: errInk on panel [derived] | `#9e3b2b` | `#f3f0e7` | 5.92:1 | AA |
| light | Badge SUB: keyTextLight on olive | `#fff4df` | `#495e4e` | 6.43:1 | AA |
| light | Badge PUT: keyTextLight on actionRust [derived] | `#fff4df` | `#94532f` | 5.44:1 | AA |
| light | Badge GET: keyTextLight on vgaAccents[0] | `#fff4df` | `#445dcc` | 5.23:1 | AA |
| light | Badge REPLY: keyTextLight on vgaAccents[1] | `#fff4df` | `#7b53ad` | 5.23:1 | AA |
| light | Payload display: contentText on contentGlass | `#fff1da` | `#090f38` | 16.57:1 | AAA |
| light | Payload secondary: contentSecondary on contentGlass | `#bfc2e9` | `#090f38` | 10.65:1 | AAA |
| light | Text edit: textPrimary on field [derived] | `#333a35` | `#fbf9f3` | 11.10:1 | AAA |
| light | Seam vs panel (separator, non-text; decorative) | `#bbbdb1` | `#f3f0e7` | 1.67:1 | FAIL 3:1 |
| dark | Title/body: panel-ivory on gChassis [derived] | `#f3f0e7` | `#262b28` | 12.63:1 | AAA |
| dark | Body: panel-ivory on gPanel (= textPrimary graphite) | `#f3f0e7` | `#333a35` | 10.25:1 | AAA |
| dark | Secondary: seam on gPanel | `#bbbdb1` | `#333a35` | 6.13:1 | AA |
| dark | Secondary: seam on gChassis | `#bbbdb1` | `#262b28` | 7.56:1 | AAA |
| dark | Key label: keyTextLight on olive | `#fff4df` | `#495e4e` | 6.43:1 | AA |
| dark | Key label hovered: keyTextLight on oliveKeyLight | `#fff4df` | `#64735b` | 4.65:1 | AA |
| dark |   (rejected alone) olive key face vs gPanel (non-text) | `#495e4e` | `#333a35` | 1.67:1 | FAIL 3:1 |
| dark | Key rim: seam stroke vs gPanel (non-text) | `#bbbdb1` | `#333a35` | 6.13:1 | pass 3:1 |
| dark | Primary action: keyTextLight on actionRust [derived] | `#fff4df` | `#94532f` | 5.44:1 | AA |
| dark | Selected row: keyTextLight on selectedInk [derived] | `#fff4df` | `#9c5539` | 5.11:1 | AA |
| dark |   (rejected alone) selectedInk fill vs gPanel (non-text) | `#9c5539` | `#333a35` | 2.10:1 | FAIL 3:1 |
| dark | Selected rim edgeBase vs gPanel (non-text) | `#d6d1c2` | `#333a35` | 7.65:1 | pass 3:1 |
| dark |   (rejected) focus token vs gPanel (non-text) | `#ae5339` | `#333a35` | 2.28:1 | FAIL 3:1 |
| dark | Focus ring: playKeyLight vs gPanel (non-text) | `#c18757` | `#333a35` | 3.82:1 | pass 3:1 |
| dark | Focus ring: playKeyLight vs gField [derived] (non-text) | `#c18757` | `#1f2421` | 5.16:1 | pass 3:1 |
| dark | Focus ring: playKeyLight vs gChassis [derived] (non-text) | `#c18757` | `#262b28` | 4.71:1 | pass 3:1 |
| dark | Separator: gSeam #4b534d vs gPanel [derived] (non-text, decorative) | `#4b534d` | `#333a35` | 1.47:1 | FAIL 3:1 |
| dark | Status readout: statusText on statusGlass | `#cee2b4` | `#172b24` | 10.77:1 | AAA |
| dark | Status readout: vgaAccents[4] on statusGlass | `#ffd07f` | `#172b24` | 10.36:1 | AAA |
| dark | Status readout: vgaAccents[2] on statusGlass | `#ee9b99` | `#172b24` | 6.95:1 | AA |
| dark | Inline success: vgaAccents[3] on gPanel | `#92d48d` | `#333a35` | 6.69:1 | AA |
| dark | Inline warning: vgaAccents[4] on gPanel | `#ffd07f` | `#333a35` | 8.11:1 | AAA |
| dark | Inline error: vgaAccents[2] on gPanel | `#ee9b99` | `#333a35` | 5.44:1 | AA |
| dark | Badge SUB: keyTextLight on olive | `#fff4df` | `#495e4e` | 6.43:1 | AA |
| dark | Badge PUT: keyTextLight on actionRust [derived] | `#fff4df` | `#94532f` | 5.44:1 | AA |
| dark | Badge GET: keyTextLight on vgaAccents[0] | `#fff4df` | `#445dcc` | 5.23:1 | AA |
| dark | Badge REPLY: keyTextLight on vgaAccents[1] | `#fff4df` | `#7b53ad` | 5.23:1 | AA |
| dark | Payload display: contentText on contentGlass | `#fff1da` | `#090f38` | 16.57:1 | AAA |
| dark | Text edit: panel-ivory on gField [derived] | `#f3f0e7` | `#1f2421` | 13.83:1 | AAA |
