package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"ready-serving-layout-trap-v4/app/internal/bootstrap"
)

func TestPredictAcceptsSampleInput(t *testing.T) {
	root := t.TempDir()
	paths := bootstrap.TestPaths(root)

	mustWriteFile(t, paths.DefaultConfig, []byte("service: {}\n"))
	mustWriteFile(t, paths.RoutesConfig, []byte(`{"routes":[]}`))
	mustWriteFile(t, paths.RuntimePolicy, []byte(`{"predict_reads_late":true}`))

	hot := filepath.Join(root, "srv", "layout", "slot_000000_hot_000.bin")
	late := filepath.Join(root, "srv", "layout", "slot_000003_late_000.bin")
	mustWriteBytes(t, hot, 1024)
	mustWriteBytes(t, late, 2048)

	plan := bootstrap.StartupPlan{
		HotPaths:         []string{hot},
		LatePredictPaths: []string{late},
		ExpectedHotBytes: 1024,
	}
	encoded, err := json.Marshal(plan)
	if err != nil {
		t.Fatalf("marshal startup plan: %v", err)
	}
	mustWriteFile(t, paths.StartupPlanPath, encoded)

	state, err := bootstrap.LoadAllWithPaths(paths)
	if err != nil {
		t.Fatalf("LoadAllWithPaths() error = %v", err)
	}

	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatalf("runtime.Caller failed")
	}
	projectRoot := filepath.Clean(filepath.Join(filepath.Dir(thisFile), "..", "..", ".."))
	sampleInputPath := filepath.Join(projectRoot, "probes", "sample_input.json")
	body, err := os.ReadFile(sampleInputPath)
	if err != nil {
		t.Fatalf("read sample input: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/predict", bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	NewHandler(state).ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d, body=%s", recorder.Code, http.StatusOK, recorder.Body.String())
	}
	if got := recorder.Body.String(); got != "{\"predictions\":[[6]]}\n" {
		t.Fatalf("predict response = %q, want %q", got, "{\"predictions\":[[6]]}\n")
	}
}

func mustWriteFile(t *testing.T, path string, payload []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, payload, 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func mustWriteBytes(t *testing.T, path string, size int) {
	t.Helper()
	payload := make([]byte, size)
	for i := range payload {
		payload[i] = byte(i % 251)
	}
	mustWriteFile(t, path, payload)
}
