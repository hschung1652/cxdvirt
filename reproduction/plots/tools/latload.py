"""latload -- read a run's per-operation READ latencies.

WHY THIS EXISTS.  Four figures need the full per-op latency sample: they draw
CDFs, and a CDF cannot be rebuilt from percentiles.  The sample lives in YCSB's
`measurementtype=raw` output, which is ~21 MB per million operations and 4.8 GB
across the runs behind Fig. 8 -- too large to ship, and 99.9% of it is the two
columns no figure reads (the op name and the millisecond timestamp).

`precompute_latencies.py` distils each `.raw` into a `.lat.gz`: the same READ
latencies, sorted, as int32.  Sorting is not a loss here, because every consumer
sorts anyway or takes an order-independent statistic (mean, count).  Sorted
int32 gzips about 780x -- 3.8 MB becomes 5 KB -- so the whole Fig. 8 sample set
ships in about 225 KB instead of 4.8 GB.

Callers get whichever exists, preferring the compact form, so a tree with the
raw files still present behaves identically.
"""
import gzip
import io
import os

import numpy as np


def read_latencies(base, dtype=np.int64):
    """-> sorted READ latencies [us] for run `base` (a path without extension).

    Prefers <base>.lat.gz, falls back to <base>.raw.  Raises OSError if neither
    exists, matching what a bare open() would have done.
    """
    lat_gz = base + ".lat.gz"
    if os.path.exists(lat_gz):
        with gzip.open(lat_gz, "rb") as fh:
            v = np.load(io.BytesIO(fh.read()))
        return v.astype(dtype, copy=False)

    # YCSB emits one header per measurement type, so a CLEANUP block with its
    # own header follows the READ rows -- filter on the op name rather than
    # skipping a fixed number of lines.
    with open(base + ".raw") as fh:
        v = np.fromiter((int(l.rsplit(",", 1)[1])
                         for l in fh if l.startswith("READ,")), dtype=np.int64)
    v.sort()
    return v.astype(dtype, copy=False)
