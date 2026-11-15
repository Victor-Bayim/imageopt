package bootstrap

import "path/filepath"

const (
	ReadBufferSize = 256 * 1024
)

type Paths struct {
	StartupPlanPath string
	DefaultConfig   string
	RoutesConfig    string
	RuntimePolicy   string
}

func DefaultPaths() Paths {
	return Paths{
		StartupPlanPath: "/srv/control/startup_plan.json",
		DefaultConfig:   "/app/config/default.yaml",
		RoutesConfig:    "/app/config/routes.json",
		RuntimePolicy:   "/app/config/runtime_policy.json",
	}
}

func TestPaths(root string) Paths {
	return Paths{
		StartupPlanPath: filepath.Join(root, "srv", "control", "startup_plan.json"),
		DefaultConfig:   filepath.Join(root, "app", "config", "default.yaml"),
		RoutesConfig:    filepath.Join(root, "app", "config", "routes.json"),
		RuntimePolicy:   filepath.Join(root, "app", "config", "runtime_policy.json"),
	}
}
