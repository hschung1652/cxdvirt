#!/usr/bin/env python3
"""extract_fig7.py -- derive every number in Fig. 7 from run artifacts.

Works on two kinds of input, and the distinction matters:

  PAPER MODE (default).  Reads the dataset shipped in plots/data/ and must
  reproduce the published figure exactly.  The plotting scripts hold the
  published values as a tripwire and abort if a parse disagrees with them.

  CUSTOM MODE.  Reads a dataset someone else collected with the drivers in
  drivers/, whose filenames carry their own timestamps and policy tags.  The
  tripwire is disabled -- the whole point is that the numbers are different --
  and the figure is drawn from whatever was measured.

    FIG7_DATA=/path/to/my/data FIG7_MODE=custom python3 extract_fig7.py

WHY MODES RATHER THAN A CLEVERER GLOB.  Both halves of the shipped dataset are
ambiguous under a generic pattern, and silently picking wrong would be worse
than failing.  plots/data/redis holds two legs at the same cache size (an 08-24
run and the 08-25 one the figure uses), and three OCEAN sync variants sit side
by side (s4, s4atomic, s4mutex).  Paper mode names exactly what it wants;
custom mode takes the newest match per point and prints what it chose.

Rep layout differs between the two and both are handled: the paper's two OCEAN
reps were collected in separate sweeps and so live in two directories, while a
single `run_experiment.sh fig7a-ocean` writes r1 and r2 into one.  Reps are
gathered across every configured directory, so either shape gives the same
answer.

WHAT EACH NUMBER COMES FROM
  OCEAN   the patched SPLASH reporting (see bench-patches/): "Initialization
          time" and "Total time without initialization", both in seconds at
          nanosecond resolution.  Stock SPLASH prints whole seconds and no
          init/solve split, so this figure cannot be built without that patch.
  Redis   the SERVER's own accounting, never the YCSB client: `INFO
          commandstats` usec_per_call for the mean and `INFO latencystats` p99,
          both for hgetall, run phase only.  Throughput is the one number taken
          from the client, and it is not comparable across boots -- see
          redis_perf.py.
"""
import glob
import os
import re
import sys

ROOT = os.environ.get("CXDVIRT_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = (os.environ.get("FIG7_DATA") or os.environ.get("CXDVIRT_DATA")
        or os.path.join(ROOT, "plots", "data"))
MODE = os.environ.get("FIG7_MODE", "paper").lower()
CUSTOM = MODE == "custom" or bool(os.environ.get("FIG7_DATA"))

# Normalized WSS -> the cache size in MB that selects it, against a fixed
# footprint.  OCEAN's -n must be a power of two plus 2, so its footprint
# quantises 4x per step and the cache is what varies instead.
OCEAN_CACHES = {"0.35": 40578, "1.1": 12912, "2.2": 6456}
# Redis fixes the cache at 4914 MB (matching Cylon's bufsz = ssd_size/20) and
# varies the record count instead.
REDIS_RECORDS = {"0.35": 1000000, "1.1": 3000000, "2.2": 6000000}
REDIS_CACHE = int(os.environ.get("FIG7_REDIS_CACHE", 4914))
REDIS_THREADS = int(os.environ.get("FIG7_REDIS_THREADS", 1))
OCEAN_P = int(os.environ.get("FIG7_OCEAN_P", 8))
OCEAN_MODE = int(os.environ.get("FIG7_OCEAN_MODE", 2))

# Which directories hold the CXDVirt reps.  Paper mode names the two sweeps the
# figure was built from; custom mode defaults to the single directory
# run_experiment.sh writes into.
_dirs = os.environ.get("FIG7_OCEAN_DIRS")
OCEAN_DIRS = (_dirs.split(":") if _dirs
              else ["ocean_wss_s4"] if CUSTOM
              else ["ocean_wss_s4atomic", "ocean_wss_s4"])
# Filename prefix that identifies the Redis leg.  "*" in custom mode.
REDIS_TAG = os.environ.get("FIG7_REDIS_TAG", "*" if CUSTOM else "clockswclk_d0")
OCEAN_CYLON_DIR = os.environ.get("FIG7_OCEAN_CYLON_DIR", "ocean_wss_cylon_s4")
# Kernel prefix on the native OCEAN filenames.  NOT optional and NOT a glob:
# plots/data/ocean_native holds runs from BOTH kernels (k646 = Cylon's host,
# k6185 = the one nvmev.ko needs), and native OCEAN differs about 4% between
# them, almost entirely in the page-fault path.  The figure draws 6.4.6 so that
# both its panels quote one kernel; averaging across the two would silently mix
# them and move the dashed line.
NATIVE_KERNEL = os.environ.get("FIG7_NATIVE_KERNEL", "k646")
# The HEAP arm -- the one both dashed lines draw -- is measured on CXDVirt's own
# kernel, because it is the placement-matched twin of the CXDVirt runs.
HEAP_KERNEL = os.environ.get("FIG7_HEAP_KERNEL", "k6185")
# Where the Cylon Redis leg lives.  The shipped reference is leg 3 of our runs;
# drivers/cylon/run_redis_cylon.sh writes a re-collection to redis_cylon/.
REDIS_CYLON_DIR = os.environ.get("FIG7_REDIS_CYLON_DIR",
                                 "redis_cylon" if CUSTOM else "redis/cylon_leg3")

