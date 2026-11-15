"""Run selected ORIGINAL unit tests, without a container daemon, eBPF or API account."""
from pathlib import Path
import io, json, os, platform, sys, unittest, datetime

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT
MODULES = ['test_llm_provider_parsing','test_llm_provider_mock','test_llm_provider_config','test_orchestrator_runtime','test_orchestrator_workloads','test_pipeline_tools.SelectorTests','test_quality_gate']

def main():
    os.chdir(SOURCE)
    sys.path.insert(0,str(SOURCE))
    sys.path.insert(0,str(SOURCE/'tests'))
    sys.dont_write_bytecode = True
    suite = unittest.TestSuite()
    for module in MODULES:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(module))
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    text = log.getvalue().replace(str(ROOT),'<package-root>')
    out = ROOT/'validation'
    out.mkdir(exist_ok=True)
    (out/'portable-tests.txt').write_text(text,encoding='utf-8')
    summary = {'run_date':datetime.datetime.now(datetime.timezone.utc).isoformat(),'platform':platform.system(),'python':platform.python_version(),'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'success':result.wasSuccessful(),'modules':MODULES,'scope':'Original provider, runtime, workload, selector and quality-gate tests, with one interpreter-portability fix; no end-to-end container performance experiment.'}
    (out/'portable-tests.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(text)
    print(json.dumps(summary,indent=2))
    return 0 if result.wasSuccessful() else 1

if __name__ == '__main__':
    raise SystemExit(main())
