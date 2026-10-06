#!/usr/bin/env bash
# get_nvmevirt.sh -- build the CXDVirt module tree from upstream + our patch.
#
#   ./bin/get_nvmevirt.sh            # clone upstream, apply, verify -> ./nvmevirt
#   NVMEVIRT_SRC=/path ./bin/get_nvmevirt.sh    # use an existing clone instead
#
# The artifact ships our changes as a patch rather than a copy of the tree, so
# the boundary between upstream NVMeVirt and CXDVirt is something you can check
# instead of something a README asserts.  `git apply --stat` on the patch is the
# contribution, in full, in one place.
#
# Needs network the first time.  If you have no network, or if upstream has
# moved, point NVMEVIRT_SRC at any clone that contains the base commit -- the
# patch is against a commit hash, not a branch, so a fork or an old mirror works
# as long as that object is present.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CXDVIRT_ROOT:-$(cd "$HERE/.." && pwd)}
P=$ROOT/nvmevirt-patch
DEST=${DEST:-$ROOT/nvmevirt}

BASE=$(cat "$P/BASE_COMMIT")
REMOTE=${NVMEVIRT_REMOTE:-$(cat "$P/BASE_REMOTE")}

[ -e "$DEST" ] && { echo "$DEST already exists; remove it or set DEST=" >&2; exit 1; }

if [ -n "${NVMEVIRT_SRC:-}" ]; then
	echo "== cloning from $NVMEVIRT_SRC"
	git clone -q --no-checkout "$NVMEVIRT_SRC" "$DEST"
else
	echo "== cloning $REMOTE"
	# Not --depth 1: the patch is against a specific commit, and a shallow
	# clone of the default branch may not contain it.
	git clone -q "$REMOTE" "$DEST"
fi

git -C "$DEST" cat-file -e "$BASE^{commit}" 2>/dev/null || {
	echo "base commit $BASE is not in that repository." >&2
	echo "Upstream may have rewritten history; point NVMEVIRT_SRC at a clone" >&2
	echo "that still has it." >&2
	exit 1; }

echo "== checking out $BASE"
git -C "$DEST" -c advice.detachedHead=false checkout -q "$BASE"

# --binary is required: two of the added files are compiled ACPI tables
# (cxl_hostbridge.aml, cxl_root.aml).  A text-only apply would drop them and the
# module would fail to build with no obvious cause.
echo "== applying cxdvirt-nvmevirt.patch"
git -C "$DEST" apply --binary --whitespace=nowarn "$P/cxdvirt-nvmevirt.patch"

echo "== verifying"
( cd "$DEST" && sha256sum -c --quiet "$P/SHA256SUMS" ) || {
	echo "the patched tree does not match the manifest -- do not trust this build." >&2
	exit 1; }
echo "   $(wc -l < "$P/SHA256SUMS") files match SHA256SUMS"
echo
echo "module source ready at $DEST"
echo "next: ./bin/cxdvirt build"
