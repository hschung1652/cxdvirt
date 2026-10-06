#!/usr/bin/env bash
# fetch.sh -- fetch everything this artifact builds on but does not ship.
#
#   ./scripts/fetch.sh            # NVMeVirt + benchmarks + YCSB + Cylon's MIO
#   ./scripts/fetch.sh --cylon    # ... + all of Cylon, for the Cylon-side experiments
#
#   ../emulator/nvmevirt/  the emulator's module source: upstream NVMeVirt with
#                   the CXDVirt patch, prepared by the emulator's own
#                   bin/get_nvmevirt.sh
#   bench/<name>    the benchmarks in bench/PINS.txt, at the pinned commits, with
#                   bench-patches/ applied (ndctl goes to ndctl/, where the
#                   bring-up scripts look for it)
#   bench/ycsb      YCSB 0.17.0's Redis binding, plus ycsb-configs/ as
#                   bench/ycsb/cxdvirt-configs
#   cylon-tree/     Cylon (MoatLab/Cylon) at cylon/BASE_COMMIT with
#                   cylon/cylon-instrumentation.patch applied.  By default only
#                   Cylon-scripts/eval-mio is checked out -- MIO is all the CXDVirt
#                   side needs (Fig. 5).  --cylon checks out the rest: CylonLinux
#                   (the 6.4.6 host kernel), CylonFEMU and the guest tools.
#
# Everything is pinned rather than vendored: a commit hash is a stronger claim
# about what ran than a copied tree is.  Idempotent; re-running skips what is
# already there.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CXDVIRT_ROOT:-$(cd "$HERE/.." && pwd)}
PINS=$ROOT/bench/PINS.txt
# A tree cloned to a path other than the one it was staged at: rewrite the
# scripts' absolute paths first.  Idempotent, and silent when nothing moved.
[ -x "$HERE/relocate.sh" ] && "$HERE/relocate.sh" | sed -n '/^rewriting/p'
CYLON_FULL=0
for a in "$@"; do [ "$a" = --cylon ] && CYLON_FULL=1; done

# ---- the emulator's module source --------------------------------------------
# ../emulator is the emulator.  Its module ships as a patch against upstream
# NVMeVirt; its own bin/get_nvmevirt.sh clones the base, applies the patch and
# verifies the result.
EMU=${CXDVIRT_EMU:-$(cd "$ROOT/.." && pwd)/emulator}
if [ -d "$EMU/nvmevirt" ]; then
  echo "== emulator module source already present"
elif [ -x "$EMU/bin/get_nvmevirt.sh" ]; then
  "$EMU/bin/get_nvmevirt.sh"
else
  echo "ERROR: no emulator at $EMU" >&2; exit 1
fi

# ---- benchmarks, pinned ------------------------------------------------------
[ -f "$PINS" ] || { echo "ERROR: $PINS missing" >&2; exit 1; }
while read -r name sha url; do
  case "$name" in ''|\#*) continue ;; esac
  [ "$url" = "-" ] && continue
  case "$sha" in *[!0-9a-f]*) echo "skip $name (no vcs pin)"; continue ;; esac
  dest="$ROOT/bench/$name"
  # init_device.sh, teardown_device.sh and build.sh look for ndctl at the root
  [ "$name" = ndctl ] && dest="$ROOT/ndctl"
  if [ -d "$dest/.git" ]; then
    echo "== $name already present"
  else
    echo "== $name  $url  @ $sha"
    git clone "$url" "$dest"
  fi
  git -C "$dest" checkout --quiet "$sha"

  # Splash-4 does not measure finely enough as published; see
  # bench-patches/README.md.  Applied here so a fetch produces a tree that can
  # actually reproduce Fig. 7(a).
  case "$name" in
    Splash-4) pat=$ROOT/bench-patches/splash4-ns-clock.patch ;;
    *) pat= ;;
  esac
  if [ -n "$pat" ] && [ -f "$pat" ]; then
    if git -C "$dest" apply --check "$pat" 2>/dev/null; then
      git -C "$dest" apply "$pat"; echo "   applied $(basename "$pat")"
    else
      echo "   $(basename "$pat") already applied (or does not apply cleanly)"
    fi
  fi
done < "$PINS"

