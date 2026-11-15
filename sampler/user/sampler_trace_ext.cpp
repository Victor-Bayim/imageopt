// sampler/user/sampler_trace_ext.cpp
// 用户态：加载/附加 eBPF（含 LSM.s）、递归写入容器 cgroup 子树白名单、ringbuf 拉取事件、Prometheus/Redis 指标
// 重点：
// - 默认安静 (--quiet 默认 true)，仅指标；可用 --log-sample N 抽样打印，--log-file 输出到文件
// - ringbuf poll 20ms；采样器尽量减少 stdout 开销
// - 从 BPF stats_arr 读 ringbuf_drops / filtered_out / sampling_drops；open/mmap/total 仍由用户态基于事件计数
// - 有 LSM 就禁用 tp_sys_enter_mmap，没 LSM 就启用它作为回退
// - 在 attach 前写好 cgroup 白名单（递归遍历 /sys/fs/cgroup/<rel>）

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <linux/limits.h>
#include <pwd.h>
#include <sstream>
#include <string>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <thread>
#include <unistd.h>
#include <vector>
#include <deque>
#include <mutex>
#include <condition_variable>

#include <bpf/libbpf.h>
#include <bpf/bpf.h>

// skeleton
#include "file_access_ebpf_trace.skel.h"
#include "file_event_abi.h"

// prometheus-cpp (pull)
#include <prometheus/exposer.h>
#include <prometheus/registry.h>
#include <prometheus/counter.h>
#include <prometheus/gauge.h>

// redis++ (optional)
#include <sw/redis++/redis++.h>

using prometheus::BuildCounter;
using prometheus::BuildGauge;
using prometheus::Registry;

static std::atomic<bool> g_stop{false};
static void handle_sig(int){ g_stop = true; }

enum stat_idx {
    STAT_OPEN_OK = 0,
    STAT_MMAP_OK = 1,
    STAT_FILTERED = 2,
    STAT_SAMPLING_DROP = 3,
    STAT_RINGBUF_DROP = 4,
    STAT_MAX
};

/* -------------------- 实用函数 -------------------- */
static void bump_memlock_rlimit() {
    rlimit r{RLIM_INFINITY, RLIM_INFINITY};
    if (setrlimit(RLIMIT_MEMLOCK, &r) != 0) {
        std::cerr << "setrlimit MEMLOCK failed: " << strerror(errno) << "\n";
    }
}

static bool kernel_has_bpf_lsm() {
    if (access("/sys/kernel/btf/vmlinux", R_OK) != 0) return false;
    std::ifstream f("/sys/kernel/security/lsm");
    if (!f.good()) return false;
    std::string s; std::getline(f, s, '\0');
    return s.find("bpf") != std::string::npos;
}

/* 获取 /proc/<pid>/cgroup 中 v2 统一层级的相对路径 */
static std::string read_cgroup_v2_path(pid_t pid) {
    char fn[64]; snprintf(fn, sizeof(fn), "/proc/%d/cgroup", pid);
    FILE* fp = fopen(fn, "re");
    if (!fp) return {};
    char *line = nullptr; size_t cap = 0; std::string out;
    while (getline(&line, &cap, fp) != -1) {
        char *p = strstr(line, "::/");
        if (p) {
            out = p + 2; // 指向 "/..."
            while (!out.empty() && (out.back()=='\n' || out.back()=='\r')) out.pop_back();
            break;
        }
    }
    if (line) free(line);
    fclose(fp);
    return out;
}

/* name_to_handle_at: 获取 cgroup v2 目录的 8 字节 cgroup_id */
#ifndef MAX_HANDLE_SZ
#define MAX_HANDLE_SZ 128
#endif
struct file_handle_t {
    unsigned int handle_bytes;
    int handle_type;
    unsigned char f_handle[MAX_HANDLE_SZ];
};
static bool get_cgroup_id_by_path(const std::string& path, uint64_t &cgid_out) {
    file_handle_t fh{};
    fh.handle_bytes = MAX_HANDLE_SZ;
    int mount_id = 0;
    int ret = syscall(SYS_name_to_handle_at, AT_FDCWD, path.c_str(), &fh, &mount_id, 0);
    if (ret != 0) return false;
    if (fh.handle_bytes < 8) return false;
    memcpy(&cgid_out, fh.f_handle, 8);
    return true;
}

