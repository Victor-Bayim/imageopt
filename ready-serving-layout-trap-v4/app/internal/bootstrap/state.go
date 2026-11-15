package bootstrap

import (
	"fmt"
	"sync"
	"time"
)

type WarmState struct {
	Ready               bool     `json:"ready"`
	BootstrapDurationMs int64    `json:"bootstrap_duration_ms"`
	HotFilesLoaded      int      `json:"hot_files_loaded"`
	HotBytesLoaded      int64    `json:"hot_bytes_loaded"`
	LoadedPaths         []string `json:"loaded_paths"`
	Checksum            uint64   `json:"checksum"`
}

type AppState struct {
	mu    sync.RWMutex
	paths Paths
	plan  StartupPlan
	warm  WarmState
}

func LoadAll() (*AppState, error) {
	return LoadAllWithPaths(DefaultPaths())
}

func LoadAllWithPaths(paths Paths) (*AppState, error) {
	start := time.Now()

	plan, err := LoadStartupPlan(paths.StartupPlanPath)
	if err != nil {
		return nil, err
	}

	for _, path := range []string{paths.StartupPlanPath, paths.DefaultConfig, paths.RoutesConfig, paths.RuntimePolicy} {
		if _, err := ReadPath(path); err != nil {
			return nil, err
		}
	}

	warm := WarmState{
		LoadedPaths: make([]string, 0, len(plan.HotPaths)),
	}

	for _, path := range plan.HotPaths {
		stats, err := ReadPath(path)
		if err != nil {
			return nil, err
		}
		warm.HotFilesLoaded++
		warm.HotBytesLoaded += stats.BytesRead
		warm.Checksum ^= stats.Checksum + uint64(warm.HotFilesLoaded)
		warm.LoadedPaths = append(warm.LoadedPaths, path)
	}

	if plan.ExpectedHotBytes > 0 && warm.HotBytesLoaded != plan.ExpectedHotBytes {
		return nil, fmt.Errorf(
			"hot bytes mismatch: loaded=%d expected=%d",
			warm.HotBytesLoaded,
			plan.ExpectedHotBytes,
		)
	}

	warm.Ready = true
	warm.BootstrapDurationMs = time.Since(start).Milliseconds()

	return &AppState{
		paths: paths,
		plan:  plan,
		warm:  warm,
	}, nil
}

func (s *AppState) IsReady() bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.warm.Ready
}

func (s *AppState) Snapshot() WarmState {
	s.mu.RLock()
	defer s.mu.RUnlock()
	snapshot := s.warm
	snapshot.LoadedPaths = append([]string(nil), s.warm.LoadedPaths...)
	return snapshot
}

func (s *AppState) ReadLatePredictArtifact() error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if len(s.plan.LatePredictPaths) == 0 {
		return fmt.Errorf("no late predict paths configured")
	}
	_, err := ReadPath(s.plan.LatePredictPaths[0])
	return err
}
