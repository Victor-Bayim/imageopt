#pragma once
#include <stdint.h>

#define EVT_OPEN 1
#define EVT_MMAP 2

struct file_event {
    uint64_t ts_ns;
    uint32_t pid;
    uint32_t tgid;
    uint32_t type;
    uint32_t flags_or_prot;
    int32_t dirfd;
    uint64_t dev;
    uint64_t inode;
    uint64_t cgroup_id;
    char path[256];
};
