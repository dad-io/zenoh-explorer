#!/bin/bash
# kbpass.sh <label> <ntabs> : Tab through the focused window, diff each capture vs baseline, crop focus region
S=$(dirname "$0"); L=$1; N=$2; O=$S/kb/$L; rm -rf $O; mkdir -p $O
PID=$(cat $S/explorer.pid); $S/win activate $PID
read ID WX WY WW WH < <($S/win find zenoh-explorer | head -1)
screencapture -x -o -l $ID $O/base.png
for i in $(seq -w 1 $N); do
  $S/win key 48; sleep 0.45
  screencapture -x -o -l $ID $O/t$i.png
  bb=$($S/pdiff $O/base.png $O/t$i.png); echo "tab $i: $bb" >> $O/log.txt
  if [ "$bb" != "none" ] && [ "$bb" != "size-mismatch" ]; then
    set -- $bb; x=$(( $1>40?$1-40:0 )); y=$(( $2>40?$2-40:0 )); w=$(( $3+80 )); h=$(( $4+80 ))
    sips -c $h $w --cropOffset $y $x $O/t$i.png --out $O/crop$i.png >/dev/null 2>&1
  fi
done
cat $O/log.txt
