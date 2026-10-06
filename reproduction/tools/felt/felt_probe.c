// SPDX-License-Identifier: GPL-2.0-only
/*
 * felt_probe - measure the felt (application-visible) per-access latency on
 * /dev/dax0.0.
 *
 * This is the CXDVirt counterpart of what Cylon's refault breakdown measures.
 * Cylon must join guest TSC to host windows through kvmwin_*.csv because the
 * two live in different timebases; CXDVirt has no VM, and this machine's TSC is
 * constant_tsc/nonstop_tsc, so the fault handler and this program read the SAME
 * counter and the records join directly on it.
 *
 * Per access we record (page_idx, tsc_before, tsc_after).  Everything between
 * those two stamps is what the application actually waits for:
 *
 *     felt = kernel fault entry/exit + handler work + TLB + resume
 *
 * join_felt.py then matches each window against /proc/nvmev/optb4k to split
 * felt into the handler stages and the residual outside them.
 *
 * Access mode matters for what a miss costs.  A write miss is write-allocate:
 * the handler skips the NAND read entirely and charges tPROG later, at eviction.
 * Only a READ of a page whose nand_valid bit is set pays tR.  So replicating
 * Cylon's refault measurement needs mode `wr`: pass 0 writes (setting nand_valid
 * and dirtying), later passes read the pages back after they have been evicted.
 *
 * Threads matter for two of the stages.  `wait` (Cylon's primary-fill stall) and
 * VM_FAULT_RETRY refaults (multi-exit refault) only occur when two threads fault
 * the SAME page at once, so they are identically zero in a single-threaded run.
 * `share` makes every thread sweep the whole footprint in step to produce them;
 * the default `split` gives each thread its own disjoint range.
 *
 * warmup_passes leading passes run but are NOT recorded.  MIO does the same: it
 * writes the whole buffer in init_ptr_buf and runs one untimed chase before its
 * timed loop, so its felt distribution is over READS only.  Use `wr` with
 * warmup 1 to match that population.
 *
 * STRIDE controls whether hits are exercised at all.  At the default 4096 every
 * access lands on a new page and faults, so the run is ~100% miss.  A 64-byte
 * stride walks cache lines within a page like MIO's pointer chase: the first
 * access to a page faults and the next 63 hit in the resident page, which is
 * what produces the bimodal hit/miss distribution a CXL-SSD should show.
 *
 * Build: gcc -O2 -pthread -o felt_probe felt_probe.c
 * Run  : ./felt_probe <MB> <passes> <seq|rand> <out.csv> [w|r|wr]
 *                     [threads] [split|share] [warmup] [stride] [sample] [group] [chase]
 *        (prints tsc_per_ns on stdout - pass it to join_felt.py)
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <pthread.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <x86intrin.h>

#define DAX_DEV "/dev/dax0.0"
#define PAGE_SZ 4096UL
#define MAX_THREADS 64

struct acc {
	uint32_t page_idx;
	uint64_t t0, t1;
};

struct worker {
	pthread_t tid;
	uint32_t ktid;                /* gettid(): matches the record's `tid` */
	int id;
	unsigned long first, count;   /* this thread's slice of the footprint */
	struct acc *rec;
	size_t n, cap;
	unsigned long sink;
	unsigned long chase_head;     /* byte offset of the ring entry point */
};

/* shared across workers */
static volatile unsigned char *g_p;
static unsigned long g_pages;
static int g_passes, g_rnd, g_share, g_warm;
static unsigned long g_stride = PAGE_SZ;   /* bytes between accesses */
static unsigned long g_sample = 1;         /* record 1 in N accesses */
static unsigned long g_group = 1;          /* accesses per recorded sample */
static int g_chase = 0;                    /* dependent pointer chase */
static const char *g_mode;
static pthread_barrier_t g_bar;

/* cycles per nanosecond, measured against CLOCK_MONOTONIC */
static double calibrate(void)
{
	struct timespec a, b;
	uint32_t aux;
	uint64_t c0, c1;
	double ns;

	clock_gettime(CLOCK_MONOTONIC, &a);
	c0 = __rdtscp(&aux);
	usleep(200000);
	c1 = __rdtscp(&aux);
	clock_gettime(CLOCK_MONOTONIC, &b);
	ns = (b.tv_sec - a.tv_sec) * 1e9 + (b.tv_nsec - a.tv_nsec);
	return (double)(c1 - c0) / ns;
}

