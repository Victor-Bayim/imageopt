// SPDX-License-Identifier: GPL-2.0
// File access eBPF tracer for container startup
#include <vmlinux.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

#define DEDUP_NS 1000000000ULL
#define EVT_MMAP 2

char LICENSE[] SEC("license") = "GPL";

struct file_event {
    __u64 ts_ns;
    __u32 pid;
    __u32 tgid;
    __u32 type;
    __u32 flags_or_prot;
    __s32 dirfd;
    __u64 dev;
    __u64 inode;
    __u64 cgroup_id;
    char path[256];
};

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 32 * 1024 * 1024);
} events_rb SEC(".maps");

struct di_key {
    __u64 dev;
    __u64 ino;
};

struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __uint(max_entries, 65536);
    __type(key, struct di_key);
    __type(value, __u64);
} dedup_di SEC(".maps");

SEC("lsm/file_mmap")
int BPF_PROG(trace_mmap, struct file *file, unsigned long prot)
{
    if (!file) return 0;
    struct inode *inode = BPF_CORE_READ(file, f_inode);
    if (!inode) return 0;
    __u64 ino = (__u64)BPF_CORE_READ(inode, i_ino);
    __u64 dev = 0;
    struct super_block *sb = BPF_CORE_READ(inode, i_sb);
    if (sb) dev = (__u64)BPF_CORE_READ(sb, s_dev);

    struct di_key key = {.dev = dev, .ino = ino};
    __u64 now = bpf_ktime_get_ns();
    __u64 *lastp = bpf_map_lookup_elem(&dedup_di, &key);
    if (lastp) {
        if (now - *lastp < DEDUP_NS) return 0;
        *lastp = now;
    } else {
        bpf_map_update_elem(&dedup_di, &key, &now, BPF_ANY);
    }

    struct file_event *e = bpf_ringbuf_reserve(&events_rb, sizeof(*e), 0);
    if (e) {
        __u64 id = bpf_get_current_pid_tgid();
        e->ts_ns = now;
        e->pid = (__u32)id;
        e->tgid = id >> 32;
        e->type = EVT_MMAP;
        e->flags_or_prot = (__u32)prot;
        e->dirfd = 0;
        e->dev = dev;
        e->inode = ino;
        e->cgroup_id = bpf_get_current_cgroup_id();
        e->path[0] = '\0';
        bpf_ringbuf_submit(e, 0);
    }
    return 0;
}
