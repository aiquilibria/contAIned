package manifest

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── LoadRepoManifest ──────────────────────────────────────────────────────────

func TestLoadRepoManifest_Absent_ReturnsNil(t *testing.T) {
	dir := t.TempDir()
	r, err := LoadRepoManifest(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if r != nil {
		t.Fatalf("expected nil for absent file, got %+v", r)
	}
}

func TestLoadRepoManifest_ValidFields_Parsed(t *testing.T) {
	dir := writeRepoManifest(t, `
ecosystems:
  go: "1.22.5"
policy:
  qa:
    checks:
      - name: test
        command: [go, test, ./...]
`)
	r, err := LoadRepoManifest(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if r.Ecosystems["go"] != "1.22.5" {
		t.Errorf("ecosystem go: got %q", r.Ecosystems["go"])
	}
	if len(r.Policy.QA.Checks) != 1 || r.Policy.QA.Checks[0].Name != "test" {
		t.Errorf("qa checks: got %+v", r.Policy.QA.Checks)
	}
}

func TestLoadRepoManifest_DisallowedField_ReturnsError(t *testing.T) {
	dir := writeRepoManifest(t, `
runtime:
  docker:
    image: myimage:latest
`)
	_, err := LoadRepoManifest(dir)
	if err == nil || !strings.Contains(err.Error(), "disallowed fields") {
		t.Fatalf("expected disallowed fields error, got: %v", err)
	}
}

func TestLoadRepoManifest_DisallowedPolicyField_ReturnsError(t *testing.T) {
	dir := writeRepoManifest(t, `
policy:
  bash:
    rules:
      - name: no-rm
        patterns: ["rm -rf"]
        action: block
`)
	_, err := LoadRepoManifest(dir)
	if err == nil || !strings.Contains(err.Error(), "disallowed fields") {
		t.Fatalf("expected disallowed fields error, got: %v", err)
	}
}

func TestLoadRepoManifest_OldToolchainsFormat_ReturnsError(t *testing.T) {
	dir := writeRepoManifest(t, "runtime:\n  docker:\n    toolchains:\n      node: \"20\"\n")
	_, err := LoadRepoManifest(dir)
	if err == nil || !strings.Contains(err.Error(), "disallowed fields") {
		t.Fatalf("expected disallowed fields error for old toolchains format, got: %v", err)
	}
}

func TestLoadRepoManifest_OnlyEcosystems_Valid(t *testing.T) {
	dir := writeRepoManifest(t, "ecosystems:\n  node: \"20\"\n")
	r, err := LoadRepoManifest(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if r.Ecosystems["node"] != "20" {
		t.Errorf("ecosystem node: got %q", r.Ecosystems["node"])
	}
}

func TestLoadRepoManifest_OnlyQAChecks_Valid(t *testing.T) {
	dir := writeRepoManifest(t, "policy:\n  qa:\n    checks:\n      - name: lint\n        command: [ruff, check, .]\n")
	r, err := LoadRepoManifest(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(r.Policy.QA.Checks) != 1 {
		t.Fatalf("expected 1 check, got %d", len(r.Policy.QA.Checks))
	}
}

func TestLoadRepoManifest_Empty_Valid(t *testing.T) {
	dir := writeRepoManifest(t, "")
	r, err := LoadRepoManifest(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if r == nil {
		t.Fatal("expected non-nil result for empty file")
	}
}

// ── ValidateRepoManifest ──────────────────────────────────────────────────────

func TestValidateRepoManifest_Valid(t *testing.T) {
	r := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}
	r.Policy.QA.Checks = []QACheck{{Name: "test", Command: []string{"go", "test", "./..."}}}
	if err := ValidateRepoManifest(r); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestValidateRepoManifest_EmptyEcosystemName(t *testing.T) {
	r := &RepoManifest{
		Ecosystems: map[string]string{"": "1.22.5"},
	}
	err := ValidateRepoManifest(r)
	if err == nil || !strings.Contains(err.Error(), "must not be empty") {
		t.Fatalf("expected empty name error, got: %v", err)
	}
}

func TestValidateRepoManifest_QACheckMissingName(t *testing.T) {
	r := &RepoManifest{}
	r.Policy.QA.Checks = []QACheck{{Command: []string{"pytest"}}}
	err := ValidateRepoManifest(r)
	if err == nil || !strings.Contains(err.Error(), "name is required") {
		t.Fatalf("expected name error, got: %v", err)
	}
}

func TestValidateRepoManifest_QACheckMissingCommand(t *testing.T) {
	r := &RepoManifest{}
	r.Policy.QA.Checks = []QACheck{{Name: "test"}}
	err := ValidateRepoManifest(r)
	if err == nil || !strings.Contains(err.Error(), "command is required") {
		t.Fatalf("expected command error, got: %v", err)
	}
}

// ── MergeRepoManifest ─────────────────────────────────────────────────────────

func operatorWithEcosystems() *Manifest {
	m := validManifest()
	m.Policy.Network.Enabled = true
	m.EcosystemDefinitions = map[string]EcosystemDef{
		"go": {
			Toolchain:      "go",
			NetworkDomains: []string{"proxy.golang.org", "sum.golang.org"},
		},
		"python": {
			NetworkDomains: []string{"pypi.org", "files.pythonhosted.org"},
		},
		"typescript": {
			Toolchain:      "node",
			NetworkDomains: []string{"registry.npmjs.org"},
		},
		"node": {
			Toolchain:      "node",
			NetworkDomains: []string{"registry.npmjs.org"},
		},
	}
	return m
}

func TestMergeRepoManifest_NilRepo_ReturnsCopyOfOperator(t *testing.T) {
	m := validManifest()
	m.Policy.QA.Checks = []QACheck{{Name: "lint", Command: []string{"ruff", "check", "."}}}

	merged, err := MergeRepoManifest(m, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(merged.Policy.QA.Checks) != 1 {
		t.Errorf("expected 1 check, got %d", len(merged.Policy.QA.Checks))
	}
}

func TestMergeRepoManifest_EcosystemResolvesToToolchain(t *testing.T) {
	m := operatorWithEcosystems()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if merged.Runtime.Docker.Toolchains["go"] != "1.22.5" {
		t.Errorf("go toolchain: got %q", merged.Runtime.Docker.Toolchains["go"])
	}
}

func TestMergeRepoManifest_EcosystemAddsNetworkDomains(t *testing.T) {
	m := operatorWithEcosystems()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	domains := make(map[string]bool)
	for _, d := range merged.Policy.Network.AllowedDomains {
		domains[d] = true
	}
	if !domains["proxy.golang.org"] {
		t.Error("expected proxy.golang.org in allowed domains")
	}
	if !domains["sum.golang.org"] {
		t.Error("expected sum.golang.org in allowed domains")
	}
}

func TestMergeRepoManifest_TypescriptMapsToNodeToolchain(t *testing.T) {
	m := operatorWithEcosystems()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"typescript": "20"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if merged.Runtime.Docker.Toolchains["node"] != "20" {
		t.Errorf("node toolchain: got %q", merged.Runtime.Docker.Toolchains["node"])
	}
}

func TestMergeRepoManifest_PythonNoToolchain_DomainsAdded(t *testing.T) {
	m := operatorWithEcosystems()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"python": ""},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// No toolchain installed for python (pre-installed).
	if _, ok := merged.Runtime.Docker.Toolchains["python"]; ok {
		t.Error("python should not appear in toolchains (pre-installed)")
	}
	// But domains should be added.
	domains := make(map[string]bool)
	for _, d := range merged.Policy.Network.AllowedDomains {
		domains[d] = true
	}
	if !domains["pypi.org"] {
		t.Error("expected pypi.org in allowed domains")
	}
}

func TestMergeRepoManifest_UnknownEcosystem_ReturnsError(t *testing.T) {
	m := operatorWithEcosystems()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"rust": "1.80"},
	}

	_, err := MergeRepoManifest(m, repo)
	if err == nil || !strings.Contains(err.Error(), "not defined in the operator manifest") {
		t.Fatalf("expected unknown ecosystem error, got: %v", err)
	}
}

func TestMergeRepoManifest_RepoVersionSatisfiesFloorConstraint(t *testing.T) {
	m := operatorWithEcosystems()
	m.Runtime.Docker.Toolchains = map[string]string{"go": ">=1.21"}
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if merged.Runtime.Docker.Toolchains["go"] != "1.22.5" {
		t.Errorf("go toolchain: got %q", merged.Runtime.Docker.Toolchains["go"])
	}
}

func TestMergeRepoManifest_RepoVersionViolatesFloorConstraint(t *testing.T) {
	m := operatorWithEcosystems()
	m.Runtime.Docker.Toolchains = map[string]string{"go": ">=1.23"}
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}

	_, err := MergeRepoManifest(m, repo)
	if err == nil || !strings.Contains(err.Error(), "does not satisfy") {
		t.Fatalf("expected constraint violation error, got: %v", err)
	}
}

func TestMergeRepoManifest_RepoVersionMatchesExactConstraint(t *testing.T) {
	m := operatorWithEcosystems()
	m.Runtime.Docker.Toolchains = map[string]string{"go": "==1.22.5"}
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if merged.Runtime.Docker.Toolchains["go"] != "1.22.5" {
		t.Errorf("go toolchain: got %q", merged.Runtime.Docker.Toolchains["go"])
	}
}

func TestMergeRepoManifest_RepoVersionViolatesExactConstraint(t *testing.T) {
	m := operatorWithEcosystems()
	m.Runtime.Docker.Toolchains = map[string]string{"go": "==1.22.5"}
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.23.0"},
	}

	_, err := MergeRepoManifest(m, repo)
	if err == nil || !strings.Contains(err.Error(), "does not satisfy") {
		t.Fatalf("expected constraint violation error, got: %v", err)
	}
}

func TestMergeRepoManifest_QAChecks_Concatenated(t *testing.T) {
	m := validManifest()
	m.Policy.QA.Checks = []QACheck{{Name: "mAInlined-check", Command: []string{"echo", "ok"}}}

	repo := &RepoManifest{}
	repo.Policy.QA.Checks = []QACheck{
		{Name: "test", Command: []string{"go", "test", "./..."}},
		{Name: "lint", Command: []string{"golangci-lint", "run"}},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(merged.Policy.QA.Checks) != 3 {
		t.Fatalf("expected 3 checks, got %d: %+v", len(merged.Policy.QA.Checks), merged.Policy.QA.Checks)
	}
	if merged.Policy.QA.Checks[0].Name != "mAInlined-check" {
		t.Errorf("first check should be mAInlined-check, got %q", merged.Policy.QA.Checks[0].Name)
	}
}

func TestMergeRepoManifest_BothQAEmpty_Valid(t *testing.T) {
	m := validManifest()
	repo := &RepoManifest{}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(merged.Policy.QA.Checks) != 0 {
		t.Errorf("expected 0 checks, got %d", len(merged.Policy.QA.Checks))
	}
}

func TestMergeRepoManifest_DoesNotMutateOperator(t *testing.T) {
	m := operatorWithEcosystems()
	m.Policy.Network.AllowedDomains = []string{"api.anthropic.com"}
	origDomains := len(m.Policy.Network.AllowedDomains)

	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}

	if _, err := MergeRepoManifest(m, repo); err != nil {
		t.Fatal(err)
	}

	if len(m.Policy.Network.AllowedDomains) != origDomains {
		t.Error("MergeRepoManifest must not mutate the operator manifest's AllowedDomains")
	}
	if _, ok := m.Runtime.Docker.Toolchains["go"]; ok {
		t.Error("MergeRepoManifest must not mutate the operator manifest's Toolchains")
	}
}

func TestMergeRepoManifest_NoDuplicateDomains(t *testing.T) {
	m := operatorWithEcosystems()
	m.Policy.Network.AllowedDomains = []string{"api.anthropic.com", "proxy.golang.org"}
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	count := 0
	for _, d := range merged.Policy.Network.AllowedDomains {
		if d == "proxy.golang.org" {
			count++
		}
	}
	if count != 1 {
		t.Errorf("proxy.golang.org should appear exactly once, got %d", count)
	}
}

// ── satisfiesConstraint ───────────────────────────────────────────────────────

func TestSatisfiesConstraint_FloorConstraint(t *testing.T) {
	cases := []struct {
		constraint string
		version    string
		want       bool
	}{
		{">=3.13", "3.14", true},
		{">=3.13", "3.13", true},
		{">=3.13", "3.12", false},
		{">=3.13", "4.0", true},
		{">=1.22", "1.22.5", true},
		{">=1.23", "1.22.5", false},
	}
	for _, c := range cases {
		got, err := satisfiesConstraint(c.constraint, c.version)
		if err != nil {
			t.Errorf("satisfiesConstraint(%q, %q): unexpected error: %v", c.constraint, c.version, err)
			continue
		}
		if got != c.want {
			t.Errorf("satisfiesConstraint(%q, %q) = %v, want %v", c.constraint, c.version, got, c.want)
		}
	}
}

func TestSatisfiesConstraint_ExactConstraint(t *testing.T) {
	cases := []struct {
		constraint string
		version    string
		want       bool
	}{
		{"==3.13", "3.13", true},
		{"==3.13", "3.14", false},
		{"3.13", "3.13", true},  // bare treated as ==
		{"3.13", "3.14", false}, // bare treated as ==
	}
	for _, c := range cases {
		got, err := satisfiesConstraint(c.constraint, c.version)
		if err != nil {
			t.Errorf("satisfiesConstraint(%q, %q): unexpected error: %v", c.constraint, c.version, err)
			continue
		}
		if got != c.want {
			t.Errorf("satisfiesConstraint(%q, %q) = %v, want %v", c.constraint, c.version, got, c.want)
		}
	}
}

// ── helpers ───────────────────────────────────────────────────────────────────

func writeRepoManifest(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, ".contAIned_manifest.yaml"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}