# ---- YCSB 0.17.0, Redis binding ----------------------------------------------
# The drivers run bench/ycsb/bin/ycsb, which needs python2 on PATH and a JVM.
Y=$ROOT/bench/ycsb
YCSB_URL=${YCSB_URL:-https://github.com/brianfrankcooper/YCSB/releases/download/0.17.0/ycsb-redis-binding-0.17.0.tar.gz}
if [ -x "$Y/bin/ycsb" ]; then
  echo "== ycsb already present"
else
  echo "== ycsb  $YCSB_URL"
  mkdir -p "$Y"
  curl -fL "$YCSB_URL" | tar -xz -C "$Y" --strip-components=1 || {
    echo "   download failed: unpack ycsb-redis-binding-0.17.0.tar.gz into $Y by hand" >&2; }
fi
# The property files the runs used.  These are not upstream: they set the record
# counts, the 10x100 B field layout, the thread count and the percentile list
# the figures read.  A run with stock YCSB defaults is a different experiment.
if [ -d "$ROOT/ycsb-configs" ]; then
  echo "== ycsb configs -> bench/ycsb/cxdvirt-configs"
  mkdir -p "$Y/cxdvirt-configs"
  cp "$ROOT/ycsb-configs/"*.properties "$Y/cxdvirt-configs/"
fi

# ---- Cylon ---------------------------------------------------------------------
C=$ROOT/cylon-tree
CYLON_URL=${CYLON_URL:-https://github.com/MoatLab/Cylon.git}
CPATCH=$ROOT/cylon/cylon-instrumentation.patch
MIO=Cylon-scripts/eval-mio
if [ -f "$CPATCH" ] && [ -f "$ROOT/cylon/BASE_COMMIT" ]; then
  CSHA=$(cat "$ROOT/cylon/BASE_COMMIT")
  if [ ! -d "$C/.git" ]; then
    echo "== cylon  $CYLON_URL  @ $CSHA  ($([ "$CYLON_FULL" = 1 ] && echo full || echo "$MIO only"))"
    if [ "$CYLON_FULL" = 1 ]; then
      git clone --no-checkout "$CYLON_URL" "$C"
    else
      # A blob-less, sparse clone: CylonLinux alone is a whole kernel tree, and
      # the CXDVirt side needs only MIO's four source files.
      git clone --filter=blob:none --no-checkout "$CYLON_URL" "$C"
      git -C "$C" sparse-checkout init --cone
      git -C "$C" sparse-checkout set "$MIO"
    fi
    git -C "$C" -c advice.detachedHead=false checkout --quiet "$CSHA"
  fi
  # .git/cxdvirt-applied records how much of the patch is in: "mio" or "full".
  applied=$(cat "$C/.git/cxdvirt-applied" 2>/dev/null || echo none)
  if [ "$CYLON_FULL" = 1 ] && [ "$applied" != full ]; then
    # Leaving sparse mode refuses while tracked files are modified, so take the
    # MIO hunks back out first and apply the whole patch once everything is out.
    [ "$applied" = mio ] && git -C "$C" apply -R --include="$MIO/*" "$CPATCH"
    git -C "$C" sparse-checkout disable 2>/dev/null || true
    git -C "$C" apply "$CPATCH"
    echo full > "$C/.git/cxdvirt-applied"
    mkdir -p "$C/CylonLogs"
    echo "   applied cylon-instrumentation.patch (full)"
  elif [ "$applied" = none ]; then
    git -C "$C" apply --include="$MIO/*" "$CPATCH"
    echo mio > "$C/.git/cxdvirt-applied"
    echo "   applied cylon-instrumentation.patch ($MIO)"
  else
    echo "== cylon already present ($applied)"
  fi
fi

cat <<'EOT'

Next: ./scripts/build.sh all.  YCSB's launcher needs python2 on PATH and a JVM;
Redis runs with the redis.conf in ycsb-configs/ (io-threads 1, latency-tracking
yes, persistence off).
EOT
[ "$CYLON_FULL" = 1 ] && cat <<'EOT'

Cylon: build and boot cylon-tree/CylonLinux (the 6.4.6 host kernel with the KVM
instrumentation), build cylon-tree/CylonFEMU, and prepare a guest image, as in
cylon-tree/README.md and cylon-tree/docs/.  FEMU's femu-copy-scripts.sh puts the
patched femu-scripts/run-cxlssd.sh into build-femu/; that is the launch script
every Cylon target assumes.  scripts/README.md, "Cylon experiments", lists what
the guest needs.
EOT
exit 0
