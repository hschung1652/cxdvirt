#!/usr/bin/env python3
"""turns_cylon.py — how Cylon's single miss server (QEMU's global lock) is shared
between guest CPUs, from Cylon's per-request records (<cell>.optb.csv).

  turns_cylon.py <cell>.optb.csv [more.csv ...]

Each record is one MMIO miss: vcpu, t_mmio_in (entry to Cylon's handler, taken
under the lock), and its stage times.  Sorted by t_mmio_in, the vcpu column is the
order in which CPUs got the lock.  Reported per file:
  - in-flight check (at most one request inside the device callback),
  - streaks: consecutive turns won by the same vCPU (fair round-robin -> ~1),
  - gap from the end of one callback to the start of the next, split by whether
    the SAME vCPU went again or a DIFFERENT one took over,
  - how the streak length and the number of active vCPUs evolve over the run.
"""
import subprocess
import sys

import numpy as np


def load(path):
    # vcpu, t_mmio_in, callback duration (total + modeled); awk keeps numpy's input small
    out = subprocess.run(["awk", "-F,", "NR>1{print $1, $2, $8+$9}", path],
                         capture_output=True, text=True, check=True).stdout
    a = np.fromstring(out, sep=" ").reshape(-1, 3)
    a = a[np.argsort(a[:, 1], kind="stable")]
    return a[:, 0].astype(int), a[:, 1], a[:, 2]


def streaks(v):
    change = np.flatnonzero(np.diff(v) != 0)
    bounds = np.concatenate(([0], change + 1, [len(v)]))
    return np.diff(bounds)


for path in sys.argv[1:]:
    v, t0, d = load(path)
    t1 = t0 + d
    n = len(v)
    overlap = np.sum(t0[1:] < t1[:-1])
    L = streaks(v)
    gap = (t0[1:] - t1[:-1]) / 1000.0
    same = v[1:] == v[:-1]
    span = (t1.max() - t0.min()) / 1e9
    print(f"\n{path.split('/')[-2]}/{path.split('/')[-1]}: {n:,} requests over {span:.1f} s, "
          f"{len(np.unique(v))} vCPUs; overlapping callbacks: {overlap}")
    print(f"  streak length (turns in a row by one vCPU): p50 {np.median(L):.0f}  mean {L.mean():.2f}  "
          f"p90 {np.percentile(L, 90):.0f}  p99 {np.percentile(L, 99):.0f}  max {L.max()}")
    print(f"  handovers to the SAME vCPU: {100 * same.mean():.1f}% of turns")
    for lab, g in (("same vCPU again", gap[same]), ("different vCPU", gap[~same])):
        if len(g):
            print(f"  gap before next turn, {lab:16}: p10 {np.percentile(g, 10):6.2f}  p50 {np.median(g):6.2f}  "
                  f"p90 {np.percentile(g, 90):7.2f} us")
    # evolution: 10 equal slices of wall time
    edges = np.linspace(t0.min(), t1.max(), 11)
    print("  by tenth of the run:  turns   active vCPUs   same-vCPU share   mean streak")
    for i in range(10):
        m = (t0 >= edges[i]) & (t0 < edges[i + 1])
        if m.sum() < 2:
            print(f"    {i}: {m.sum():8,}")
            continue
        vv = v[m]
        s = np.mean(vv[1:] == vv[:-1])
        print(f"    {i}: {m.sum():8,}   {len(np.unique(vv)):>6}        {100 * s:10.1f}%      {streaks(vv).mean():8.2f}")
