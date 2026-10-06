#!/usr/bin/env bash
# guest_build_mio.sh -- put the patched MIO into the Cylon guest and build it
# there, against the guest's own glibc and libnuma.
#
#   ./guest_build_mio.sh            # needs the VM up, ssh on localhost:8080
#
# Copies main.c, utils.c and utils.h from the patched Cylon tree, plus
# scripts/build_mio.sh, into the guest's MIO directory and builds bench512_W
# (Fig. 5) and bench_4096_tsc_chase (Figs. 1 and 6) in place.  The capture
# scripts run MIO from that directory.  The guest needs gcc and libnuma-dev;
# the binaries stay on the guest's disk across VM relaunches, so this is a
# once-per-guest-image step.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CXDVIRT_ROOT:-$(cd "$HERE/../.." && pwd)}
SRC=${SRC:-$ROOT/cylon-tree/Cylon-scripts/eval-mio/src}
GUEST_DIR=${GUEST_DIR:-'~/Cylon/Cylon-scripts/eval-mio/src'}
G=(ssh -p 8080 -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@localhost)

[ -f "$SRC/main.c" ] || { echo "ERROR: no patched MIO at $SRC -- scripts/fetch.sh --cylon" >&2; exit 1; }
"${G[@]}" true || { echo "ERROR: guest not reachable on :8080 (VM up?)" >&2; exit 1; }
"${G[@]}" "mkdir -p $GUEST_DIR" || exit 1
# scp expands ~ on the remote side, so the quoted GUEST_DIR works as a target
scp -q -P 8080 -o StrictHostKeyChecking=no \
    "$SRC/main.c" "$SRC/utils.c" "$SRC/utils.h" "$ROOT/scripts/build_mio.sh" \
    "root@localhost:$GUEST_DIR/" || exit 1
"${G[@]}" "cd $GUEST_DIR && sh build_mio.sh . all" || {
  echo "ERROR: build failed in the guest (gcc and libnuma-dev installed?)" >&2; exit 1; }
