package docker

import (
	"runtime"

	"contained.dev/cli/internal/hostdeps"
)

// EnsureDocker checks whether the Docker binary is available on the host.
// Docker is not auto-installed — it requires the user to start a daemon or
// Desktop application after installation, which cannot be done automatically.
// A platform-appropriate installation hint is returned when Docker is missing.
//
// The returned string is a short status suitable for the contained init result
// table ("ok" or "missing — …").
func EnsureDocker() string {
	if _, err := FindDockerBin(); err == nil {
		return "ok"
	}
	switch runtime.GOOS {
	case "darwin":
		return hostdeps.Missing("brew install --cask docker  (then open Docker.app)")
	case "linux":
		return hostdeps.Missing("see https://docs.docker.com/engine/install/")
	default:
		return hostdeps.Missing("see https://docs.docker.com/get-started/get-docker/")
	}
}
