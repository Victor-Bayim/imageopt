#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    const char *release_file;

    if (argc < 3) {
        fprintf(stderr, "usage: %s <release-file> <program> [args...]\n", argv[0]);
        return 64;
    }

    release_file = argv[1];
    while (access(release_file, F_OK) != 0) {
        if (errno != ENOENT) {
            fprintf(stderr, "gate wait failed for %s: %s\n", release_file, strerror(errno));
            return 70;
        }
        usleep(50000);
    }

    execvp(argv[2], &argv[2]);
    fprintf(stderr, "execvp(%s) failed: %s\n", argv[2], strerror(errno));
    return 127;
}