static unsigned long sysfs_ul(const char *path, unsigned long fb)
{
	unsigned long v;
	FILE *f = fopen(path, "r");

	if (!f)
		return fb;
	if (fscanf(f, "%lu", &v) != 1)
		v = fb;
	fclose(f);
	return v;
}

static void *run(void *arg)
{
	struct worker *w = arg;
	/* distinct stream per thread, but deterministic across runs */
	uint64_t rs = 88172645463325252ULL + 0x9e3779b97f4a7c15ULL * (w->id + 1);
	int i;

	w->ktid = (uint32_t)syscall(SYS_gettid);

	for (i = 0; i < g_passes; i++) {
		/* `wr`: pass 0 writes so nand_valid is set and the pages are
		 * dirty; later passes read them back, which is the access that
		 * actually pays tR once they have been evicted */
		int wr = !strcmp(g_mode, "w") ||
			 (!strcmp(g_mode, "wr") && i == 0);
		unsigned long k;

		/* keep the threads in the same pass, so `share` really does
		 * collide rather than drifting into disjoint regions */
		pthread_barrier_wait(&g_bar);

		if (g_chase) {
			unsigned long k2;
			volatile uint64_t *cur;

			if (i < g_warm) {
				/* Build the ring over this thread's range; each
				 * slot holds the address of the next, so the
				 * traversal is a dependent load chain.  Doubles
				 * as the write pass that sets nand_valid.
				 *
				 * chase=1 links k -> k+1.  The chain is then
				 * dependent but SEQUENTIAL, so the hardware
				 * prefetcher still streams it and hits land in
				 * L1 (~5 ns).  chase=2 walks a random
				 * permutation instead, which the prefetcher
				 * cannot follow, so a hit costs a real DRAM
				 * access.  MIO shuffles the same way under -R. */
				unsigned long *ord = malloc(w->count * sizeof(*ord));

				if (!ord) { fprintf(stderr, "shuffle malloc\n"); return NULL; }
				for (k2 = 0; k2 < w->count; k2++)
					ord[k2] = k2;
				if (g_chase >= 2) {          /* Fisher-Yates */
					for (k2 = w->count - 1; k2 > 0; k2--) {
						unsigned long j2;
						rs ^= rs << 13; rs ^= rs >> 7;
						rs ^= rs << 17;
						j2 = rs % (k2 + 1);
						ord[k2] ^= ord[j2];
						ord[j2] ^= ord[k2];
						ord[k2] ^= ord[j2];
					}
				}
				for (k2 = 0; k2 < w->count; k2++) {
					unsigned long c = (w->first + ord[k2]) * g_stride;
					unsigned long nx = (w->first +
						ord[(k2 + 1) % w->count]) * g_stride;
					*(volatile uint64_t *)(g_p + c) =
						(uint64_t)(uintptr_t)(g_p + nx);
				}
				w->chase_head = (w->first + ord[0]) * g_stride;
				free(ord);
				continue;
			}

			cur = (volatile uint64_t *)(g_p + w->chase_head);
			for (k2 = 0; k2 < w->count; k2 += g_group) {
				unsigned long g, n2 = g_group;
				uint32_t aux;
				uint64_t t0, t1;
				const volatile uint64_t *start = cur;

				if (k2 + n2 > w->count)
					n2 = w->count - k2;
				t0 = __rdtscp(&aux);
				for (g = 0; g < n2; g++)
					cur = (volatile uint64_t *)(uintptr_t)*cur;
				t1 = __rdtscp(&aux);

				w->sink += (unsigned long)(uintptr_t)cur;

				/* The chain must be followed unbroken, so
				 * sampling can only drop the RECORD, never the
				 * access itself. */
				if (g_sample > 1 && ((k2 / g_group) % g_sample))
					continue;
				if (w->n >= w->cap) {
					fprintf(stderr, "rec overflow n=%zu "
						"cap=%zu\n", w->n, w->cap);
					abort();
				}
				w->rec[w->n].page_idx = (uint32_t)
					(((const unsigned char *)start - g_p) / PAGE_SZ);
				/* Keep t0 ABSOLUTE: join_felt.py matches a handler
				 * record by tsc_in in [t0,t1], so a zero base can never
				 * contain a real TSC.  Scaling t1 keeps (t1-t0) the
				 * per-access mean for the CDF consumers; at GROUP=1
				 * (the join case) it is exact. */
				w->rec[w->n].t0 = t0;
				w->rec[w->n].t1 = t0 + (t1 - t0) / n2;
				w->n++;
			}
			continue;
		}

		for (k = 0; k < w->count; k += g_group) {
			unsigned long idx, off, g, n = g_group;
			uint32_t aux;
			uint64_t t0, t1;

			if (k + n > w->count)
				n = w->count - k;

			/* ONE bracket around the whole group, exactly as MIO's
			 * op_ptr_chase(ptr, interval) times `interval` dependent
			 * chase steps and reports result/interval */
			t0 = __rdtscp(&aux);
			for (g = 0; g < n; g++) {
				if (i == 0 || !g_rnd) {
					idx = w->first + k + g;
				} else {
					rs ^= rs << 13; rs ^= rs >> 7;
					rs ^= rs << 17;
					idx = w->first + rs % w->count;
				}
				off = idx * g_stride;
				if (wr)
					g_p[off] = (unsigned char)(idx + i);
				else
					w->sink += g_p[off];
			}
			t1 = __rdtscp(&aux);

			if (i < g_warm)
				continue;   /* warm-up pass: executed, not recorded */
			if (g_sample > 1 && ((k / g_group) % g_sample))
				continue;

			if (w->n >= w->cap) {
				fprintf(stderr, "rec overflow n=%zu cap=%zu\n",
					w->n, w->cap);
				abort();
			}
			/* store the group MEAN, so one row is one sample of the same
			 * statistic MIO records */
			w->rec[w->n].page_idx =
				(uint32_t)(((w->first + k) * g_stride) / PAGE_SZ);
			/* absolute t0 -- see the chase branch above */
			w->rec[w->n].t0 = t0;
			w->rec[w->n].t1 = t0 + (t1 - t0) / n;
			w->n++;
		}
	}
	return NULL;
}

