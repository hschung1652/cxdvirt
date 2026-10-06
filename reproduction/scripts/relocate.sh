#!/usr/bin/env bash
# relocate.sh -- rewrite absolute paths after moving the artifact.
#
# Run once after unpacking somewhere other than where it was staged:
#     reproduction/scripts/relocate.sh
#
# Idempotent.  Rewrites shell and python under scripts/, drivers/, plots/tools/
# and tools/; leaves data files alone, since the paths inside a run log are a
# record of where that run happened and should not be edited.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
NEW=${1:-$(cd "$HERE/../.." && pwd)}
OLD=$(grep -m1 '^ARTIFACT_ROOT=' "$HERE/.artifact_root" 2>/dev/null | cut -d= -f2-)
[ -n "${OLD:-}" ] || { echo "ERROR: scripts/.artifact_root missing" >&2; exit 1; }
[ "$OLD" = "$NEW" ] && { echo "already at $NEW"; exit 0; }
echo "rewriting $OLD -> $NEW"
find "$NEW/reproduction/scripts" "$NEW/reproduction/drivers" "$NEW/reproduction/plots/tools" "$NEW/reproduction/tools" -type f \( -name '*.sh' -o -name '*.py' \) -print0 |
  xargs -0 sed -i "s|$OLD|$NEW|g"
echo "ARTIFACT_ROOT=$NEW" > "$HERE/.artifact_root"
echo done
