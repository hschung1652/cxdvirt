#!/usr/bin/env python3
"""precompute_latencies.py -- distil per-operation latency logs into .lat.gz.

    ./scripts/precompute_latencies.py plots/data/redis [...]
    ./scripts/precompute_latencies.py --mio plots/data/fig5_4914

For every <base>.raw under the given directories, writes <base>.lat.gz holding
the sorted READ latencies as int32.  With --mio it does the same for MIO's
per-access output, <cell>.txt (one latency in ns per line, cells named
{1,8}thr_{seq,rnd}), which is what Fig. 5 plots.  Idempotent; skips a run whose
.lat.gz is newer than its source.

This is what makes Fig. 8 shippable.  The figures read the full per-op sample
because they draw CDFs, but they only ever read the latency column and they
sort it; the .raw carries an op name and a millisecond timestamp per row as
well.  Dropping those and sorting turns 4.8 GB into about 225 KB, losslessly
with respect to every statistic any figure computes.

The figures read either form, so this is optional: it shrinks a results tree
before it is archived.
"""
import gzip
import io
import os
import re
import sys

import numpy as np


def distil(raw):
    out = raw[:-4] + ".lat.gz"
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(raw):
        return None
    with open(raw) as fh:
        v = np.fromiter((int(l.rsplit(",", 1)[1])
                         for l in fh if l.startswith("READ,")), dtype=np.int64)
    if not v.size:
        return None
    v.sort()
    buf = io.BytesIO()
    np.save(buf, v.astype(np.int32))
    with gzip.open(out, "wb", compresslevel=6) as fh:
        fh.write(buf.getvalue())
    return out, v.size, os.path.getsize(raw), os.path.getsize(out)


MIO_CELL = re.compile(r"^[0-9]+thr_(seq|rnd)\.txt$")


def distil_mio(txt):
    """Same format for a MIO cell: every value is an integer latency in ns, the
    largest in the Fig. 5 cells 43.4 ms, so int32 holds them exactly; the plot
    only takes quantiles, so sorting loses nothing either."""
    out = txt[:-4] + ".lat.gz"
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(txt):
        return None
    v = np.array(open(txt).read().split(), dtype=np.int64)
    if not v.size:
        return None
    if v.min() < 0 or v.max() > np.iinfo(np.int32).max:
        raise ValueError(f"{txt}: values outside int32")
    v.sort()
    buf = io.BytesIO()
    np.save(buf, v.astype(np.int32))
    with gzip.open(out, "wb", compresslevel=6) as fh:
        fh.write(buf.getvalue())
    return out, v.size, os.path.getsize(txt), os.path.getsize(out)


def main(dirs, mio=False):
    n = src = dst = 0
    for d in dirs:
        for root, _, files in os.walk(d):
            for f in sorted(files):
                if mio:
                    if not MIO_CELL.match(f):
                        continue
                elif not f.endswith(".raw"):
                    continue
                try:
                    r = (distil_mio if mio else distil)(os.path.join(root, f))
                except OSError as e:
                    # superseded_* subdirectories are root-owned and read-only;
                    # no figure reads them, so a failure there is not fatal.
                    print(f"  SKIP {f}: {e}", file=sys.stderr)
                    continue
                if r:
                    _, ops, sz_in, sz_out = r
                    n += 1; src += sz_in; dst += sz_out
                    print(f"  {f}  {ops} ops  {sz_in>>10} KiB -> {sz_out>>10} KiB")
    if n:
        print(f"\n{n} runs distilled: {src>>20} MiB -> {dst>>10} KiB "
              f"({src/max(dst,1):.0f}x)")
    else:
        print("nothing to do")


if __name__ == "__main__":
    args = sys.argv[1:]
    mio = "--mio" in args
    args = [a for a in args if a != "--mio"]
    main(args or ["plots/data"], mio)
