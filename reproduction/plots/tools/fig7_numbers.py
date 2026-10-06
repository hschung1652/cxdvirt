#!/usr/bin/env python3
"""fig7_numbers.py -- every number the paper's Fig. 7 text quotes, recomputed.

Reads the same dataset extract_fig7.py does -- the shipped reference by default,
your own runs under FIG7_DATA (make_figures.sh sets it to results/) -- and
prints each quantity next to the value the paper quotes, so a re-collection can
be checked claim by claim rather than by eye against the bars.

PER-MISS OVERHEAD (the "1.1-2.6 us against 15.8-17.3 us" sentence) is computed
at 2.2x WSS only:

    overhead/miss = (T - T_native - fetches*c - inline_wb) / misses

  T          init + solve, CXDVirt the mean of its reps
  T_native   the placement-matched remote-DRAM line
  inline_wb  CXDVirt's write-back stall inside faults, sync_wb_applied_ns / 8
             threads (modelled latency the drain did not absorb); Cylon's
             write-backs are all asynchronous, so 0
  c          how much wall time one NAND read costs after concurrency.  Not
             directly measurable; the range spans three values of it:
               measured  from a mode-0 control (cxl_wr_alloc=0 at 2.2x, which
                         removes ~56% of the fetches and leaves the evictions):
                         c = (dT + d_inline_wb) / d_fetches
               nominal   (tR + channel transfer) / 8 threads = 0.779 us
               zero      every read fully hidden
The mode-0 control is run_experiment.sh fig7a-ocean-mode0.  The paper's c came
from the Splash-3 pair in plots/data/ocean_wss (1.726 us); a re-collection uses
its own Splash-4 pair when present and says so when it is not.
"""
import glob
import os
import re
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_fig7 as X  # noqa: E402

TH = 8
T_R_NS, XFER_NS = 3000, 3232
CACHE22 = X.OCEAN_CACHES["2.2"]
# The paper's fetch-cost pair (Splash-3, same drain setting as the mode-0 run).
PAPER_FETCH_PAIR = ("ocean_wss/0825_203317_m2_c6456_p8_r1",
                    "ocean_wss/0826_115431_m0_c6456_p8_r1")


def num(txt, pat):
    m = re.search(pat, txt, re.M)
    if not m:
        raise SystemExit(f"missing: {pat}")
    return float(m.group(1))


def nvmev(stem):
    t = open(stem + ".nvmev", errors="replace").read()
    return dict(misses=num(t, r"^cache_misses:\s*([0-9]+)"),
                fetches=num(t, r"^nand_reads:\s*([0-9]+)"),
                wb=num(t, r"^sync_wb_applied_ns:\s*([0-9]+)") / 1e9 / TH,
                total=sum(X.ocean_run(stem + ".run")))


def cylon_buffer(stem):
    t = open(stem + ".buffer", errors="replace").read()
    rm = int(re.search(r"Buffer read:\s*\d+ hit/\s*(\d+) miss", t).group(1))
    wm = int(re.search(r"Buffer write:\s*\d+ hit/\s*(\d+) miss", t).group(1))
    ft = num(t, r"First-touch programs: DEFERRED to eviction \((\d+) skipped\)") \
        if "First-touch programs: DEFERRED" in t else 0.0
    inl = num(t, r"WB charge:\s*(\d+) inline") / 1e9 / TH if "WB charge:" in t else 0.0
    return dict(misses=rm + wm, fetches=rm + wm - ft, wb=inl,
                total=sum(X.ocean_run(stem + ".run")))


def fetch_cost():
    """-> (c_us, provenance) from a mode-0 / mode-2 pair at 2.2x, or None."""
    if X.CUSTOM:
        m0 = sorted(h for d in X.OCEAN_DIRS
                    for h in glob.glob(f"{X.DATA}/{d}/*_m0_c{CACHE22}_p{X.OCEAN_P}_r*.run"))
        m2 = sorted(h for d in X.OCEAN_DIRS
                    for h in glob.glob(f"{X.DATA}/{d}/*_m2_c{CACHE22}_p{X.OCEAN_P}_r*.run"))
        if not (m0 and m2):
            return None
        a = [nvmev(p[:-4]) for p in m2]
        b = [nvmev(p[:-4]) for p in m0]
        prov = f"mode-0 control, {len(b)} run(s) vs {len(a)} mode-2 rep(s)"
    else:
        a = [nvmev(f"{X.DATA}/{PAPER_FETCH_PAIR[0]}")]
        b = [nvmev(f"{X.DATA}/{PAPER_FETCH_PAIR[1]}")]
        prov = "the paper's Splash-3 mode-0 / mode-2 pair"
    mean = lambda rs, k: st.mean(r[k] for r in rs)
    dT = mean(a, "total") - mean(b, "total")
    dWB = mean(b, "wb") - mean(a, "wb")
    dF = mean(a, "fetches") - mean(b, "fetches")
    return (dT + dWB) / dF * 1e6, prov