/* 递归遍历 /sys/fs/cgroup/<relpath>，把所有子目录的 cgroup_id 写入 map */
static void walk_cgroup_tree_and_fill(int map_fd, const std::string& rel) {
    std::string root = "/sys/fs/cgroup";
    std::string base = (rel.empty() || rel[0]!='/') ? (root + "/" + rel) : (root + rel);
    while (base.size() > 1 && base.back()=='/') base.pop_back();

    std::vector<std::string> stack;
    stack.push_back(base);
    while (!stack.empty()) {
        std::string cur = std::move(stack.back()); stack.pop_back();

        uint64_t cgid = 0;
        if (get_cgroup_id_by_path(cur, cgid)) {
            uint8_t one = 1;
            bpf_map_update_elem(map_fd, &cgid, &one, BPF_ANY);
        }

        DIR* d = opendir(cur.c_str());
        if (!d) continue;
        struct dirent* de;
        while ((de = readdir(d)) != nullptr) {
            if (de->d_name[0]=='.') continue;
            if (de->d_type == DT_DIR || de->d_type == DT_UNKNOWN) {
                std::string child = cur + "/" + de->d_name;
                stack.push_back(child);
            }
        }
        closedir(d);
    }
}

/* flag 解码（仅用于抽样日志） */
#include <sys/mman.h>
#include <fcntl.h>
static std::string decode_open_flags(uint32_t f) {
    std::vector<std::string> v;
    if ((f & O_WRONLY) || (f & O_RDWR)) v.push_back("write");
    else v.push_back("read");
    if (f & O_CREAT) v.push_back("creat");
    if (f & O_TRUNC) v.push_back("trunc");
    if (f & O_APPEND) v.push_back("append");
    if (f & O_DIRECTORY) v.push_back("dir");
    if (f & O_NOFOLLOW) v.push_back("nofollow");
    if (f & O_CLOEXEC) v.push_back("cloexec");
    if (f & O_EXCL) v.push_back("excl");
    std::ostringstream oss;
    for (size_t i=0;i<v.size();++i){ if(i) oss<<'|'; oss<<v[i]; }
    return oss.str();
}
static std::string decode_mmap_prot(uint32_t p) {
    std::vector<std::string> v;
    if (p & PROT_READ) v.push_back("R");
    if (p & PROT_WRITE) v.push_back("W");
    if (p & PROT_EXEC) v.push_back("X");
    if (p & PROT_NONE) v.push_back("NONE");
    std::ostringstream oss;
    for (size_t i=0;i<v.size();++i){ if(i) oss<<'|'; oss<<v[i]; }
    return v.empty() ? "0" : oss.str();
}

/* -------------------- Prometheus / Redis -------------------- */
struct Metrics {
    std::shared_ptr<Registry> reg;
    prometheus::Counter* file_evt_total{nullptr};
    prometheus::Counter* open_evt_total{nullptr};
    prometheus::Counter* mmap_evt_total{nullptr};
    prometheus::Counter* file_sink_lines_total{nullptr};
    prometheus::Counter* file_sink_bytes_total{nullptr};
    prometheus::Counter* file_sink_write_failures_total{nullptr};
    prometheus::Counter* redis_queue_dropped_total{nullptr};
    prometheus::Counter* redis_publish_failures_total{nullptr};

    prometheus::Counter* rb_drops_total{nullptr};
    prometheus::Counter* filtered_total{nullptr};
    prometheus::Counter* sampling_drops_total{nullptr};

    prometheus::Gauge*   eps_gauge{nullptr};
    prometheus::Gauge*   file_sink_enabled{nullptr};
    std::deque<std::chrono::steady_clock::time_point> tsq;

