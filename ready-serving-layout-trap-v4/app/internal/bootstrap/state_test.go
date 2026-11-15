package bootstrap

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestLoadAllWithPathsLoadsOnlyHotPathsIntoWarmState(t *testing.T) {
	root := t.TempDir()
	paths := TestPaths(root)

	mustWriteFile(t, paths.DefaultConfig, []byte("service: {}\n"))
	mustWriteFile(t, paths.RoutesConfig, []byte(`{"routes":[]}`))
	mustWriteFile(t, paths.RuntimePolicy, []byte(`{"predict_reads_late":true}`))

	hotA := filepath.Join(root, "srv", "layout", "slot_000000_hot_000.bin")
	hotB := filepath.Join(root, "srv", "layout", "slot_000004_hot_001.bin")
	cold := filepath.Join(root, "srv", "layout", "slot_000001_cold_000.bin")
	late := filepath.Join(root, "srv", "layout", "slot_000003_late_000.bin")

	mustWriteBytes(t, hotA, 1024)
	mustWriteBytes(t, hotB, 2048)
	mustWriteBytes(t, cold, 4096)
	mustWriteBytes(t, late, 4096)

	plan := StartupPlan{
		HotPaths:         []string{hotA, hotB},
		LatePredictPaths: []string{late},
		ExpectedHotBytes: 1024 + 2048,
	}
	encoded, err := json.Marshal(plan)
	if err != nil {
		t.Fatalf("marshal startup plan: %v", err)
	}
	mustWriteFile(t, paths.StartupPlanPath, encoded)

	state, err := LoadAllWithPaths(paths)
	if err != nil {
		t.Fatalf("LoadAllWithPaths() error = %v", err)
	}

	snapshot := state.Snapshot()
	if !snapshot.Ready {
		t.Fatalf("state should be ready")
	}
	if snapshot.HotFilesLoaded != 2 {
		t.Fatalf("hot_files_loaded = %d, want 2", snapshot.HotFilesLoaded)
	}
	if snapshot.HotBytesLoaded != 3072 {
		t.Fatalf("hot_bytes_loaded = %d, want 3072", snapshot.HotBytesLoaded)
	}
	if len(snapshot.LoadedPaths) != 2 {
		t.Fatalf("loaded_paths count = %d, want 2", len(snapshot.LoadedPaths))
	}
	for _, path := range snapshot.LoadedPaths {
		if path == cold || path == late {
			t.Fatalf("loaded_paths should not include cold or late path: %s", path)
		}
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