def main():
    o, on, r, rn = X.ocean(), X.ocean_native(), X.redis(), X.redis_native()
    nat = sum(on["heap"])
    rnat = st.mean(rn["heap"]["0.35"])
    print(f"dataset: {X.DATA}   mode: {'custom (your runs)' if X.CUSTOM else 'paper (reference)'}\n")

    def row(label, value, paper):
        print(f"  {label:<58}{value:>16}   paper: {paper}")

    print("Fig. 7(a) OCEAN")
    tot = {w: (sum(v["cxd"]), sum(v["cyl"])) for w, v in o.items()}
    ratios = [c / x for x, c in tot.values()]
    row("CXDVirt runtime reduction over Cylon", f"{min(ratios):.2f}-{max(ratios):.2f}x", "3.06-3.51x")
    row("CXDVirt vs remote DRAM at 0.35x", f"{100*(tot['0.35'][0]/nat-1):+.1f}%", "within 5%")
    row("Cylon vs remote DRAM at 0.35x", f"{tot['0.35'][1]/nat:.2f}x", "3.6x")

    # per-miss overhead at 2.2x
    reps = [nvmev(p[:-4]) for d in X.OCEAN_DIRS for p in
            sorted(glob.glob(f"{X.DATA}/{d}/*_m{X.OCEAN_MODE}_c{CACHE22}_p{X.OCEAN_P}_r*.run"))]
    cyl_stem = X._pick(f"{X.DATA}/{X.OCEAN_CYLON_DIR}/cylon_n*_p{X.OCEAN_P}_c{CACHE22}.run",
                       "Cylon OCEAN 2.2x")[:-4]
    have_counters = reps and all(os.path.exists(f"{X.DATA}/{d}") for d in X.OCEAN_DIRS) \
        and os.path.exists(cyl_stem + ".buffer")
    fc = fetch_cost()
    if not have_counters:
        print("  per-miss overhead: device counters (.nvmev / .buffer) missing -- skipped")
    else:
        cx = {k: st.mean(x[k] for x in reps) for k in reps[0]}
        cy = cylon_buffer(cyl_stem)
        cs = [(0.0, "zero")]
        cs.insert(0, ((T_R_NS + XFER_NS) / TH / 1e3, "nominal"))
        if fc:
            cs.insert(0, (fc[0], "measured"))
        per = lambda d, c: 1e6 * (d["total"] - nat - d["fetches"] * c * 1e-6 - d["wb"]) / d["misses"]
        a = [per(cx, c) for c, _ in cs]
        b = [per(cy, c) for c, _ in cs]
        row("per-miss overhead at 2.2x, CXDVirt", f"{min(a):.2f}-{max(a):.2f} us", "1.1-2.6 us")
        row("per-miss overhead at 2.2x, Cylon", f"{min(b):.2f}-{max(b):.2f} us", "15.8-17.3 us")
        print("    fetch cost c: " + ", ".join(f"{n} {c:.3f} us" for c, n in cs)
              + (f"   [measured from {fc[1]}]" if fc else
                 "   [no mode-0 control: run_experiment.sh fig7a-ocean-mode0 adds the measured end]"))

    print("\nFig. 7(b) Redis")
    m = {w: (v["cxd"][0], v["cyl"][0], v["cxd"][1], v["cyl"][1]) for w, v in r.items()}
    row("CXDVirt vs remote DRAM at 0.35x (mean)", f"{100*(m['0.35'][0]/rnat-1):+.1f}%", "meets remote DRAM")
    row("Cylon vs remote DRAM at 0.35x (mean)", f"{m['0.35'][1]/rnat:.2f}x", "3.1x")
    row("Cylon mean above remote DRAM at 0.35x", f"{m['0.35'][1]-rnat:.2f} us", "5 us floor")
    row("Cylon/CXDVirt p99 at 1.1x and 2.2x",
        f"{m['1.1'][3]/m['1.1'][2]:.2f}x, {m['2.2'][3]/m['2.2'][2]:.2f}x", "1.5x, 1.35x")
    gc, gy = m["2.2"][0] / m["0.35"][0], m["2.2"][1] / m["0.35"][1]
    row("mean growth 0.35->2.2x, CXDVirt / Cylon", f"{gc:.2f}x / {gy:.2f}x", "4.38x / 2.31x")
    row("ratio of the two growths", f"{gc/gy:.2f}x", "1.9x")


if __name__ == "__main__":
    main()
