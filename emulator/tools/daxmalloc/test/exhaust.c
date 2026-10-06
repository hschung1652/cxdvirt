/*
 * exhaust.c — what happens when the arena fills up.
 *
 * The arena is fixed (ONLY_MSPACES + HAVE_MMAP=0), so exhaustion must return
 * NULL with ENOMEM rather than quietly growing onto the normal heap -- which
 * would put part of the benchmark's working set in DRAM and silently corrupt
 * the measurement.  Freeing must then make the space usable again.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHUNK (1u << 20)
#define MAXBLK 4096

int main(void)
{
	static void *blk[MAXBLK];
	int n = 0, fail = 0;
	void *big;

	errno = 0;
	while (n < MAXBLK) {
		void *p = malloc(CHUNK);

		if (!p)
			break;
		memset(p, 0xa5, CHUNK); /* fault it in; prove it is writable */
		blk[n++] = p;
	}

	printf("allocated %d x 1MiB before exhaustion (errno=%d %s)\n", n, errno,
	       errno ? strerror(errno) : "");

	if (n == MAXBLK) {
		printf("FAIL: never exhausted -- region larger than the test, "
		       "or the arena grew beyond its base\n");
		return 1;
	}
	if (errno != ENOMEM) {
		printf("FAIL: exhaustion did not set ENOMEM (got %d)\n", errno);
		fail = 1;
	}

	/* Free everything; a large request must now succeed, proving the
	 * freed chunks coalesced rather than fragmenting the arena away. */
	for (int i = 0; i < n; i++)
		free(blk[i]);

	big = malloc((size_t)(n / 2) * CHUNK);
	if (!big) {
		printf("FAIL: %d MiB request failed after freeing %d MiB "
		       "(no coalescing)\n", n / 2, n);
		fail = 1;
	} else {
		printf("ok: %d MiB allocated after free (coalescing works)\n",
		       n / 2);
		free(big);
	}

	printf("%s\n", fail ? "FAILED" : "PASSED");
	return fail;
}