    static std::unique_ptr<Metrics> create(uint16_t port, const std::string& label_value) {
        auto m = std::make_unique<Metrics>();
        static auto exposer = std::make_unique<prometheus::Exposer>("0.0.0.0:" + std::to_string(port));
        m->reg = std::make_shared<Registry>();
        exposer->RegisterCollectable(m->reg);

        auto& fam_cnt1 = BuildCounter().Name("file_events_total")
            .Help("Total sampled file events (open+mmap)")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->file_evt_total = &fam_cnt1.Add({});

        auto& fam_cnt2 = BuildCounter().Name("open_events_total")
            .Help("Open events successfully emitted")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->open_evt_total = &fam_cnt2.Add({});

        auto& fam_cnt3 = BuildCounter().Name("mmap_events_total")
            .Help("Mmap events successfully emitted")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->mmap_evt_total = &fam_cnt3.Add({});

        auto& fam_drop = BuildCounter().Name("ringbuf_drops_total")
            .Help("Ringbuffer reserve failures (drops)")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->rb_drops_total = &fam_drop.Add({});

        auto& fam_filtered = BuildCounter().Name("filtered_out_total")
            .Help("Events filtered out by cgroup whitelist")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->filtered_total = &fam_filtered.Add({});

        auto& fam_sampling = BuildCounter().Name("sampling_drops_total")
            .Help("Events dropped due to sampling rate")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->sampling_drops_total = &fam_sampling.Add({});

        auto& fam_file_sink_lines = BuildCounter().Name("file_sink_lines_total")
            .Help("Lines successfully written to the local file sink")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->file_sink_lines_total = &fam_file_sink_lines.Add({});

        auto& fam_file_sink_bytes = BuildCounter().Name("file_sink_bytes_total")
            .Help("Bytes successfully written to the local file sink")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->file_sink_bytes_total = &fam_file_sink_bytes.Add({});

        auto& fam_file_sink_fail = BuildCounter().Name("file_sink_write_failures_total")
            .Help("File sink write failures")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->file_sink_write_failures_total = &fam_file_sink_fail.Add({});

        auto& fam_redis_drop = BuildCounter().Name("redis_queue_dropped_total")
            .Help("Redis queue drops in user-space pipeline")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->redis_queue_dropped_total = &fam_redis_drop.Add({});

        auto& fam_redis_fail = BuildCounter().Name("redis_publish_failures_total")
            .Help("Redis publish failures in user-space pipeline")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->redis_publish_failures_total = &fam_redis_fail.Add({});

        auto& fam_gauge = BuildGauge().Name("file_events_per_second")
            .Help("Sampled file events per second (1s window)")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->eps_gauge = &fam_gauge.Add({});

        auto& fam_file_sink_enabled = BuildGauge().Name("file_sink_enabled")
            .Help("Whether the local file sink is enabled")
            .Labels({{"target", label_value}})
            .Register(*m->reg);
        m->file_sink_enabled = &fam_file_sink_enabled.Add({});
        m->file_sink_enabled->Set(0.0);
        return m;
    }
    void on_event(uint32_t type) {
        file_evt_total->Increment();
        if (type == EVT_OPEN) open_evt_total->Increment();
        else if (type == EVT_MMAP) mmap_evt_total->Increment();

        auto now = std::chrono::steady_clock::now();
        tsq.push_back(now);
        while (!tsq.empty() &&
               std::chrono::duration_cast<std::chrono::milliseconds>(now - tsq.front()).count() > 1000) {
            tsq.pop_front();
        }
        eps_gauge->Set(static_cast<double>(tsq.size()));
    }
    void add_rb_drops(uint64_t v){ if (v) rb_drops_total->Increment((double)v); }
    void add_filtered(uint64_t v){ if (v) filtered_total->Increment((double)v); }
    void add_sampling_drops(uint64_t v){ if (v) sampling_drops_total->Increment((double)v); }
    void add_file_sink_line(size_t bytes) {
        file_sink_lines_total->Increment();
        if (bytes) file_sink_bytes_total->Increment(static_cast<double>(bytes));
    }
    void add_file_sink_failures(uint64_t v){ if (v) file_sink_write_failures_total->Increment((double)v); }
    void set_file_sink_enabled(bool enabled_flag){ file_sink_enabled->Set(enabled_flag ? 1.0 : 0.0); }
    void add_redis_queue_drops(uint64_t v){ if (v) redis_queue_dropped_total->Increment((double)v); }
    void add_redis_publish_failures(uint64_t v){ if (v) redis_publish_failures_total->Increment((double)v); }
};