_STAMP = re.compile(r"(\d{4}_\d{6})")
_notes = []


def _newest(hits):
    """Latest MMDD_HHMMSS stamp in the name, falling back to mtime."""
    def key(p):
        m = _STAMP.findall(os.path.basename(p))
        return (m[-1] if m else "", os.path.getmtime(p))
    return sorted(hits, key=key)[-1]


def _pick(pattern, what):
    """One file, chosen explicitly.  Ambiguity is reported, never hidden."""
    hits = sorted(glob.glob(pattern))
    if not hits:
        raise SystemExit(
            f"no {what} found.\n  looked for: {pattern}\n"
            f"  If this is your own data, set FIG7_DATA=<dir> (and see the\n"
            f"  FIG7_* overrides at the top of extract_fig7.py).")
    if len(hits) > 1:
        chosen = _newest(hits)
        _notes.append(f"{what}: {len(hits)} matches, using {os.path.basename(chosen)}")
        return chosen
    return hits[0]


def _all(patterns, what):
    hits = sorted(h for p in patterns for h in glob.glob(p))
    if not hits:
        raise SystemExit(f"no {what} found.\n  looked for: {'  '.join(patterns)}")
    return hits


# ---------------------------------------------------------------- OCEAN -----
def ocean_run(path):
    """-> (init_s, solve_s) from a patched SPLASH OCEAN run."""
    txt = open(path, errors="replace").read()
    init = re.search(r"^Initialization time\s*:\s*([0-9.]+)", txt, re.M)
    solve = re.search(r"^Total time without initialization\s*:\s*([0-9.]+)", txt, re.M)
    if not (init and solve):
        raise SystemExit(f"{path}: no init/solve lines -- was SPLASH patched? "
                         "see bench-patches/README.md")
    return float(init.group(1)), float(solve.group(1))


def ocean():
    """-> {wss: dict(cxd=(init,solve), cyl=(init,solve), reps=[(init,solve)...])}

    Reps are gathered across every directory in OCEAN_DIRS, so two sweeps of one
    rep and one sweep of two reps both work.  Cylon is n=1 at every point in the
    shipped data; that is a property of the data, not of this parse.
    """
    out = {}
    for wss, cache in OCEAN_CACHES.items():
        pats = [f"{DATA}/{d}/*_m{OCEAN_MODE}_c{cache}_p{OCEAN_P}_r*.run"
                for d in OCEAN_DIRS]
        reps = [ocean_run(p) for p in _all(pats, f"OCEAN {wss}x runs")]
        cyl = ocean_run(_pick(
            f"{DATA}/{OCEAN_CYLON_DIR}/cylon_n*_p{OCEAN_P}_c{cache}.run",
            f"Cylon OCEAN {wss}x"))
        # Rounded to 6 dp, the precision the published literals carry.
        mean = tuple(round(sum(r[i] for r in reps) / len(reps), 6) for i in (0, 1))
        out[wss] = dict(cxd=mean, cyl=cyl, reps=reps)
    return out


def ocean_native():
    """-> {'heap': (init, solve), 'remote': ..., 'local': ...}

    HEAP is the arm the dashed line draws.  It is launched exactly as the
    CXDVirt runs are -- node-0 CPUs, --membind 0, daxmalloc preloaded -- with
    /dev/dax0.0 swapped for a /dev/shm file pre-faulted on node 1, on CXDVirt's
    kernel.  The heap sits on node 1 in the same dlmalloc arena, everything else
    on node 0: only the device is missing.

    REMOTE and LOCAL are the earlier `--membind` arms on 6.4.6, kept for
    context and optional.  --membind 1 also put the binary, stack and libc on
    node 1 and the heap in glibc malloc, which made remote 4.7% slower than the
    matched arm and hid CXDVirt's fault-path cost (+0.08% against it, +4.8%
    against heap).
    """
    out = {}
    for arm, kern in (("heap", HEAP_KERNEL), ("remote", NATIVE_KERNEL),
                      ("local", NATIVE_KERNEL)):
        pat = f"{DATA}/ocean_native/{kern}_s4_{arm}_r*.out"
        if arm != "heap" and not glob.glob(pat):
            continue
        hits = _all([pat], f"native OCEAN {arm} on {kern}")
        runs = [ocean_run(h) for h in hits]
        out[arm] = tuple(round(sum(r[i] for r in runs) / len(runs), 6) for i in (0, 1))
    return out


# ---------------------------------------------------------------- Redis -----
def _usec_per_call(path, cmd="hgetall"):
    m = re.findall(rf"cmdstat_{cmd}:.*?usec_per_call=([0-9.]+)",
                   open(path, errors="replace").read())
    if len(m) != 1:
        raise SystemExit(f"{path}: expected 1 cmdstat_{cmd}, found {len(m)}")
    return float(m[0])


