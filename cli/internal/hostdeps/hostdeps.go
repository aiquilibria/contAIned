// Package hostdeps provides a uniform approach to detecting and installing
// host-side dependencies required by contained (docker, cosign, clipboard
// tools). Each dependency is represented by an Ensure* function in its own
// package; this package supplies the shared primitives they all use.
//
// # Auto-install policy
//
// On macOS, dependencies available in Homebrew are installed automatically
// when brew is present. On Linux, auto-install is not attempted (apt-get
// requires sudo and may not be available); a human-readable hint is returned
// instead. Docker is never auto-installed on any platform — it requires the
// user to start a daemon or Desktop app after installation.
//
// # Result strings
//
// Every Ensure* function returns a short status string suitable for display in
// the contained init result table:
//
//   - "ok"              – dependency is present and usable
//   - "installed"       – was missing, installed successfully via brew
//   - "install failed"  – brew returned a non-zero exit code
//   - "missing — …"     – not found, manual install instructions provided
//   - "n/a"             – platform does not support this dependency
package hostdeps

import (
	"fmt"
	"os"
	"os/exec"
)

// FindBrew returns the path to the Homebrew binary, or an empty string if
// Homebrew is not installed. It checks PATH first, then the two standard
// locations (Apple-Silicon and Intel).
func FindBrew() string {
	if p, err := exec.LookPath("brew"); err == nil {
		return p
	}
	for _, p := range []string{"/opt/homebrew/bin/brew", "/usr/local/bin/brew"} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

// BrewInstall ensures formula is installed via Homebrew. It prints an inline
// progress line to stdout (matching the style of other contained init output),
// then returns a status string.
//
// name is a human-readable label used in progress output (e.g. "cosign");
// formula is the Homebrew formula/cask to install (e.g. "cosign",
// "--cask docker").
func BrewInstall(name, formula string) string {
	brew := FindBrew()
	if brew == "" {
		return fmt.Sprintf("missing — brew install %s", formula)
	}
	fmt.Printf("  Installing %s via Homebrew …", name)
	args := append([]string{"install"}, formula)              //nolint:gocritic
	if err := exec.Command(brew, args...).Run(); err != nil { //nolint:gosec
		fmt.Println(" failed")
		return "install failed"
	}
	fmt.Println(" done")
	return "installed"
}

// AptHint returns a "missing — …" string suggesting the apt command to install
// pkg. No install is attempted (apt-get requires sudo and a display may not be
// available in all Linux environments).
func AptHint(pkg string) string {
	return fmt.Sprintf("missing — apt install %s", pkg)
}

// Missing returns a platform-neutral "missing — …" string with a custom hint.
// Use this for dependencies that cannot be auto-installed on any platform.
func Missing(hint string) string {
	return "missing — " + hint
}
