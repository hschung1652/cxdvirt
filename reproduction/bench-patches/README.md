# Benchmark patches

## `splash4-ns-clock.patch`

Required for Fig. 7(a).  Applied to Splash-4 by `scripts/fetch.sh`.

- Replaces SPLASH's `CLOCK` macro (`gettimeofday`, whole-second output) with
  `clock_gettime(CLOCK_MONOTONIC)` at nanosecond resolution.
- Makes OCEAN report initialization time and solve time separately, which the
  stacked bars of Fig. 7(a) plot.  The solve time excludes the first timestep;
  five timesteps are timed.