def _pctile(path, p, cmd="hgetall"):
    m = re.search(rf"latency_percentiles_usec_{cmd}:(\S+)",
                  open(path, errors="replace").read())
    if not m:
        raise SystemExit(f"{path}: no {cmd} percentiles. Is latency-tracking on "
                         "in redis.conf?")
    d = dict(kv.split("=") for kv in m.group(1).split(","))
    return float(d[f"p{p}"])


def _throughput(path):
    m = re.search(r"^\[OVERALL\], Throughput\(ops/sec\), ([0-9.]+)",
                  open(path, errors="replace").read(), re.M)
    if not m:
        raise SystemExit(f"{path}: no OVERALL throughput line")
    return float(m.group(1))


def redis():
    """-> {wss: dict(cxd=(mean, p99, thpt), cyl=(mean, p99, thpt))}"""
    out = {}
    for wss, rec in REDIS_RECORDS.items():
        cx = _pick(f"{DATA}/redis/{REDIS_TAG}*_c{REDIS_CACHE}_{rec}_"
                   f"{REDIS_THREADS}t_*.commandstats", f"CXDVirt Redis {wss}x")
        stem = cx[: -len(".commandstats")]
        cyc = _pick(f"{DATA}/{REDIS_CYLON_DIR}/*commandstats*{rec}*",
                    f"Cylon Redis {wss}x")
        cyl_stem = lambda kind, ext: _pick(
            f"{DATA}/{REDIS_CYLON_DIR}/*{kind}*{rec}*.{ext}",
            f"Cylon Redis {wss}x {kind}")
        out[wss] = dict(
            cxd=(_usec_per_call(cx), _pctile(stem + ".latencystats", 99),
                 round(_throughput(stem + ".run.out"))),
            cyl=(_usec_per_call(cyc), _pctile(cyl_stem("latencystats", "txt"), 99),
                 round(_throughput(cyl_stem("run", "out")))),
        )
    return out


def redis_native():
    """-> {'heap': {wss: [mean, ...]}, 'remote': {...}, 'local': {...}}, one
    entry per rep.

    HEAP draws the line (see ocean_native): n=3 at 0.35x, the only point the
    line is drawn from, on CXDVirt's kernel.  REMOTE/LOCAL are the earlier
    --membind arms on 6.4.6 (n=2 at 0.35x remote, n=1 elsewhere), kept for
    context and for the flatness of native across the sweep.  These stay lists
    rather than being averaged here.
    """
    out = {}
    for arm in ("heap", "remote", "local"):
        out[arm] = {}
        for wss, rec in REDIS_RECORDS.items():
            fs = sorted(glob.glob(
                f"{DATA}/redis_native/native_{arm}_*_{rec}_"
                f"{REDIS_THREADS}t_*.commandstats"))
            out[arm][wss] = [_usec_per_call(f) for f in fs]
    return out


# ------------------------------------------------------------------ main ----
def main():
    o, on, r, rn = ocean(), ocean_native(), redis(), redis_native()
    print(f"dataset: {DATA}   mode: {'custom' if CUSTOM else 'paper'}")
    for n in _notes:
        print(f"  note: {n}")

    print("\nOCEAN  (Splash-4 -p%d, seconds)" % OCEAN_P)
    print(f"  {'WSS':<6}{'CXDVirt init':>14}{'solve':>11}"
          f"{'Cylon init':>13}{'solve':>11}{'ratio':>8}{'reps':>6}")
    for wss in OCEAN_CACHES:
        c, y = o[wss]["cxd"], o[wss]["cyl"]
        print(f"  {wss:<6}{c[0]:14.6f}{c[1]:11.6f}{y[0]:13.6f}{y[1]:11.6f}"
              f"{sum(y)/sum(c):7.2f}x{len(o[wss]['reps']):6d}")
    for arm, v in on.items():
        print(f"  native {arm:<7}{v[0]:8.6f} + {v[1]:.6f} = {sum(v):.6f}")

    print("\nRedis  (YCSB-C, %d thread, server-side, us)" % REDIS_THREADS)
    print(f"  {'WSS':<6}{'CXD mean':>10}{'p99':>8}{'ops/s':>9}"
          f"{'Cyl mean':>11}{'p99':>8}{'ops/s':>9}")
    for wss in REDIS_RECORDS:
        c, y = r[wss]["cxd"], r[wss]["cyl"]
        print(f"  {wss:<6}{c[0]:10.2f}{c[1]:8.3f}{c[2]:9d}"
              f"{y[0]:11.2f}{y[1]:8.3f}{y[2]:9d}")
    for arm, v in rn.items():
        pretty = "  ".join(f"{w}:{'/'.join(f'{x:.2f}' for x in m)}"
                           for w, m in v.items() if m)
        print(f"  native {arm:<7}{pretty}")


if __name__ == "__main__":
    sys.exit(main())
