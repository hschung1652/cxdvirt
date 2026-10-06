#!/usr/bin/env python3
"""join_felt.py — decompose CXDVirt's felt per-access latency.

Produces, for CXDVirt, the decomposition Cylon gets from its refault breakdown.
Cylon needs a guest->host TSC join (kvmwin_*.csv) because guest and host clocks
differ; CXDVirt has no VM and this machine's TSC is constant_tsc/nonstop_tsc, so
the fault handler and the userspace probe read the same counter and records join
by simple containment.

Inputs
  --felt   from felt_probe : tid,page_idx,tsc0,tsc1              (one row/access)
  --optb   from the module : tid,page_idx,outcome,tsc_in,tsc_dur,
                             dispatch,wait,evict,modeled,install,total

Join
  a handler record belongs to an access when (tid, page_idx) matches and tsc_in
  lies inside [tsc0, tsc1].  Several matches means the access faulted more than
  once.  tid is part of the key because two threads faulting the same page have
  overlapping windows: on page_idx alone one record would be credited to every
  one of them, inflating every stage total.

Output, the additive split of what the application waited for:

  felt = SUM over the access's faults of
             (dispatch + wait + evict + modeled + install)
       + re-entry gap                       (felt minus the handler spans)

  stage             Cylon counterpart
  ---------------   --------------------------
  modeled + evict   FTL+NAND
  wait              primary fill stall
  n_faults > 1      multi-exit refault
  re-entry gap      re-entry gap
  dispatch          (no counterpart; CXDVirt has no KVM/QEMU legs)

Run with the plots venv:
  /mnt/nvme/cxdvirt/cxdvirt/reproduction/plots/.venv/bin/python join_felt.py --felt f.csv --optb o.csv \
      --tsc-per-ns 2.194 --out joined.csv
"""
import argparse
import numpy as np

STAGES = ["dispatch", "wait", "evict", "modeled", "install"]
PCTS = [50, 90, 99, 99.9, 99.99]
CHUNK = 1 << 20          # accesses per vectorised block