struct RedisPipeline {
    Metrics* metrics{nullptr};
    std::unique_ptr<sw::redis::Redis> redis; // optional
    std::string list_key{"file_open_events"};
    std::mutex mu;
    std::condition_variable cv;
    std::deque<std::string> q;
    std::atomic<bool> stop{false};
    std::atomic<uint64_t> dropped{0};
    std::atomic<uint64_t> publish_failures{0};
    std::thread worker;

    void start(const std::string& url, const std::string& key, Metrics* m = nullptr) {
        metrics = m;
        if (!key.empty()) list_key = key;
        redis = std::make_unique<sw::redis::Redis>(url);
        worker = std::thread([this]{
            for (;;) {
                std::unique_lock<std::mutex> lk(mu);
                cv.wait_for(lk, std::chrono::milliseconds(200), [&]{ return stop.load() || !q.empty(); });
                if (q.empty()) {
                    if (stop.load()) break;
                    continue;
                }
                std::string s = std::move(q.front()); q.pop_front();
                lk.unlock();
                try { redis->rpush(list_key, s); }
                catch (...) {
                    publish_failures.fetch_add(1, std::memory_order_relaxed);
                    if (metrics) metrics->add_redis_publish_failures(1);
                }
            }
        });
    }
    void push(std::string s) {
        if (!redis) return;
        std::lock_guard<std::mutex> lk(mu);
        if (q.size() > 8192) {
            q.pop_front();
            dropped.fetch_add(1, std::memory_order_relaxed);
            if (metrics) metrics->add_redis_queue_drops(1);
        }
        q.emplace_back(std::move(s));
        cv.notify_one();
    }
    void stop_and_join() {
        if (!redis) return;
        stop.store(true);
        cv.notify_all();
        if (worker.joinable()) worker.join();
        auto d = dropped.load(std::memory_order_relaxed);
        if (d > 0) {
            std::cerr << "[sampler] WARN: redis queue dropped events=" << d << "\n";
        }
        auto pf = publish_failures.load(std::memory_order_relaxed);
        if (pf > 0) {
            std::cerr << "[sampler] WARN: redis publish failures=" << pf << "\n";
        }
    }
};

struct FileSink {
    Metrics* metrics{nullptr};
    std::string path;
    std::unique_ptr<std::ofstream> file;
    std::atomic<uint64_t> write_failures{0};

    bool start(const std::string& sink_path, Metrics* m = nullptr) {
        metrics = m;
        path = sink_path;
        file = std::make_unique<std::ofstream>(path, std::ios::out | std::ios::app);
        if (metrics) metrics->set_file_sink_enabled(file && file->is_open());
        return file->is_open();
    }

    bool enabled() const {
        return file && file->is_open();
    }

    void push_line(const std::string& line) {
        if (!enabled()) {
            return;
        }
        (*file) << line << "\n";
        file->flush();
        if (!file->good()) {
            write_failures.fetch_add(1, std::memory_order_relaxed);
            if (metrics) metrics->add_file_sink_failures(1);
            file->clear();
        } else if (metrics) {
            metrics->add_file_sink_line(line.size() + 1);
        }
    }

    void stop() {
        if (!enabled()) {
            return;
        }
        file->flush();
        file->close();
        if (metrics) metrics->set_file_sink_enabled(false);
        auto failures = write_failures.load(std::memory_order_relaxed);
        if (failures > 0) {
            std::cerr << "[sampler] WARN: file sink write failures=" << failures
                      << " path=" << path << "\n";
        }
    }
};

