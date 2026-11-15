#pragma once
#include <string>
#include <fstream>
#include <bpf/bpf.h>
#include <fcntl.h>
#include <unistd.h>

static inline std::string cg_path_from_pid(pid_t pid) {
    std::ifstream in("/proc/" + std::to_string(pid) + "/cgroup");
    std::string line;
    while (std::getline(in, line)) {
        if (line.rfind("0::", 0) == 0) {
            std::string suffix = line.substr(3);
            if (!suffix.empty() && suffix[0] == '/') {
                return std::string("/sys/fs/cgroup") + suffix;
            }
        }
    }
    return {};
}

static inline bool set_target_cgroup_map(int map_fd, pid_t pid) {
    if (map_fd < 0) return false;
    std::string cg = cg_path_from_pid(pid);
    if (cg.empty()) return false;
    int cfd = open(cg.c_str(), O_DIRECTORY | O_RDONLY | O_CLOEXEC);
    if (cfd < 0) return false;
    __u32 idx = 0;
    int rc = bpf_map_update_elem(map_fd, &idx, &cfd, BPF_ANY);
    close(cfd);
    return rc == 0;
}

static inline void setup_cgroup_filter(int map_fd, pid_t pid) {
    if (set_target_cgroup_map(map_fd, pid)) {
        fprintf(stderr, "[sampler] cgroup filter set from PID %d\n", (int)pid);
    } else {
        fprintf(stderr, "[sampler] WARN: fail to set cgroup filter, fallback to PID-only\n");
    }
}

