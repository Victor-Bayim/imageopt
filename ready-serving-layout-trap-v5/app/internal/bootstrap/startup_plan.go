package bootstrap

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type StartupPlan struct {
	HotPaths         []string `json:"hot_paths"`
	LatePredictPaths []string `json:"late_predict_paths"`
	ExpectedHotBytes int64    `json:"expected_hot_bytes"`
}

func LoadStartupPlan(path string) (StartupPlan, error) {
	payload, err := os.ReadFile(path)
	if err != nil {
		return StartupPlan{}, fmt.Errorf("read startup plan %s: %w", path, err)
	}
	var plan StartupPlan
	if err := json.Unmarshal(payload, &plan); err != nil {
		return StartupPlan{}, fmt.Errorf("decode startup plan %s: %w", path, err)
	}
	if err := plan.Validate(); err != nil {
		return StartupPlan{}, err
	}
	return plan, nil
}

func (p StartupPlan) Validate() error {
	if len(p.HotPaths) == 0 {
		return fmt.Errorf("startup plan hot_paths is empty")
	}
	if len(p.LatePredictPaths) == 0 {
		return fmt.Errorf("startup plan late_predict_paths is empty")
	}
	for _, group := range [][]string{p.HotPaths, p.LatePredictPaths} {
		for _, path := range group {
			if !filepath.IsAbs(path) {
				return fmt.Errorf("startup plan path must be absolute: %s", path)
			}
		}
	}
	return nil
}