/* -------------------- 事件回调（抽样日志） -------------------- */
struct LogCtl {
    bool quiet{true};
    uint32_t sample_n{0}; // 0 = 不打印；>0 每 N 条打印1条
    uint64_t cnt{0};
    std::unique_ptr<std::ofstream> file;
};

static void maybe_log(const file_event* e, LogCtl& lc)
{
    if (lc.quiet) return;
    if (lc.sample_n == 0) return;
    lc.cnt++;
    if ((lc.cnt % lc.sample_n) != 0) return;

    std::ostringstream oss;
    std::string kind = (e->type == EVT_MMAP) ? "MMAP" : "OPEN";
    std::string flag_s = (e->type == EVT_MMAP) ? decode_mmap_prot(e->flags_or_prot)
                                               : decode_open_flags(e->flags_or_prot);
    oss << "[" << kind << "] pid=" << e->pid
        << " cgid=" << (unsigned long long)e->cgroup_id
        << " flags/prot=" << flag_s
        << " dirfd=" << e->dirfd
        << " dev=" << (unsigned long long)e->dev
        << " ino=" << (unsigned long long)e->inode
        << " path=\"" << e->path << "\""
        << " ts=" << e->ts_ns;

    if (lc.file && lc.file->is_open()) {
        (*lc.file) << oss.str() << "\n";
        lc.file->flush();
    } else {
        std::cout << oss.str() << "\n";
    }
}

struct CB {
    Metrics* metrics{};
    RedisPipeline* rp{};
    FileSink* fs{};
    LogCtl* log{};
    int sampling_rate{100};
};

static bool validate_file_event_abi(const file_access_ebpf_trace_bpf* skel) {
    if (!skel || !skel->rodata) {
        std::cerr << "BPF skeleton rodata is unavailable; cannot validate file_event ABI\n";
        return false;
    }

    const auto& abi = *skel->rodata;
    const struct {
        const char* name;
        uint32_t actual;
        uint32_t expected;
    } checks[] = {
        {"version", abi.file_event_abi_version, FILE_EVENT_ABI_VERSION},
        {"size", abi.file_event_size, static_cast<uint32_t>(sizeof(file_event))},
        {"dirfd_offset", abi.file_event_dirfd_offset, static_cast<uint32_t>(offsetof(file_event, dirfd))},
        {"dev_offset", abi.file_event_dev_offset, static_cast<uint32_t>(offsetof(file_event, dev))},
        {"inode_offset", abi.file_event_inode_offset, static_cast<uint32_t>(offsetof(file_event, inode))},
        {"cgroup_id_offset", abi.file_event_cgroup_id_offset, static_cast<uint32_t>(offsetof(file_event, cgroup_id))},
        {"path_offset", abi.file_event_path_offset, static_cast<uint32_t>(offsetof(file_event, path))},
    };

    for (const auto& check : checks) {
        if (check.actual != check.expected) {
            std::cerr << "file_event ABI mismatch: " << check.name
                      << " actual=" << check.actual
                      << " expected=" << check.expected << "\n";
            return false;
        }
    }
    return true;
}

static std::string json_escape(const char* s);

static std::string encode_event_json(const file_event* e, int sampling_rate) {
    std::ostringstream oss;
    oss << "{\"ts\":" << e->ts_ns
        << ",\"pid\":" << e->pid
        << ",\"tgid\":" << e->tgid
        << ",\"type\":\"" << (e->type==EVT_MMAP?"MMAP":"OPEN") << "\""
        << ",\"flags_or_prot\":" << e->flags_or_prot
        << ",\"dirfd\":" << e->dirfd
        << ",\"dev\":" << (unsigned long long)e->dev
        << ",\"ino\":" << (unsigned long long)e->inode
        << ",\"cgid\":" << (unsigned long long)e->cgroup_id
        << ",\"sample_rate\":" << sampling_rate
        << ",\"path\":\"" << json_escape(e->path) << "\"}";
    return oss.str();
}

