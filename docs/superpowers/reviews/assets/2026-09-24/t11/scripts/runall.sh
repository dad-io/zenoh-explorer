#!/bin/bash
R=$(dirname "$0")/run.sh
# timing / repaint-policy runs (no readback)
bash $R release reveal rel-reveal
bash $R release reveal rel-reveal-rows2000 "" 2000
bash $R release supersede rel-supersede
bash $R release reduced rel-reduced
bash $R release toggle rel-toggle
bash $R debug reveal dev-reveal
# frame capture runs (GL framebuffer readback every frame)
bash $R release reveal shots-reveal 1
bash $R release supersede shots-supersede 1
bash $R release reduced shots-reduced 1
bash $R release toggle shots-toggle 1
