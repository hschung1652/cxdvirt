# results/

Output of `scripts/run_experiment.sh`, and the default input of
`scripts/make_figures.sh`.  Empty as distributed.  The directory layout matches
that of the reference data in `plots/data/`, which holds the subset for Figs. 5,
7 and 8.

| Directory | Target | Figure |
|---|---|---|
| `cylon/` | `fig1-cylon-breakdown` | 1, 6 |
| `fig5_4914/cxdvirt/c4914_d64/` | `fig5-mio` | 5 |
| `fig5_4914/cylon/` | `fig5-mio-cylon` | 5 |
| `nvmev/` | `fig6-felt-cxdvirt` | 6 |
| `ocean_wss_s4/` | `fig7a-ocean`, `fig7a-ocean-mode0` | 7(a) |
| `ocean_wss_cylon_s4/` | `fig7a-ocean-cylon` | 7(a) |
| `redis/` | `fig7b-redis` | 7(b) |
| `redis_cylon/` | `fig7b-redis-cylon` | 7(b) |
| `ocean_native/`, `redis_native/` | `fig7-native` | 7 |
| `redis/fig8_4914/policy/`, `redis/fig8_4914/prefetch/` | `fig8a-policy`, `fig8bcd-prefetch` | 8 |

Where a point has been run more than once, the most recent run is used.
`SEEDED_FROM_REFERENCE.txt`, when present, lists files copied from
`plots/data/` by `scripts/seed_results.sh`.