static std::string json_escape(const char* s) {
    std::ostringstream oss;
    if (!s) return {};
    for (const unsigned char* p = reinterpret_cast<const unsigned char*>(s); *p; ++p) {
        switch (*p) {
            case '\\': oss << "\\\\"; break;
            case '"':  oss << "\\\""; break;
            case '\n': oss << "\\n"; break;
            case '\r': oss << "\\r"; break;
            case '\t': oss << "\\t"; break;
            default:
                if (*p < 0x20) {
                    oss << "\\u"
                        << std::hex << std::setw(4) << std::setfill('0') << static_cast<int>(*p)
                        << std::dec << std::setfill(' ');
                } else {
                    oss << static_cast<char>(*p);
                }
        }
    }
    return oss.str();
}

static int on_rb(void* ctx, void* data, size_t) {
    auto* c = reinterpret_cast<CB*>(ctx);
    auto* e = reinterpret_cast<file_event*>(data);

    if (c->metrics) c->metrics->on_event(e->type);
    if (c->log)     maybe_log(e, *c->log);

    if ((c->rp && c->rp->redis) || (c->fs && c->fs->enabled())) {
        std::string payload = encode_event_json(e, c->sampling_rate);
        if (c->rp && c->rp->redis) {
            c->rp->push(payload);
        }
        if (c->fs && c->fs->enabled()) {
            c->fs->push_line(payload);
        }
    }
    return 0;
}

/* -------------------- CLI & helpers -------------------- */
static void usage(const char* prog) {
    std::cerr <<
      "Usage: sudo " << prog << " --pid <PID>\n"
      "               [--prom-port 8000]\n"
      "               [--sampling-rate 100]\n"
      "               [--mmap-window SEC]\n"
      "               [--redis-url redis://127.0.0.1:6379/0]\n"
      "               [--redis-key file_open_events]\n"
      "               [--file-sink /path/to/raw_events.jsonl]\n"
      "               [--quiet 1]\n"
      "               [--log-sample N]\n"
      "               [--log-file /path/to/log]\n"
      "               [--cg-refresh SEC]\n";
}

/* 从容器中任意进程 PID 推导 v2 相对路径，并递归填充子 cgroup id 到 map */
static bool prime_cgroup_whitelist(int map_fd, pid_t pid) {
    std::string rel = read_cgroup_v2_path(pid);
    if (rel.empty()) {
        std::cerr << "[sampler] cannot read v2 cgroup path from /proc/" << pid << "/cgroup\n";
        return false;
    }
    walk_cgroup_tree_and_fill(map_fd, rel);
    return true;
}