def load(path, want, optional=()):
    """np.loadtxt + header-driven column selection; returns {name: 1-D array}.

    Columns in `optional` may be absent; they come back missing from the dict.
    """
    with open(path) as f:
        hdr = f.readline().strip().lstrip("#").strip().split(",")
    hdr = [h.strip() for h in hdr]
    missing = [w for w in want if w not in hdr and w not in optional]
    if missing:
        raise SystemExit(f"{path}: missing column(s) {missing}; header is {hdr}")
    want = [w for w in want if w in hdr]
    cols = [hdr.index(w) for w in want]
    arr = np.loadtxt(path, delimiter=",", skiprows=1, usecols=cols,
                     dtype=np.float64, ndmin=2)
    return {w: arr[:, i] for i, w in enumerate(want)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--felt", required=True)
    ap.add_argument("--optb", required=True)
    ap.add_argument("--tsc-per-ns", type=float, required=True,
                    help="cycles per ns; felt_probe prints it on stdout")
    ap.add_argument("--out", default=None, help="write the per-access join as CSV")
    ap.add_argument("--band", type=float, default=0.05,
                    help="relative half-width of the percentile band (default 0.05)")
    ap.add_argument("--drop-outcome", default="",
                    help="comma-separated outcomes to exclude from the join "
                         "(0 filled, 1 retry, 2 CACHED re-fault).  Use 2 to "
                         "compare against Cylon's per-fill breakdown, which "
                         "counts demand fills only.")
    a = ap.parse_args()
    drop = {int(x) for x in a.drop_outcome.split(",") if x.strip()}

    acc = load(a.felt, ["tid", "page_idx", "tsc0", "tsc1"], optional=["tid"])
    rec = load(a.optb, ["tid", "page_idx", "outcome", "tsc_in", "tsc_dur"] + STAGES,
               optional=["tid", "outcome"])

    if drop:
        if "outcome" not in rec:
            raise SystemExit("--drop-outcome needs an `outcome` column in --optb")
        keep = ~np.isin(rec["outcome"], sorted(drop))
        print(f"dropped {int((~keep).sum()):,} record(s) with outcome in "
              f"{sorted(drop)}; {int(keep.sum()):,} remain\n")
        rec = {k: v[keep] for k, v in rec.items()}

    # captures from before the tid column existed still join, but only a
    # single-threaded one is trustworthy: without tid, concurrent faults on one
    # page are credited to every thread that was inside its window
    use_tid = "tid" in acc and "tid" in rec
    if not use_tid:
        print("WARNING: no tid column - falling back to a page_idx-only join.\n"
              "         Valid ONLY for a single-threaded capture; with threads\n"
              "         it over-counts every stage.  Reload the module built\n"
              "         with the tid field to fix.\n")

    t0 = acc["tsc0"]
    t1 = acc["tsc1"]

    # one composite key, so the match is a single comparison.  int64, not the
    # float the CSVs load as: float64 carries 53 mantissa bits and this needs up
    # to 32 + 32, which would silently collide keys.
    def key(d):
        k = d["page_idx"].astype(np.int64) << np.int64(32)
        return (k | d["tid"].astype(np.int64)) if use_tid else k

    akey = key(acc)
    n = len(t0)
    if n == 0 or len(rec["tsc_in"]) == 0:
        raise SystemExit("empty input")

    # sort handler records by entry stamp, so every access window maps onto one
    # contiguous slice of records; page_idx then picks ours out of that slice
    order = np.argsort(rec["tsc_in"], kind="stable")
    rin = rec["tsc_in"][order]
    rkey = key(rec)[order]
    rdur = rec["tsc_dur"][order]
    rstage = np.column_stack([rec[s][order] for s in STAGES])

    lo = np.searchsorted(rin, t0, side="left")
    hi = np.searchsorted(rin, t1, side="right")
    cnt = np.maximum(hi - lo, 0)

    nfault = np.zeros(n, np.int64)
    hdur = np.zeros(n)                       # summed handler span, cycles
    first_in = np.full(n, np.inf)            # earliest handler entry, cycles
    last_out = np.zeros(n)                   # latest handler exit, cycles
    ssum = np.zeros((n, len(STAGES)))

    # expand each window into its candidate records without a Python loop over
    # accesses; chunked so a pathological overlap cannot blow up memory
    for base in range(0, n, CHUNK):
        sl = slice(base, min(base + CHUNK, n))
        c = cnt[sl]
        tot = int(c.sum())
        if tot == 0:
            continue
        ai = np.repeat(np.arange(sl.start, sl.stop), c)          # access index
        off = np.arange(tot) - np.repeat(np.cumsum(c) - c, c)    # 0..c_i-1
        ri = np.repeat(lo[sl], c) + off                          # record index

        m = rkey[ri] == akey[ai]
        ai, ri = ai[m], ri[m]
        if ai.size == 0:
            continue
        np.add.at(nfault, ai, 1)
        np.add.at(hdur, ai, rdur[ri])
        # earliest handler entry and latest handler exit for each access, so the
        # gap can be split at the handler boundary (see gap_pre/gap_post below)
        np.minimum.at(first_in, ai, rin[ri])
        np.maximum.at(last_out, ai, rin[ri] + rdur[ri])
        for k in range(len(STAGES)):
            np.add.at(ssum[:, k], ai, rstage[ri, k])

    felt_ns = (t1 - t0) / a.tsc_per_ns
    hdur_ns = hdur / a.tsc_per_ns
    gap_ns = np.maximum(felt_ns - hdur_ns, 0.0)   # everything outside the handler

    # Split that gap at the handler boundary.  The module's `emulation overhead`
    # (dispatch+install) covers only code INSIDE cxl_page_fault_handler, so the
    # kernel's own fault path -- which exists solely because the emulator zapped
    # the PTE -- lands in the gap and is invisible.  tsc_in/tsc_dur already
    # locate the handler inside [tsc0, tsc1], so no extra instrumentation is
    # needed to separate them:
    #   gap_pre  = trap + kernel fault prologue, up to .fault being called
    #   gap_post = kernel epilogue + return to user + re-execute + TLB refill
    # (with >1 fault per access the time BETWEEN handlers also lands in post).
    hit = nfault > 0
    gap_pre = np.zeros(n); gap_post = np.zeros(n)
    gap_pre[hit]  = np.maximum(first_in[hit] - t0[hit], 0.0) / a.tsc_per_ns
    gap_post[hit] = np.maximum(t1[hit] - last_out[hit], 0.0) / a.tsc_per_ns

    miss = nfault > 0
    nmiss = int(miss.sum())
    print(f"accesses      : {n:,}")
    print(f"  faulting    : {nmiss:,} ({100 * nmiss / n:.1f}%)")
    if nmiss:
        multi = int((nfault[miss] > 1).sum())
        print(f"  multi-fault : {multi:,} ({100 * multi / nmiss:.2f}% of faulting)")
    print(f"  hits        : {n - nmiss:,}")
    unmatched = len(rin) - int(nfault.sum())
    print(f"  handler records unmatched: {unmatched:,} of {len(rin):,}"
          "   (other threads / pre-run residue)\n")

    if not nmiss:
        raise SystemExit("nothing joined - check that both captures cover the "
                         "same run, and that felt_probe mapped from offset 0 so "
                         "its page_idx base matches the module's")

    f = felt_ns[miss]
    names = STAGES + ["gap"]
    print(f"{'pct':>8}{'felt':>10}" + "".join(f"{s:>10}" for s in names) +
          f"{'faults':>8}{'n':>9}")
    print(f"{'':>8}{'(ns)':>10}" + "".join(f"{'(ns)':>10}" for _ in names) +
          f"{'/acc':>8}{'':>9}")
    for p in PCTS:
        v = np.percentile(f, p)
        band = miss & (np.abs(felt_ns - v) <= a.band * v)
        if band.sum() < 20:                  # widen rather than report noise
            band = miss & (np.abs(felt_ns - v) <= 5 * a.band * v)
        row = [ssum[band, k].mean() for k in range(len(STAGES))]
        row.append(gap_ns[band].mean())
        print(f"p{p:<7g}{v:10.0f}" + "".join(f"{x:10.0f}" for x in row) +
              f"{nfault[band].mean():8.2f}{int(band.sum()):9,d}")

    print("\nfelt = sum of the stages over every fault of the access, + gap.")
    print("gap  = fault entry/exit, TLB, and resume - what the application waits")
    print("       for that the handler's own stamps cannot see.")
    print("Stage columns are ns from local_clock(); felt and gap are TSC cycles")
    print(f"converted at {a.tsc_per_ns:.4f} cycles/ns.")

    if a.out:
        hdr = "felt_ns,gap_ns,gap_pre_ns,gap_post_ns,n_faults," + ",".join(STAGES)
        cols = [felt_ns, gap_ns, gap_pre, gap_post, nfault.astype(float)] + \
               [ssum[:, k] for k in range(len(STAGES))]
        np.savetxt(a.out, np.column_stack(cols)[miss], delimiter=",",
                   header=hdr, comments="", fmt="%.1f")
        print(f"\nwrote {a.out} ({nmiss:,} faulting accesses)")


if __name__ == "__main__":
    main()