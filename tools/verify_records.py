"""Recompute recorded metrics and verify the delivered source snapshot. No external services."""
from pathlib import Path
from collections import defaultdict
from statistics import median
import csv, hashlib, json

ROOT = Path(__file__).resolve().parents[1]

def main():
    manifest = json.loads((ROOT/'provenance/restored-source.json').read_text(encoding='utf-8'))
    for entry in manifest['files']:
        data = (ROOT/entry['path']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry['sha256'], entry['path']
    run = ROOT/'benchmarks/historical/llm-manual'
    rows = list(csv.DictReader((run/'coldstart_compare.csv').open(encoding='utf-8', newline='')))
    groups = defaultdict(list)
    for row in rows:
        assert row['status'] == 'ok', row
        assert int(row['lazy_guard_failed']) == 0
        assert int(row['hot_files_loaded']) == 32
        assert int(row['hot_bytes_loaded']) == 67108864
        groups[row['label']].append(int(row['ready_ms']))
        if row['label'] != 'baseline':
            assert int(row['explicit_pull_executed']) == 0
            assert int(row['full_pull_evidence']) == 0
            assert row['ready_ms_semantics'] == 'lazy_ready_ms'
    assert set(groups) == {'baseline','unoptimized_lazy','optimized'}
    assert all(len(v) == 3 for v in groups.values())
    medians = {key: median(values) for key,values in groups.items()}
    assert medians == {'baseline':171702,'unoptimized_lazy':2763,'optimized':2101}
    report = json.loads((run/'benchmark_result.json').read_text(encoding='utf-8'))
    for key, field in [('baseline','baseline_median_full_pull_ready_ms'),('unoptimized_lazy','unoptimized_median_lazy_ready_ms'),('optimized','optimized_median_lazy_ready_ms')]:
        assert medians[key] == report[field], field
    delta = (1 - medians['optimized']/medians['unoptimized_lazy']) * 100
    assert abs(delta-report['optimized_vs_unoptimized_improvement_pct']) < 1e-10
    assert round(delta,2) == 23.96
    llm = json.loads((run/'llm_result.json').read_text(encoding='utf-8'))
    assert llm['requested_mode'] == 'llm-manual' and llm['api_attempted'] is False
    deterministic = json.loads((ROOT/'benchmarks/historical/deterministic-summary.json').read_text(encoding='utf-8'))
    rule_delta = (1-deterministic['optimized_lazy_ready_ms']/deterministic['unoptimized_lazy_ready_ms'])*100
    assert round(rule_delta,2) == 28.94
    assert deterministic['raw_per_run_records_in_this_package'] is False
    summary = {'verified_files':len(manifest['files']),'recorded_llm_manual_samples':len(rows),'llm_manual_medians_ms':medians,'llm_manual_lazy_improvement_pct':round(delta,2),'deterministic_reported_aggregate_improvement_pct':round(rule_delta,2),'method':'Recalculation of supplied records, not a new container benchmark run.'}
    print(json.dumps(summary,indent=2))

if __name__ == '__main__':
    main()
