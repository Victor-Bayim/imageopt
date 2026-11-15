// SPDX-License-Identifier: (GPL-2.0 OR BSD-3-Clause)
// sampler/BPF/file_access_ebpf_trace.bpf.c
// 目标：稳定采集容器内 open/openat/openat2 + mmap（优先 LSM.s，失败回退 sys_enter_mmap）
// 过滤策略：严格 cgroup v2 子树白名单（用户态递归填充）
// 吞吐策略：ringbuf 32MB；用户态默认不逐条打印；BPF 端统计 drops/filters；LSM 路径对 (dev,inode) 做 1s 去重
// 注意：sleepable LSM 程序只能使用 array/hash/ringbuf/local_storage 类型的 map。

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_tracing.h>
#include "file_event_abi.h"

char LICENSE[] SEC("license") = "Dual BSD/GPL";

#define DEDUP_NS (1000000000ULL) /* 1s */
#define RINGBUF_SZ (1u << 25)    /* 32MB */
#define OPEN_AT_FDCWD (-100)

/* -------------------- 事件与统计 -------------------- */
const volatile __u32 file_event_abi_version = FILE_EVENT_ABI_VERSION;
const volatile __u32 file_event_size = sizeof(struct file_event);
const volatile __u32 file_event_dirfd_offset = __builtin_offsetof(struct file_event, dirfd);
const volatile __u32 file_event_dev_offset = __builtin_offsetof(struct file_event, dev);
const volatile __u32 file_event_inode_offset = __builtin_offsetof(struct file_event, inode);
const volatile __u32 file_event_cgroup_id_offset = __builtin_offsetof(struct file_event, cgroup_id);
const volatile __u32 file_event_path_offset = __builtin_offsetof(struct file_event, path);

enum stat_idx {
    STAT_OPEN_OK = 0,
    STAT_MMAP_OK = 1,
    STAT_FILTERED = 2,
    STAT_SAMPLING_DROP = 3,
    STAT_RINGBUF_DROP = 4,
    STAT_MAX
};

/* -------------------- Maps -------------------- */

/* 容器 cgroup 子树白名单（key=cgroup_id, value=1） */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 8192);
    __type(key, __u64);
    __type(value, __u8);
} container_cg_map SEC(".maps");

/* 环形缓冲（传事件到用户态） */
struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, RINGBUF_SZ);
} events_rb SEC(".maps");

/* 采样率（0~100），key=0 */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u32);
} sampling_rate SEC(".maps");

/* mmap 捕获窗口（CLOCK_MONOTONIC ns），key=0 */
struct mmap_cfg {
    __u64 end_ns;   /* 0 表示不限窗口 */
    __u32 enable;   /* 1/0 */
    __u32 _pad;
};
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct mmap_cfg);
} mmap_cfg SEC(".maps");

/* 统计数组（sleepable 兼容）：u64 counters[STAT_MAX] */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, STAT_MAX);
    __type(key, __u32);
    __type(value, __u64);
} stats_arr SEC(".maps");

/* LSM 路径用去重 HASH：key=(dev,ino) -> last_ts_ns */
struct di_key { __u64 dev; __u64 ino; };
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 8192);
    __type(key, struct di_key);
    __type(value, __u64);
} dedup_di SEC(".maps");

/* -------------------- 小工具 -------------------- */

static __always_inline void inc_stat(__u32 idx, __u64 v)
{
    if (idx >= STAT_MAX) return;
    __u64 *p = bpf_map_lookup_elem(&stats_arr, &idx);
    if (p) {
        __sync_fetch_and_add(p, v);
    }
}

static __always_inline bool pass_cgroup_filter(void)
{
    __u64 cg = bpf_get_current_cgroup_id();
    __u8  *ok = bpf_map_lookup_elem(&container_cg_map, &cg);
    if (!ok) {
        inc_stat(STAT_FILTERED, 1);
        return false;
    }
    return true;
}

static __always_inline bool should_sample_event(void)
{
    __u32 zero = 0;
    __u32 *ratep = bpf_map_lookup_elem(&sampling_rate, &zero);
    __u32 rate = ratep ? *ratep : 100;
    __u32 rnd = bpf_get_prandom_u32() % 100;
    bool yes = rnd < rate;
    if (!yes) inc_stat(STAT_SAMPLING_DROP, 1);
    return yes;
}

static __always_inline void submit_event_or_drop(struct file_event *e)
{
    if (!e) {
        inc_stat(STAT_RINGBUF_DROP, 1);
        return;
    }
    bpf_ringbuf_submit(e, 0);
}

/* -------------------- OPEN 系列（tracepoint） -------------------- */

static __always_inline void handle_open_common(const char *filename, __u32 flags, __s32 dirfd)
{
    if (!pass_cgroup_filter()) return;
    if (!should_sample_event()) return;

    struct file_event *e = bpf_ringbuf_reserve(&events_rb, sizeof(*e), 0);

    if (e) {
        __u64 id = bpf_get_current_pid_tgid();
        e->ts_ns = bpf_ktime_get_ns();
        e->pid   = (__u32)id;
        e->tgid  = id >> 32;
        e->type  = EVT_OPEN;
        e->flags_or_prot = flags;
        e->dirfd = dirfd;
        e->dev   = 0;
        e->inode = 0;
        e->cgroup_id = bpf_get_current_cgroup_id();
        e->path[0] = '\0';
        if (filename)
            bpf_probe_read_user_str(e->path, sizeof(e->path), filename);
        inc_stat(STAT_OPEN_OK, 1);
    }
    submit_event_or_drop(e);
}

