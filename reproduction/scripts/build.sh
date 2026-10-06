#!/usr/bin/env bash
# build.sh -- build everything the experiments need, in dependency order.
#
#   ./scripts/build.sh              # module + tools (+ MIO when Cylon's tree is fetched)
#   ./scripts/build.sh all          # + ndctl + benchmarks (needs ./scripts/fetch.sh first)
#
# The EMULATOR is built by the emulator (../emulator/bin/cxdvirt build: nvmev.ko,
# daxmalloc.so, and its example), exactly as a user of the emulator alone would
# build it.  This script adds what only the paper's experiments need: felt_probe,
# MIO, ndctl and the benchmarks.
#
# Does NOT build the host kernel; see ../emulator/kernel/README.md.  nvmev.ko will
# not load on a kernel without its patch, and modpost reports the missing
# exports as undefined at build time if you try.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
EMU=${CXDVIRT_EMU:-$(cd "$ROOT/.." && pwd)/emulator}
KDIR=${KDIR:-/lib/modules/$(uname -r)/build}
JOBS=${JOBS:-$(nproc)}
WHAT=${1:-core}

say() { printf '\n== %s\n' "$*"; }

[ -x "$EMU/bin/cxdvirt" ] || { echo "ERROR: no emulator at $EMU -- run scripts/fetch.sh." >&2; exit 1; }
say "emulator: module + daxmalloc (against $KDIR)"
KDIR="$KDIR" "$EMU/bin/cxdvirt" build
modinfo -F srcversion "$EMU/nvmevirt/nvmev.ko" | sed 's/^/   srcversion /'

# felt_probe: brackets each page touch with rdtscp, so the join can separate what
# the application waits for from what the fault handler recorded (Fig. 6).
say "felt_probe"
( cd "$ROOT/tools/felt" && gcc -O2 -o felt_probe felt_probe.c -pthread )

# MIO, Cylon's microbenchmark (Fig. 5 on the host; the guest builds its own with
# drivers/cylon/guest_build_mio.sh).  Its source comes with Cylon's tree.
MIO_SRC=$ROOT/cylon-tree/Cylon-scripts/eval-mio/src
if [ -f "$MIO_SRC/main.c" ]; then
  say "MIO (bench512_W)"
  "$HERE/build_mio.sh" "$MIO_SRC" bench512_W
else
  say "MIO: skipped -- $MIO_SRC missing (scripts/fetch.sh fetches it)"
fi

[ "$WHAT" = all ] || { echo; echo "core build done.  './scripts/build.sh all' also builds ndctl and the benchmarks."; exit 0; }

# ndctl v78 or newer: distro packages older than v78 have no `cxl` subcommand at
# all, which is the one this artifact needs.
say "ndctl (cxl + daxctl)"
# Only cxl(1) and daxctl(1) are needed, so only they are built, with the options
# the paper's ndctl was configured with.  The tracing, documentation and keyring
# features pull in libraries (libtraceevent, libtracefs, asciidoctor, keyutils)
# that neither tool uses, and the ndctl(1) binary does not build without keyutils.
( cd "$ROOT/ndctl" && { [ -f build/build.ninja ] || { rm -rf build &&
    meson setup build -Ddocs=disabled -Dasciidoctor=disabled -Dlibtracefs=disabled \
      -Dkeyutils=disabled -Dtest=disabled -Diniparserdir=iniparser; }; } &&
  meson compile -C build cxl/cxl:executable daxctl/daxctl:executable )

say "benchmarks"
( cd "$ROOT/bench/Splash-4" && make -j"$JOBS" )   # -> Splash-4/ocean-contiguous_partitions/OCEAN-CONT
# MALLOC=libc: daxmalloc interposes malloc(), which Redis's default jemalloc
# bypasses; the drivers refuse any other build.
( cd "$ROOT/bench/redis" && make -j"$JOBS" MALLOC=libc )
echo
echo "all done"
