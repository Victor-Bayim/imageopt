# Restoration on 2026-11-15

The prior public snapshot contained placeholder stage bodies and a CLI that only
printed a start message. This revision restores the supplied implementation:
real stage dispatch, manifests/resume, container workload resolution, OPEN/MMAP
sampling and collection, event enrichment, candidate selection, optional LLM
providers/grounding/fallback, image application, cold-start measurement and reports.

The original source README is preserved as `ORIGINAL_PROJECT_README.md`. The source
manifest records 160 supplied files. One original test is kept in
`provenance/original/tests/test_quality_gate.py`; the active copy uses
`sys.executable` instead of a hard-coded `python3` subprocess. Its patch is retained.
The selector fixture and demonstration are new synthetic examples.

`python tools/validate_portable.py` runs 74 tests covering provider contracts,
mock/error responses, runtime argument forwarding, workload configuration,
budgeted path selection and sampling-quality classification. On 2026-11-15 all
74 passed on Windows / Python 3.12.14. `python tools/demo_selector.py` also generated
real hints/list/stats files and checked budget/directory behavior.

`python tools/verify_records.py` checks the 160 original hashes and recomputes the
9 preserved cold-start observations. The 28.94% deterministic result is a reported
aggregate; the separate 23.96% LLM-assisted batch used manually imported responses.
These checks do not reproduce Linux container performance or make an online API call.

Linux BPF compilation, containerd/stargz execution, the Go workload, and target
performance have not been executed in this Windows restoration session. Follow
USAGE.md and sampler/user/README.md on the intended Linux host. The sampler requires
its native dependencies and generated BPF skeleton. Large workload resources are
generated locally, and are not committed.