int main(int argc, char **argv)
{
	size_t mb       = (argc > 1) ? strtoul(argv[1], NULL, 10) : 512;
	int    passes   = (argc > 2) ? atoi(argv[2]) : 2;
	int    rnd      = (argc > 3 && !strcmp(argv[3], "rand"));
	const char *out = (argc > 4) ? argv[4] : "felt.csv";
	const char *mode = (argc > 5) ? argv[5] : "wr";
	int    nthr     = (argc > 6) ? atoi(argv[6]) : 1;
	int    share    = (argc > 7 && !strcmp(argv[7], "share"));
	int    warm     = (argc > 8) ? atoi(argv[8]) : 0;
	unsigned long stride = (argc > 9) ? strtoul(argv[9], NULL, 10) : PAGE_SZ;
	unsigned long sample = (argc > 10) ? strtoul(argv[10], NULL, 10) : 1;
	unsigned long group  = (argc > 11) ? strtoul(argv[11], NULL, 10) : 1;
	int chase = (argc > 12) ? atoi(argv[12]) : 0;
	unsigned long align = sysfs_ul("/sys/bus/dax/devices/dax0.0/align", 2UL << 20);
	unsigned long devsz = sysfs_ul("/sys/bus/dax/devices/dax0.0/size", 0);
	size_t bytes = mb * (1UL << 20);
	struct worker w[MAX_THREADS];
	unsigned long pages, nacc, sink = 0;
	size_t total = 0, percap;
	double cyc_ns;
	int fd, t;
	FILE *f;

	if (strcmp(mode, "w") && strcmp(mode, "r") && strcmp(mode, "wr")) {
		fprintf(stderr, "mode must be w, r, or wr (got \"%s\")\n", mode);
		return 1;
	}
	if (nthr < 1 || nthr > MAX_THREADS) {
		fprintf(stderr, "threads must be 1..%d (got %d)\n", MAX_THREADS, nthr);
		return 1;
	}
	if (devsz && bytes > devsz)
		bytes = devsz;
	if (align && (bytes % align))
		bytes = (bytes / align) * align;   /* devdax mmap alignment */
	if (!bytes) {
		fprintf(stderr, "footprint rounds to zero\n");
		return 1;
	}
	if (!stride || (stride & (stride - 1))) {
		fprintf(stderr, "stride must be a power of two (got %lu)\n", stride);
		return 1;
	}
	if (!sample) sample = 1;
	/* Faults land on the first access of each page, i.e. every
	 * (PAGE_SZ/stride)-th access.  A modulo subsample that DIVIDES that
	 * period keeps every fault while dropping hits, inflating the miss
	 * fraction by exactly that factor.  Require the two to be coprime so
	 * the sampled positions cycle through all residues. */
	if (!group) group = 1;
	if (chase && stride < 8) {
		fprintf(stderr, "chase needs stride >= 8 (a pointer per slot)\n");
		return 1;
	}
	if (chase && warm < 1) {
		fprintf(stderr, "chase needs warmup >= 1 to build the ring\n");
		return 1;
	}
	/* only a SEQUENTIAL walk aligns faults with k; a shuffled ring or a
	 * random order scatters them, so the correlation cannot arise */
	if (sample > 1 && stride < PAGE_SZ && chase < 2 && !rnd) {
		unsigned long per_pg = PAGE_SZ / stride, a = per_pg, b = sample, t;
		while (b) { t = a % b; a = b; b = t; }
		if (a != 1) {
			fprintf(stderr,
				"sample=%lu shares a factor (%lu) with the %lu accesses "
				"per page: misses would be over-represented %lux.\n"
				"Use a sample coprime with %lu (e.g. %lu) or sample=1.\n",
				sample, a, per_pg, a, per_pg, per_pg - 1);
			return 1;
		}
	}
	pages = bytes / PAGE_SZ;
	nacc = bytes / stride;              /* accesses, not pages */
	if ((unsigned long)nthr > nacc)
		nthr = (int)nacc;

	fd = open(DAX_DEV, O_RDWR);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", DAX_DEV, strerror(errno));
		return 1;
	}
	g_p = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (g_p == MAP_FAILED) {
		fprintf(stderr, "mmap %zu MB: %s\n", bytes >> 20, strerror(errno));
		return 1;
	}

	g_pages = pages; g_passes = passes; g_rnd = rnd;
	g_mode = mode; g_share = share; g_warm = warm;
	g_stride = stride; g_sample = sample; g_group = group; g_chase = chase;
	pthread_barrier_init(&g_bar, NULL, nthr);

	for (t = 0; t < nthr; t++) {
		w[t].id = t;
		if (share) {
			/* every thread sweeps the whole footprint, so they
			 * collide on the same pages: this is what produces
			 * `wait` (primary-fill stall) and RETRY refaults */
			w[t].first = 0;
			w[t].count = nacc;
		} else {
			unsigned long chunk = nacc / nthr;
			w[t].first = (unsigned long)t * chunk;
			w[t].count = (t == nthr - 1) ? nacc - w[t].first : chunk;
		}
		percap = w[t].count * (passes - warm > 0 ? passes - warm : 1);
		percap = percap / (sample * group) + 2;
		w[t].rec = malloc(percap * sizeof(struct acc));
		if (!w[t].rec) {
			fprintf(stderr, "malloc %zu records: %s\n", percap,
				strerror(errno));
			return 1;
		}
		/* fault the record buffer in now: its own page faults must not
		 * land inside a measured DAX access window */
		memset(w[t].rec, 0, percap * sizeof(struct acc));
		w[t].cap = percap;
		w[t].n = 0;
		w[t].sink = 0;
		w[t].chase_head = 0;
	}

	cyc_ns = calibrate();
	fprintf(stderr,
		"footprint=%zu MB (%lu pages, %lu accesses @%lu B) passes=%d(warm %d) "
		"order=%s mode=%s threads=%d %s sample=1/%lu group=%lu chase=%d\n",
		bytes >> 20, pages, nacc, stride, passes, warm,
		rnd ? "random" : "sequential", mode, nthr,
		share ? "share" : "split", sample, group, chase);

	for (t = 0; t < nthr; t++) {
		if (pthread_create(&w[t].tid, NULL, run, &w[t])) {
			fprintf(stderr, "pthread_create %d: %s\n", t, strerror(errno));
			return 1;
		}
	}
	for (t = 0; t < nthr; t++)
		pthread_join(w[t].tid, NULL);

	f = fopen(out, "w");
	if (!f) {
		fprintf(stderr, "open %s: %s\n", out, strerror(errno));
		return 1;
	}
	fprintf(f, "tid,page_idx,tsc0,tsc1\n");
	for (t = 0; t < nthr; t++) {
		size_t k;
		for (k = 0; k < w[t].n; k++)
			fprintf(f, "%u,%u,%llu,%llu\n", w[t].ktid,
				w[t].rec[k].page_idx,
				(unsigned long long)w[t].rec[k].t0,
				(unsigned long long)w[t].rec[k].t1);
		total += w[t].n;
		sink += w[t].sink;
		free(w[t].rec);
	}
	fclose(f);
	pthread_barrier_destroy(&g_bar);

	fprintf(stderr, "wrote %s (%zu accesses, checksum %lu)\n", out, total, sink);
	printf("%.6f\n", cyc_ns);          /* tsc_per_ns, for join_felt.py */
	munmap((void *)g_p, bytes);
	close(fd);
	return 0;
}
