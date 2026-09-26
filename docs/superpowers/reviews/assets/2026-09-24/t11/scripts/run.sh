#!/bin/bash
# usage: run.sh <profile debug|release> <script> <tag> [shots] [rows]
set -u
W=${W:?set W to the spike worktree}
S=${S:?set S to a scratch dir}/t11
mkdir -p "$S"
prof=$1; scr=$2; tag=$3; shots=${4:-}; rows=${5:-0}
export SPIKE_SCRIPT=$scr SPIKE_LOG=$S/$tag.log SPIKE_ROWS=$rows
if [ -n "$shots" ]; then rm -rf "$S/shots-$tag"; mkdir -p "$S/shots-$tag"; export SPIKE_SHOTS=$S/shots-$tag; fi
"$W/target/$prof/examples/causal_motion_spike" &
pid=$!
( sleep 40; kill $pid 2>/dev/null ) &
wait $pid
echo "exit=$? log=$S/$tag.log lines=$(wc -l < $S/$tag.log)"
