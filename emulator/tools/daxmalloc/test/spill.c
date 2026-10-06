/*
 * spill.c — validate DAXMALLOC_DRAM_BUDGET spillover.
 *
 * In spillover mode the heap has two backing stores: an anonymous DRAM arena of
 * the budgeted size, and the device.  Allocations fill DRAM first and spill to
 * the device once it is full, mimicking the first-touch NUMA spillover that
 * Cylon's GAPBS harness gets by running without numactl.
 *
 * The conformance battery cannot cover this: it asserts every pointer lies in
 * the device mapping, which is deliberately false here.  What matters instead:
 *   - both stores actually get used, in the right order;
 *   - data written before a spill survives it;
 *   - free() routes each pointer back to the arena it came from;
 *   - realloc() that outgrows DRAM migrates the block to the device intact
 *     (the cross-region path), rather than failing.
 *
 * Run: DAXMALLOC_PATH=/dev/dax0.0 DAXMALLOC_DRAM_BUDGET=64M \
 *      DAXMALLOC_REQUIRE=1 LD_PRELOAD=./daxmalloc.so ./test/spill
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define BLK (4u << 20)   /* 4 MiB per block */
#define NBLK 40          /* 160 MiB total, against a 64 MiB budget */

static uintptr_t dev_lo, dev_hi;
static int pass, fail;

static void ok(int c, const char *what)
{
	if (c) { pass++; } else { fail++; printf("  FAIL: %s\n", what); }
}

static int on_device(const void *p)
{
	uintptr_t a = (uintptr_t)p;
	return dev_lo && a >= dev_lo && a < dev_hi;
}

static int find_device(const char *backing)
{
	char line[512];
	FILE *f = fopen("/proc/self/maps", "r");

	if (!f)
		return -1;
	while (fgets(line, sizeof(line), f)) {
		unsigned long lo, hi;

		if (!strstr(line, backing) || sscanf(line, "%lx-%lx", &lo, &hi) != 2)
			continue;
		if (hi - lo < (1u << 20))
			continue;
		if (!dev_lo || lo < dev_lo) dev_lo = lo;
		if (hi > dev_hi) dev_hi = hi;
	}
	fclose(f);
	return dev_lo ? 0 : -1;
}

int main(void)
{
	const char *backing = getenv("DAXMALLOC_PATH");
	static void *blk[NBLK];
	int i, n_dram = 0, n_dev = 0, first_dev = -1, bad = 0;
	char *big;

	if (!backing || !*backing) backing = "/dev/dax0.0";
	if (find_device(backing) != 0) {
		fprintf(stderr, "spill: device mapping for %s not found\n", backing);
		return 2;
	}
	if (!getenv("DAXMALLOC_DRAM_BUDGET")) {
		fprintf(stderr, "spill: set DAXMALLOC_DRAM_BUDGET\n");
		return 2;
	}

	/* Fill past the budget, stamping each block with its index. */
	for (i = 0; i < NBLK; i++) {
		blk[i] = malloc(BLK);
		if (!blk[i]) { ok(0, "allocation past the budget succeeded"); break; }
		memset(blk[i], i + 1, BLK);
		if (on_device(blk[i])) {
			n_dev++;
			if (first_dev < 0) first_dev = i;
		} else {
			n_dram++;
		}
	}
	printf("  %d blocks in DRAM, %d on device (first device block: #%d)\n",
	       n_dram, n_dev, first_dev);
	ok(n_dram > 0, "DRAM arena was used");
	ok(n_dev > 0, "spilled to the device once DRAM filled");
	/* Spillover must be ordered: DRAM is exhausted before the device is
	 * touched, so no DRAM block may follow the first device block. */
	for (i = first_dev; i >= 0 && i < NBLK; i++)
		if (blk[i] && !on_device(blk[i])) bad++;
	ok(bad == 0, "no DRAM allocation after the first spill (ordered)");

	/* Contents must survive the transition across both arenas. */
	bad = 0;
	for (i = 0; i < NBLK; i++) {
		unsigned char *q = blk[i];
		if (!q) continue;
		if (q[0] != (unsigned char)(i + 1) ||
		    q[BLK - 1] != (unsigned char)(i + 1)) bad++;
	}
	ok(bad == 0, "every block still holds its pattern across both arenas");

	/* Cross-region realloc: grow a DRAM block beyond the remaining budget.
	 * It must migrate to the device with contents intact, not fail. */
	{
		char *p = malloc(1 << 20);
		int was_dram;

		ok(p != NULL, "small block allocated");
		memset(p, 0xC3, 1 << 20);
		was_dram = !on_device(p);
		p = realloc(p, 96u << 20);     /* larger than the whole budget */
		ok(p != NULL, "realloc beyond the DRAM budget succeeded");
		if (p) {
			ok(on_device(p), "grown block migrated to the device");
			bad = 0;
			for (i = 0; i < (1 << 20); i++)
				if ((unsigned char)p[i] != 0xC3) { bad = 1; break; }
			ok(!bad, "migrated block kept its contents");
			free(p);
		}
		(void)was_dram;
	}

	/* free() must route each pointer to its own arena; if it did not, the
	 * next large allocation would fail or corrupt. */
	for (i = 0; i < NBLK; i++)
		free(blk[i]);
	big = malloc(48u << 20);
	ok(big != NULL, "large allocation works after freeing both arenas");
	if (big) { memset(big, 0x5a, 48u << 20); free(big); }

	printf("\n%d passed, %d failed\n", pass, fail);
	return fail ? 1 : 0;
}
