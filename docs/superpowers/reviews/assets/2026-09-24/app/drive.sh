S=<scratch>
PID=$(cat $S/explorer.pid)
geom(){ read ID WX WY WW WH < <($S/win find zenoh-explorer | head -1); }
# coords are window points (= capture pixels / 2)
c(){ geom; $S/win activate $PID; $S/win click $((WX+$1)) $((WY+$2)); sleep ${3:-0.8}; }
shot(){ geom; screencapture -x -o -l $ID "$1"; }
view(){ shot $S/raw.png; sips -Z $(( WW )) $S/raw.png --out $S/v.png >/dev/null; }
