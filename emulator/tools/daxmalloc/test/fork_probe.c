/*
 * fork_probe.c — pins the fork policy.
 *
 * A forked child's copy of the devdax VMA is not in the module's mmap registry
 * (its vm_ops has no .open), so eviction shootdowns miss it and the child would
 * read pages at DRAM speed with no NAND latency charged.  The default policy is
 * therefore to abort the child; DAXMALLOC_FORK=warn downgrades it.
 *
 * Exit status: 0 if observed behaviour matches the mode, 1 otherwise.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void)
{
	const char *mode = getenv("DAXMALLOC_FORK");
	int want_abort = !(mode && (*mode == 'w' || *mode == 'W'));
	pid_t pid;
	int st = 0;
	void *p;

	p = malloc(4096); /* force init before forking */
	if (!p) {
		puts("FAIL: pre-fork malloc failed");
		return 1;
	}

	pid = fork();
	if (pid == 0) {
		/* Only reached in warn mode. */
		void *q = malloc(4096);

		_exit(q ? 42 : 43);
	}
	if (pid < 0) {
		puts("FAIL: fork failed");
		return 1;
	}
	waitpid(pid, &st, 0);

	if (want_abort) {
		int aborted = WIFSIGNALED(st) && WTERMSIG(st) == SIGABRT;

		printf("default mode: child %s (signalled=%d sig=%d)\n",
		       aborted ? "aborted as expected" : "did NOT abort",
		       WIFSIGNALED(st), WIFSIGNALED(st) ? WTERMSIG(st) : 0);
		if (!aborted)
			return 1;
	} else {
		int survived = WIFEXITED(st) && WEXITSTATUS(st) == 42;

		printf("warn mode: child %s (exited=%d status=%d)\n",
		       survived ? "survived as expected" : "did NOT survive",
		       WIFEXITED(st), WIFEXITED(st) ? WEXITSTATUS(st) : -1);
		if (!survived)
			return 1;
	}

	/* The parent must be unharmed either way. */
	free(p);
	p = malloc(1 << 20);
	if (!p) {
		puts("FAIL: parent allocation broken after fork");
		return 1;
	}
	free(p);
	puts("PASSED");
	return 0;
}