/* stats 泵：每秒读 BPF stats_arr，转成 Prom counters */
static void stats_pump(std::atomic<bool>& stop, int stats_fd, Metrics* m) {
    std::vector<uint64_t> prev(STAT_MAX, 0), cur(STAT_MAX, 0);
    while (!stop.load()) {
        for (uint32_t i=0;i<STAT_MAX;++i) {
            uint64_t v = 0;
            bpf_map_lookup_elem(stats_fd, &i, &v);
            cur[i] = v;
        }
        auto d = [&](uint32_t idx)->uint64_t {
            if (cur[idx] >= prev[idx]) return cur[idx] - prev[idx];
            return cur[idx];
        };
        if (m) {
            m->add_rb_drops(d(STAT_RINGBUF_DROP));
            m->add_filtered(d(STAT_FILTERED));
            m->add_sampling_drops(d(STAT_SAMPLING_DROP));
        }
        prev = cur;
        for (int i=0;i<10 && !stop.load(); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
}

/* -------------------- main -------------------- */
int main(int argc, char** argv) {
    signal(SIGINT, handle_sig);
    signal(SIGTERM, handle_sig);
    bump_memlock_rlimit();

    pid_t pid = -1;
    uint16_t prom_port = 8000;
    int sampling = 100;
    int mmap_window_sec = 0;
    std::string redis_url;
    std::string redis_key = "file_open_events";
    std::string file_sink_path;
    bool quiet = true;
    uint32_t log_sample = 0;
    std::string log_file;
    int cg_refresh_sec = 0;

    auto need = [&](int i){ return i+1 < argc; };
    for (int i=1;i<argc;++i) {
        std::string a = argv[i];
        if (a=="-h" || a=="--help") {
            usage(argv[0]);
            return 0;
        }
        if (a=="--pid" && need(i)) pid = (pid_t)std::stol(argv[++i]);
        else if (a=="--prom-port" && need(i)) prom_port = (uint16_t)std::stoi(argv[++i]);
        else if (a=="--sampling-rate" && need(i)) sampling = std::stoi(argv[++i]);
        else if (a=="--mmap-window" && need(i)) mmap_window_sec = std::stoi(argv[++i]);
        else if (a=="--redis-url" && need(i)) redis_url = argv[++i];
        else if (a=="--redis-key" && need(i)) redis_key = argv[++i];
        else if (a=="--file-sink" && need(i)) file_sink_path = argv[++i];
        else if (a=="--quiet" && need(i)) quiet = (std::stoi(argv[++i]) != 0);
        else if (a=="--log-sample" && need(i)) log_sample = (uint32_t)std::stoul(argv[++i]);
        else if (a=="--log-file" && need(i)) log_file = argv[++i];
        else if (a=="--cg-refresh" && need(i)) cg_refresh_sec = std::stoi(argv[++i]);
        else { usage(argv[0]); return 1; }
    }
    if (pid < 0) { usage(argv[0]); return 1; }

    std::cout << "Monitoring pid=" << pid
              << " mmap_window=" << mmap_window_sec
              << " sampling_rate=" << sampling
              << " quiet=" << (quiet?"1":"0")
              << " log_sample=" << log_sample
              << "\n";

    // 打开 skeleton
    file_access_ebpf_trace_bpf* skel = file_access_ebpf_trace_bpf__open();
    if (!skel) { std::cerr << "open skeleton failed\n"; return 1; }
    if (!validate_file_event_abi(skel)) {
        file_access_ebpf_trace_bpf__destroy(skel);
        return 1;
    }

    // LSM 可用性判定与回退：有 LSM -> 关掉 tp_sys_enter_mmap；无 LSM -> 关掉 lsm_mmap_file
    bool have_lsm = kernel_has_bpf_lsm();
    if (have_lsm) {
        bpf_program__set_autoload(skel->progs.tp_sys_enter_mmap, false);
    } else {
        bpf_program__set_autoload(skel->progs.lsm_mmap_file, false);
        std::cerr << "[warn] kernel has no BPF LSM or BTF; mmap capture via tracepoint.\n";
    }

    // load
    if (file_access_ebpf_trace_bpf__load(skel)) {
        std::cerr << "load skeleton failed\n";
        file_access_ebpf_trace_bpf__destroy(skel);
        return 1;
    }

    // 配 sampling_rate 与 mmap 窗口（load 后更新 map）
    {
        __u32 zero = 0;
        __u32 rate = (sampling < 0) ? 0 : (sampling > 100 ? 100 : sampling);
        if (bpf_map_update_elem(bpf_map__fd(skel->maps.sampling_rate), &zero, &rate, BPF_ANY) != 0) {
            std::cerr << "bpf_map_update_elem(sampling_rate) failed: " << strerror(errno) << "\n";
        }
        if (have_lsm) {
            struct timespec ts{};
            clock_gettime(CLOCK_MONOTONIC, &ts);
            uint64_t now_ns = (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
            struct mmap_cfg { uint64_t end_ns; uint32_t enable; uint32_t _pad; } cfg{};
            // 0 means "no window limit", not "disable mmap capture".
            cfg.enable = 1u;
            cfg.end_ns = (mmap_window_sec > 0)
                ? now_ns + (uint64_t)mmap_window_sec * 1000000000ull
                : 0;
            if (bpf_map_update_elem(bpf_map__fd(skel->maps.mmap_cfg), &zero, &cfg, BPF_ANY) != 0) {
                std::cerr << "bpf_map_update_elem(mmap_cfg) failed: " << strerror(errno) << "\n";
            }
        }
    }

    // 递归填充容器 cgroup 子树（关键！）
    int cgmap_fd = bpf_map__fd(skel->maps.container_cg_map);
    if (!prime_cgroup_whitelist(cgmap_fd, pid)) {
        std::cerr << "[sampler] WARNING: cgroup whitelist not primed; you may see no events\n";
    }

    // 可选：周期刷新 cgroup 子树（容器新进程层级）
    std::atomic<bool> cg_stop{false};
    std::thread cg_thr;
    if (cg_refresh_sec > 0) {
        cg_thr = std::thread([&, cgmap_fd]{
            while (!cg_stop.load()) {
                prime_cgroup_whitelist(cgmap_fd, pid);
                for (int i=0;i<cg_refresh_sec*10 && !cg_stop.load(); ++i)
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
        });
    }

    // attach
    if (file_access_ebpf_trace_bpf__attach(skel)) {
        std::cerr << "attach skeleton failed\n";
        cg_stop.store(true); if (cg_thr.joinable()) cg_thr.join();
        file_access_ebpf_trace_bpf__destroy(skel);
        return 1;
    }

    // Prometheus
    auto metrics = Metrics::create(prom_port, std::to_string(pid));

    // Redis（可选）
    RedisPipeline rp;
    if (!redis_url.empty()) {
        try {
            rp.start(redis_url, redis_key, metrics.get());
            std::cout << "Redis connected: " << redis_url << " key=" << redis_key << "\n";
        }
        catch (const std::exception& e) { std::cerr << "Redis connect failed: " << e.what() << "\n"; }
    }

    // 本地 file sink（推荐）
    FileSink fs;
    if (!file_sink_path.empty()) {
        if (!fs.start(file_sink_path, metrics.get())) {
            std::cerr << "[sampler] cannot open file sink: " << file_sink_path << "\n";
            rp.stop_and_join();
            cg_stop.store(true);
            if (cg_thr.joinable()) cg_thr.join();
            file_access_ebpf_trace_bpf__destroy(skel);
            return 1;
        }
        std::cout << "File sink enabled: " << file_sink_path << "\n";
    }

    // 日志控制
    LogCtl logctl;
    logctl.quiet = quiet;
    logctl.sample_n = log_sample;
    if (!log_file.empty()) {
        logctl.file = std::make_unique<std::ofstream>(log_file, std::ios::out|std::ios::app);
        if (!logctl.file->is_open()) {
            std::cerr << "[sampler] WARN: cannot open log file: " << log_file << "\n";
        }
    }

    // ringbuf
    CB cb{metrics.get(), rp.redis ? &rp : nullptr, fs.enabled() ? &fs : nullptr, &logctl, sampling};
    ring_buffer* rb = ring_buffer__new(bpf_map__fd(skel->maps.events_rb), on_rb, &cb, nullptr);
    if (!rb) {
        std::cerr << "ring_buffer__new failed\n";
        fs.stop();
        rp.stop_and_join();
        cg_stop.store(true);
        if (cg_thr.joinable()) cg_thr.join();
        file_access_ebpf_trace_bpf__destroy(skel);
        return 1;
    }

    // stats 泵（读取 BPF 端统计）
    std::atomic<bool> stats_stop{false};
    std::thread stats_thr(stats_pump, std::ref(stats_stop),
                          bpf_map__fd(skel->maps.stats_arr), metrics.get());

    std::cout << "[sampler] ready. Ctrl-C to stop.\n";

    while (!g_stop.load()) {
        int err = ring_buffer__poll(rb, 20 /* ms */);
        if (err == -EINTR) break;
        if (err < 0) {
            std::cerr << "ring_buffer__poll failed: " << err << "\n";
            break;
        }
    }

    // 收尾
    stats_stop.store(true);
    if (stats_thr.joinable()) stats_thr.join();

    ring_buffer__free(rb);
    fs.stop();
    rp.stop_and_join();

    cg_stop.store(true);
    if (cg_thr.joinable()) cg_thr.join();

    file_access_ebpf_trace_bpf__destroy(skel);
    return 0;
}
