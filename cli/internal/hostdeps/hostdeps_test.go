package hostdeps

import (
	"strings"
	"testing"
)

func TestFindBrew(t *testing.T) {
	// FindBrew returns a string (may be empty on non-macOS CI); never panics.
	_ = FindBrew()
}

func TestBrewInstallNoBrew(t *testing.T) {
	// When brew is not on PATH and not in standard locations this host likely
	// has no Homebrew. BrewInstall must return a "missing" hint, not panic.
	// We cannot guarantee brew is absent on all CI hosts, so we only assert
	// on the shape of the result.
	got := BrewInstall("testpkg", "testpkg")
	if got == "" {
		t.Error("expected non-empty status from BrewInstall")
	}
	// Result must be one of the known status strings.
	valid := []string{"installed", "install failed", "missing —"}
	for _, v := range valid {
		if strings.HasPrefix(got, v) {
			return
		}
	}
	t.Errorf("unexpected status %q; want one of %v", got, valid)
}

func TestAptHint(t *testing.T) {
	got := AptHint("xclip")
	if !strings.Contains(got, "xclip") {
		t.Errorf("AptHint(%q) = %q; want it to mention the package", "xclip", got)
	}
	if !strings.HasPrefix(got, "missing") {
		t.Errorf("AptHint result should start with 'missing', got %q", got)
	}
}

func TestMissing(t *testing.T) {
	got := Missing("install Docker Desktop")
	if !strings.HasPrefix(got, "missing — ") {
		t.Errorf("Missing() = %q; want prefix 'missing — '", got)
	}
}
