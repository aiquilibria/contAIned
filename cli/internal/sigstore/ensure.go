package sigstore

import (
	"runtime"

	"contained.dev/cli/internal/hostdeps"
)

// EnsureCosign checks whether cosign is available on the host and, on macOS,
// installs it via Homebrew if it is missing. On Linux a manual-install hint is
// returned. cosign is only required when Sigstore image signing is enabled
// (policy.sigstore.enabled: true), but checking for it at contained init time
// lets operators resolve the missing dependency before they first need it.
//
// The returned string is a short status suitable for the contained init result
// table ("ok", "installed", "install failed", or "missing — …").
func EnsureCosign() string {
	if _, err := FindCosign(); err == nil {
		return "ok"
	}
	switch runtime.GOOS {
	case "darwin":
		return hostdeps.BrewInstall("cosign", "cosign")
	case "linux":
		return hostdeps.AptHint("cosign")
	default:
		return hostdeps.Missing("see https://docs.sigstore.dev/cosign/system_config/installation/")
	}
}
