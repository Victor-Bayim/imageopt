#ifndef SAMPLER_FILE_EVENT_ABI_H
#define SAMPLER_FILE_EVENT_ABI_H

#ifdef __cplusplus
#include <cstddef>
#include <cstdint>
typedef std::int32_t file_event_s32;
typedef std::uint32_t file_event_u32;
typedef std::uint64_t file_event_u64;
#define FILE_EVENT_OFFSETOF(type, field) offsetof(type, field)
#else
typedef __s32 file_event_s32;
typedef __u32 file_event_u32;
typedef __u64 file_event_u64;
#define FILE_EVENT_OFFSETOF(type, field) __builtin_offsetof(type, field)
#endif

#define FILE_EVENT_ABI_VERSION 2u
#define FILE_EVENT_PATH_LEN 256u
#define FILE_EVENT_SIZE_BYTES 312u

enum evt_type {
    EVT_OPEN = 1,
    EVT_MMAP = 2,
};

struct file_event {
    file_event_u64 ts_ns;
    file_event_u32 pid;
    file_event_u32 tgid;
    file_event_u32 type;
    file_event_u32 flags_or_prot;
    file_event_s32 dirfd;
    file_event_u32 reserved0;
    file_event_u64 dev;
    file_event_u64 inode;
    file_event_u64 cgroup_id;
    char path[FILE_EVENT_PATH_LEN];
};

#ifdef __cplusplus
#define FILE_EVENT_STATIC_ASSERT(cond, msg) static_assert((cond), msg)
#else
#define FILE_EVENT_STATIC_ASSERT(cond, msg) _Static_assert((cond), msg)
#endif

FILE_EVENT_STATIC_ASSERT(sizeof(struct file_event) == FILE_EVENT_SIZE_BYTES,
                         "file_event size mismatch");
FILE_EVENT_STATIC_ASSERT(FILE_EVENT_OFFSETOF(struct file_event, dirfd) == 24u,
                         "file_event dirfd offset mismatch");
FILE_EVENT_STATIC_ASSERT(FILE_EVENT_OFFSETOF(struct file_event, dev) == 32u,
                         "file_event dev offset mismatch");
FILE_EVENT_STATIC_ASSERT(FILE_EVENT_OFFSETOF(struct file_event, inode) == 40u,
                         "file_event inode offset mismatch");
FILE_EVENT_STATIC_ASSERT(FILE_EVENT_OFFSETOF(struct file_event, cgroup_id) == 48u,
                         "file_event cgroup_id offset mismatch");
FILE_EVENT_STATIC_ASSERT(FILE_EVENT_OFFSETOF(struct file_event, path) == 56u,
                         "file_event path offset mismatch");

#undef FILE_EVENT_STATIC_ASSERT
#undef FILE_EVENT_OFFSETOF

#endif