SEC("tracepoint/syscalls/sys_enter_open")
int tp_sys_enter_open(struct trace_event_raw_sys_enter *ctx)
{
    const char *filename = (const char *)ctx->args[0];
    __u64 flags = ctx->args[1];
    handle_open_common(filename, (__u32)flags, OPEN_AT_FDCWD);
    return 0;
}

SEC("tracepoint/syscalls/sys_enter_openat")
int tp_sys_enter_openat(struct trace_event_raw_sys_enter *ctx)
{
    __s32 dirfd = (__s32)ctx->args[0];
    const char *filename = (const char *)ctx->args[1];
    __u64 flags = ctx->args[2];
    handle_open_common(filename, (__u32)flags, dirfd);
    return 0;
}

/* openat2(dfd, filename, struct open_how *how, size_t size) */
struct open_how_min {
    __u64 flags;
    __u64 mode;
    __u64 resolve;
};

SEC("tracepoint/syscalls/sys_enter_openat2")
int tp_sys_enter_openat2(struct trace_event_raw_sys_enter *ctx)
{
    __s32 dirfd = (__s32)ctx->args[0];
    const char *filename = (const char *)ctx->args[1];
    void *howp = (void *)ctx->args[2];
    __u64 sz = (__u64)ctx->args[3];
    struct open_how_min how = {};
    if (howp && sz > 0) {
        if (sz > sizeof(how)) sz = sizeof(how);
        bpf_probe_read_user(&how, sz, howp);
    }
    handle_open_common(filename, (__u32)how.flags, dirfd);
    return 0;
}

/* -------------------- MMAP（优先 LSM.s，回退 tracepoint） -------------------- */
/* LSM.s: mmap_file(file, reqprot, prot, flags, addr, addr_only)
 * 注意：sleepable LSM 不允许使用 bpf_d_path。仅读取 inode/dev，并留空 path。 */
SEC("lsm.s/mmap_file")
int BPF_PROG(lsm_mmap_file, struct file *file,
             unsigned long reqprot, unsigned long prot,
             unsigned long flags,  unsigned long addr,
             unsigned long addr_only)
{
    /* 窗口 gating */
    __u32 zero = 0;
    struct mmap_cfg *cfg = bpf_map_lookup_elem(&mmap_cfg, &zero);
    if (cfg) {
        if (!cfg->enable) return 0;
        __u64 now = bpf_ktime_get_ns();
        if (cfg->end_ns && now > cfg->end_ns) return 0;
    }

    if (!file) return 0;
    if (!pass_cgroup_filter()) return 0;
    if (!should_sample_event()) return 0;

    /* 提取 dev/inode */
    struct inode *inode = BPF_CORE_READ(file, f_inode);
    if (!inode) return 0;
    __u64 ino = (__u64)BPF_CORE_READ(inode, i_ino);
    __u64 dev = 0;
    struct super_block *sb = BPF_CORE_READ(inode, i_sb);
    if (sb) dev = (__u64)BPF_CORE_READ(sb, s_dev);

    /* 1s 去重 (dev,ino) */
    struct di_key key = {.dev = dev, .ino = ino};
    __u64 now = bpf_ktime_get_ns();
    __u64 *lastp = bpf_map_lookup_elem(&dedup_di, &key);
    if (lastp) {
        if (now - *lastp < DEDUP_NS) return 0;
        *lastp = now;
    } else {
        bpf_map_update_elem(&dedup_di, &key, &now, BPF_ANY);
    }

    /* 事件 */
    struct file_event *e = bpf_ringbuf_reserve(&events_rb, sizeof(*e), 0);
    if (e) {
        __u64 id = bpf_get_current_pid_tgid();
        e->ts_ns = now;
        e->pid   = (__u32)id;
        e->tgid  = id >> 32;
        e->type  = EVT_MMAP;
        e->flags_or_prot = (__u32)prot;
        e->dirfd = 0;
        e->dev   = dev;
        e->inode = ino;
        e->cgroup_id = bpf_get_current_cgroup_id();
        e->path[0] = '\0'; /* LSM 不用 d_path */
        inc_stat(STAT_MMAP_OK, 1);
    }
    submit_event_or_drop(e);
    return 0;
}

/* 回退：tracepoint/sys_enter_mmap，无法拿到 file*，dev/inode=0，路径留空 */
SEC("tracepoint/syscalls/sys_enter_mmap")
int tp_sys_enter_mmap(struct trace_event_raw_sys_enter *ctx)
{
    if (!pass_cgroup_filter()) return 0;
    if (!should_sample_event()) return 0;

    __u64 prot = ctx->args[2];

    struct file_event *e = bpf_ringbuf_reserve(&events_rb, sizeof(*e), 0);
    if (e) {
        __u64 id = bpf_get_current_pid_tgid();
        e->ts_ns = bpf_ktime_get_ns();
        e->pid   = (__u32)id;
        e->tgid  = id >> 32;
        e->type  = EVT_MMAP;
        e->flags_or_prot = (__u32)prot;
        e->dirfd = 0;
        e->dev   = 0;
        e->inode = 0;
        e->cgroup_id = bpf_get_current_cgroup_id();
        e->path[0] = '\0';
        inc_stat(STAT_MMAP_OK, 1);
    }
    submit_event_or_drop(e);
    return 0;
}
