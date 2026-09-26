#!/bin/bash
# Launch the spike exactly as documented (cargo run --example), leave it idle 8 s, then close it.
cd "${W:?set W to the spike worktree}" || exit 1
S=${S:?set S to a scratch dir}/t11
SPIKE_LOG=$S/cargo-run-idle.log cargo run --offline --example causal_motion_spike > $S/cargo-run.txt 2>&1 &
pid=$!
sleep 8
pkill -f "target/debug/examples/causal_motion_spike"
wait $pid
echo "cargo run exit=$? (terminated by pkill after 8 s is expected)"
tail -3 $S/cargo-run.txt
echo "frames logged while idle: $(grep -c '^frame' $S/cargo-run-idle.log)"
grep '^frame' $S/cargo-run-idle.log | tail -2 | cut -c1-160
