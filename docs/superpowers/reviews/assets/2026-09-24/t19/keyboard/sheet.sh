#!/bin/bash
# sheet.sh <dir> : render crop*.png with labels into <dir>/sheet.png
O=$1; S=$(dirname "$0"); cd $O
python3 - "$O" <<'PY'
import glob,sys,os
O=sys.argv[1]; fs=sorted(glob.glob(O+'/crop*.png'))
cells=''.join(f'<figure style="margin:6px;display:inline-block;vertical-align:top;background:#000"><img src="{os.path.basename(f)}" style="max-width:640px;zoom:0.5"><figcaption>{os.path.basename(f)}</figcaption></figure>' for f in fs)
open(O+'/sheet.html','w').write('<body style="margin:0;background:#333;color:#fff;font:14px monospace;width:1400px">'+cells)
PY
rm -f sheet.png; ( "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --no-first-run --hide-scrollbars --user-data-dir=$S/ch2 --window-size=1400,${2:-1400} --screenshot=$O/sheet.png file://$O/sheet.html >/dev/null 2>&1 & )
for i in $(seq 1 40); do [ -s $O/sheet.png ] && break; perl -e 'select(undef,undef,undef,0.5)'; done; pkill -f "$S/ch2"; echo sheet done
