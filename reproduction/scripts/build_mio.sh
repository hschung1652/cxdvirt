#!/bin/sh
# build_mio.sh -- build the two MIO binaries the paper uses.
#
#   scripts/build_mio.sh [SRC] [bench512_W|bench_4096_tsc_chase|all]
#
# SRC is MIO's source directory with cylon/cylon-instrumentation.patch applied:
# <cylon-tree>/Cylon-scripts/eval-mio/src on the host (the default), or the
# copy drivers/cylon/guest_build_mio.sh places in the Cylon guest.  POSIX sh, so
# it runs unchanged inside the guest.
#
#   bench512_W            Fig. 5, both emulators.  512 B chase nodes, eight per
#                         4 KiB page, so a sequential walk misses once per page.
#                         Built as the published binary was: Cylon's Makefile
#                         flags (no -O, so -O0) plus -DCHASE_STRIDE=512.  With
#                         gcc 9.4 on the paper's host this reproduces the Fig. 5
#                         binary byte for byte (md5 22b3fc46b6cda0635b93f88061ff60ac).
#   bench_4096_tsc_chase  Figs. 1 and 6, Cylon side, built in the guest.  4 KiB
#                         nodes, one per page, so every access misses, and
#                         -DPER_ACCESS_TSC prints "<start tsc> <cycles>" per access
#                         for the guest-TSC join in capture_guestfelt_paired.sh.
#
# Both must accept -C (the option string printed at the end must read ...RCSW).
# Without -C, MIO restarts each timed sample at the next node in ADDRESS order:
# -R then shuffles a ring nobody follows, and the "pointer chase" is a scan.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
if [ -n "${1:-}" ]; then
  SRC=$1
elif [ -f ./main.c ] && grep -q op_ptr_chase_cont ./main.c; then
  SRC=.
else
  SRC=$HERE/../cylon-tree/Cylon-scripts/eval-mio/src
fi
WHAT=${2:-all}
case "$WHAT" in bench512_W|bench_4096_tsc_chase|all) ;;
  *) echo "usage: $0 [SRC] [bench512_W|bench_4096_tsc_chase|all]" >&2; exit 2 ;; esac
[ -f "$SRC/main.c" ] || { echo "ERROR: no MIO source at $SRC -- run scripts/fetch.sh first" >&2; exit 1; }
grep -q op_ptr_chase_cont "$SRC/main.c" ||
  { echo "ERROR: $SRC/main.c lacks -C: cylon-instrumentation.patch is not applied" >&2; exit 1; }
grep -q PER_ACCESS_TSC "$SRC/main.c" ||
  { echo "ERROR: $SRC/main.c lacks PER_ACCESS_TSC: re-apply cylon-instrumentation.patch" >&2; exit 1; }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
cd "$SRC"

# Compiled from inside SRC with bare file names, as the original was: gcc puts
# the source name as given into the symbol table, so a path would change the
# binary without changing the code.
build() {   # name, flags...
  name=$1; shift
  for f in utils main; do
    # -W -Wall warns about MIO's own code; show that only if the build fails
    gcc "$@" -c -o "$T/$f.o" "$f.c" 2> "$T/$f.log" || { cat "$T/$f.log" >&2; exit 1; }
  done
  gcc -o "$name" "$T/utils.o" "$T/main.o" "$@" -lpthread -lnuma -lm
  printf '%-22s %s  options %s\n' "$name" "$(md5sum "$name" | cut -d' ' -f1)" \
    "$(grep -a -o 't:m:i:r:I:T:P:c:R[A-Z]*' "$name" | head -1)"
}
case "$WHAT" in
  bench512_W|all)
    build bench512_W -I. -W -Wall -Wextra -Wuninitialized -Wstrict-aliasing \
          -march=native -DCHASE_STRIDE=512 ;;
esac
case "$WHAT" in
  bench_4096_tsc_chase|all)
    build bench_4096_tsc_chase -I. -W -Wall -O0 -march=native \
          -DCHASE_STRIDE=4096 -DPER_ACCESS_TSC ;;
esac
