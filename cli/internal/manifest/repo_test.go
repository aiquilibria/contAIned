package manifest

import (
	"encoding/base64"
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
			Deps:           []string{"golangci-lint"},
		},
		"python": {
			NetworkDomains: []string{"pypi.org", "files.pythonhosted.org"},
		},
		"typescript": {
			Toolchain:      "node",
			NetworkDomains: []string{"registry.npmjs.org"},
			Install:        []string{"npm", "install"},
		},
		"node": {
			Toolchain:      "node",
			NetworkDomains: []string{"registry.npmjs.org"},
			Install:        []string{"npm", "install"},
		},
	}
	return m
}

func TestMergeRepoManifest_NilRepo_InstallsMinVersionFromFloorConstraint(t *testing.T) {
	m := operatorWithEcosystems()
	m.Runtime.Docker.Toolchains = map[string]string{"go": ">=1.22", "node": ">=18"}

	merged, err := MergeRepoManifest(m, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if merged.Runtime.Docker.Toolchains["go"] != "1.22" {
		t.Errorf("go toolchain: got %q, want %q", merged.Runtime.Docker.Toolchains["go"], "1.22")
	}
	if merged.Runtime.Docker.Toolchains["node"] != "18" {
		t.Errorf("node toolchain: got %q, want %q", merged.Runtime.Docker.Toolchains["node"], "18")
	}
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

func TestMergeRepoManifest_EcosystemDeps_Collected(t *testing.T) {
	m := operatorWithEcosystems()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	found := false
	for _, dep := range merged.Runtime.Docker.Deps {
		if dep == "golangci-lint" {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected golangci-lint in merged deps")
	}
}

func TestMergeRepoManifest_NoDuplicateDeps(t *testing.T) {
	m := operatorWithEcosystems()
	// Add a second ecosystem that also declares golangci-lint.
	m.EcosystemDefinitions["go2"] = EcosystemDef{
		Toolchain: "go",
		Deps:      []string{"golangci-lint"},
	}
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5", "go2": "1.22.5"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	count := 0
	for _, dep := range merged.Runtime.Docker.Deps {
		if dep == "golangci-lint" {
			count++
		}
	}
	if count != 1 {
		t.Errorf("golangci-lint should appear exactly once in deps, got %d", count)
	}
}

func TestMergeRepoManifest_EcosystemInstall_CollectedIntoSetup(t *testing.T) {
	m := operatorWithEcosystems()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"typescript": "20"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(merged.Policy.QA.Setup) == 0 {
		t.Fatal("expected setup commands from typescript ecosystem, got none")
	}
	found := false
	for _, cmd := range merged.Policy.QA.Setup {
		if len(cmd) == 2 && cmd[0] == "npm" && cmd[1] == "install" {
			found = true
			break
		}
	}
	if !found {
		t.Errorf("expected [npm install] in qa.setup, got %v", merged.Policy.QA.Setup)
	}
}

func TestMergeRepoManifest_NoDuplicateSetupCommands(t *testing.T) {
	m := operatorWithEcosystems()
	// Both typescript and node declare [npm install] — should appear only once.
	repo := &RepoManifest{
		Ecosystems: map[string]string{"typescript": "20", "node": "20"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	count := 0
	for _, cmd := range merged.Policy.QA.Setup {
		if len(cmd) == 2 && cmd[0] == "npm" && cmd[1] == "install" {
			count++
		}
	}
	if count != 1 {
		t.Errorf("[npm install] should appear exactly once in setup, got %d", count)
	}
}

func TestMergeRepoManifest_NoInstall_SetupEmpty(t *testing.T) {
	m := operatorWithEcosystems()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}

	merged, err := MergeRepoManifest(m, repo)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, cmd := range merged.Policy.QA.Setup {
		if len(cmd) == 2 && cmd[0] == "npm" && cmd[1] == "install" {
			t.Error("npm install should not appear in setup when only go ecosystem is active")
		}
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

// ── CollectPlugins ────────────────────────────────────────────────────────────

func operatorWithPlugins() *Manifest {
	m := operatorWithEcosystems()
	m.Policy.Plugins.Preinstall = []PluginRef{
		{Marketplace: "claude-plugins-official", Plugin: "global-plugin"},
	}
	m.EcosystemDefinitions["go"] = EcosystemDef{
		Toolchain:      "go",
		NetworkDomains: []string{"proxy.golang.org"},
		Plugins: []PluginRef{
			{Marketplace: "claude-plugins-official", Plugin: "go-lsp"},
		},
	}
	m.EcosystemDefinitions["python"] = EcosystemDef{
		NetworkDomains: []string{"pypi.org"},
		Plugins: []PluginRef{
			{Marketplace: "claude-plugins-official", Plugin: "python-lsp"},
		},
	}
	return m
}

func TestCollectPlugins_NilRepo_OnlyPreinstall(t *testing.T) {
	m := operatorWithPlugins()
	plugins := CollectPlugins(m, nil)
	if len(plugins) != 1 {
		t.Fatalf("expected 1 plugin, got %d: %v", len(plugins), plugins)
	}
	if plugins[0].Plugin != "global-plugin" {
		t.Errorf("unexpected plugin: %v", plugins[0])
	}
}

func TestCollectPlugins_ActiveEcosystem_CollectsEcosystemPlugins(t *testing.T) {
	m := operatorWithPlugins()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}
	plugins := CollectPlugins(m, repo)
	// expect: global-plugin + go-lsp (order: preinstall first, then ecosystem)
	if len(plugins) != 2 {
		t.Fatalf("expected 2 plugins, got %d: %v", len(plugins), plugins)
	}
	names := map[string]bool{}
	for _, p := range plugins {
		names[p.Plugin] = true
	}
	if !names["global-plugin"] {
		t.Error("expected global-plugin")
	}
	if !names["go-lsp"] {
		t.Error("expected go-lsp")
	}
}

func TestCollectPlugins_MultipleEcosystems_AllCollected(t *testing.T) {
	m := operatorWithPlugins()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5", "python": ""},
	}
	plugins := CollectPlugins(m, repo)
	if len(plugins) != 3 {
		t.Fatalf("expected 3 plugins, got %d: %v", len(plugins), plugins)
	}
}

func TestCollectPlugins_Deduplicated(t *testing.T) {
	m := operatorWithPlugins()
	// Add go-lsp to preinstall as well — should appear only once.
	m.Policy.Plugins.Preinstall = append(m.Policy.Plugins.Preinstall,
		PluginRef{Marketplace: "claude-plugins-official", Plugin: "go-lsp"},
	)
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}
	plugins := CollectPlugins(m, repo)
	count := 0
	for _, p := range plugins {
		if p.Plugin == "go-lsp" {
			count++
		}
	}
	if count != 1 {
		t.Errorf("go-lsp should appear exactly once, got %d", count)
	}
}

func TestCollectPlugins_NoPlugins_ReturnsNil(t *testing.T) {
	m := validManifest()
	plugins := CollectPlugins(m, nil)
	if len(plugins) != 0 {
		t.Errorf("expected empty, got %v", plugins)
	}
}

func TestCollectPlugins_PreinstallOrderFirst(t *testing.T) {
	m := operatorWithPlugins()
	repo := &RepoManifest{
		Ecosystems: map[string]string{"go": "1.22.5"},
	}
	plugins := CollectPlugins(m, repo)
	// preinstall plugin must come before ecosystem plugin
	if plugins[0].Plugin != "global-plugin" {
		t.Errorf("expected global-plugin first, got %q", plugins[0].Plugin)
	}
}

// ── EncodePluginsArg ──────────────────────────────────────────────────────────

func TestEncodePluginsArg_Empty_ReturnsEmpty(t *testing.T) {
	if got := EncodePluginsArg(nil); got != "" {
		t.Errorf("expected empty string, got %q", got)
	}
	if got := EncodePluginsArg([]PluginRef{}); got != "" {
		t.Errorf("expected empty string, got %q", got)
	}
}

func TestEncodePluginsArg_SinglePlugin_Encoded(t *testing.T) {
	plugins := []PluginRef{
		{Marketplace: "claude-plugins-official", Plugin: "go-lsp"},
	}
	got := EncodePluginsArg(plugins)
	if got == "" {
		t.Fatal("expected non-empty base64 string")
	}
	decoded, err := base64.StdEncoding.DecodeString(got)
	if err != nil {
		t.Fatalf("base64 decode error: %v", err)
	}
	if string(decoded) != "claude-plugins-official:go-lsp" {
		t.Errorf("decoded: %q", string(decoded))
	}
}

func TestEncodePluginsArg_MultiplePlugins_NewlineDelimited(t *testing.T) {
	plugins := []PluginRef{
		{Marketplace: "mp1", Plugin: "plugin-a"},
		{Marketplace: "mp2", Plugin: "plugin-b"},
	}
	got := EncodePluginsArg(plugins)
	decoded, err := base64.StdEncoding.DecodeString(got)
	if err != nil {
		t.Fatalf("base64 decode error: %v", err)
	}
	want := "mp1:plugin-a\nmp2:plugin-b"
	if string(decoded) != want {
		t.Errorf("decoded: got %q, want %q", string(decoded), want)
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

// ── CollectMarketplaceClones / EncodeMarketplaceClonesArg ─────────────────────

func boolPtr(b bool) *bool { return &b }

func baseOperator() *Manifest {
	return &Manifest{}
}

func TestCollectMarketplaceClones_NoPlugins_Empty(t *testing.T) {
	clones := CollectMarketplaceClones(baseOperator(), nil)
	if len(clones) != 0 {
		t.Errorf("expected empty, got %v", clones)
	}
}

func TestCollectMarketplaceClones_BuiltinTrue_PluginUsesIt(t *testing.T) {
	m := baseOperator()
	m.Policy.Plugins.BuiltinMarketplace = boolPtr(true)
	plugins := []PluginRef{{Marketplace: "claude-plugins-official", Plugin: "github"}}
	clones := CollectMarketplaceClones(m, plugins)
	if len(clones) != 1 {
		t.Fatalf("expected 1, got %v", clones)
	}
	if clones[0].Name != "claude-plugins-official" || clones[0].Repo != "anthropics/claude-plugins-official" {
		t.Errorf("unexpected clone: %+v", clones[0])
	}
}

func TestCollectMarketplaceClones_BuiltinTrue_NoPluginUsesIt_Empty(t *testing.T) {
	m := baseOperator()
	m.Policy.Plugins.BuiltinMarketplace = boolPtr(true)
	plugins := []PluginRef{{Marketplace: "other-mp", Plugin: "tool"}}
	clones := CollectMarketplaceClones(m, plugins)
	if len(clones) != 0 {
		t.Errorf("expected empty (no plugin uses claude-plugins-official), got %v", clones)
	}
}

func TestCollectMarketplaceClones_BuiltinFalse_Empty(t *testing.T) {
	m := baseOperator()
	m.Policy.Plugins.BuiltinMarketplace = boolPtr(false)
	plugins := []PluginRef{{Marketplace: "claude-plugins-official", Plugin: "github"}}
	clones := CollectMarketplaceClones(m, plugins)
	if len(clones) != 0 {
		t.Errorf("expected empty when builtin_marketplace false, got %v", clones)
	}
}

func TestCollectMarketplaceClones_ExtraGithubMarketplace_Included(t *testing.T) {
	m := baseOperator()
	m.Policy.Plugins.ExtraMarketplaces = []PluginMarketplace{
		{Source: "github", Repo: "acme-corp/plugins"},
	}
	plugins := []PluginRef{{Marketplace: "acme-corp-plugins", Plugin: "mytool"}}
	clones := CollectMarketplaceClones(m, plugins)
	if len(clones) != 1 {
		t.Fatalf("expected 1, got %v", clones)
	}
	if clones[0].Name != "acme-corp-plugins" || clones[0].Repo != "acme-corp/plugins" {
		t.Errorf("unexpected clone: %+v", clones[0])
	}
}

func TestCollectMarketplaceClones_ExtraNonGithub_Excluded(t *testing.T) {
	m := baseOperator()
	m.Policy.Plugins.ExtraMarketplaces = []PluginMarketplace{
		{Source: "npm", Package: "@acme/plugins"},
	}
	plugins := []PluginRef{{Marketplace: "acme-plugins", Plugin: "mytool"}}
	clones := CollectMarketplaceClones(m, plugins)
	if len(clones) != 0 {
		t.Errorf("expected empty for non-github source, got %v", clones)
	}
}

func TestEncodeMarketplaceClonesArg_Empty_ReturnsEmpty(t *testing.T) {
	if got := EncodeMarketplaceClonesArg(nil); got != "" {
		t.Errorf("expected empty string, got %q", got)
	}
}

func TestEncodeMarketplaceClonesArg_Roundtrip(t *testing.T) {
	clones := []MarketplaceClone{
		{Name: "claude-plugins-official", Repo: "anthropics/claude-plugins-official"},
	}
	encoded := EncodeMarketplaceClonesArg(clones)
	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatalf("decode error: %v", err)
	}
	if string(decoded) != "claude-plugins-official:anthropics/claude-plugins-official\n" {
		t.Errorf("unexpected decoded: %q", string(decoded))
	}
}

// ── EncodeNetrcFromSecretsArg ─────────────────────────────────────────────────

func TestEncodeNetrcFromSecretsArg_Empty_ReturnsEmpty(t *testing.T) {
	if got := EncodeNetrcFromSecretsArg(nil); got != "" {
		t.Errorf("expected empty string, got %q", got)
	}
}

func TestEncodeNetrcFromSecretsArg_NoNetrcMachine_ReturnsEmpty(t *testing.T) {
	secrets := []ExtraSecret{
		{Path: "~/.contained/secrets/foo", Env: "FOO_TOKEN"},
	}
	if got := EncodeNetrcFromSecretsArg(secrets); got != "" {
		t.Errorf("expected empty string when no netrc_machine, got %q", got)
	}
}

func TestEncodeNetrcFromSecretsArg_WithNetrcMachine_Roundtrip(t *testing.T) {
	secrets := []ExtraSecret{
		{Path: "~/.contained/secrets/github_token", Env: "GITHUB_PERSONAL_ACCESS_TOKEN", NetrcMachine: "github.com"},
	}
	encoded := EncodeNetrcFromSecretsArg(secrets)
	if encoded == "" {
		t.Fatal("expected non-empty encoded string")
	}
	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatalf("decode error: %v", err)
	}
	want := "github.com:GITHUB_PERSONAL_ACCESS_TOKEN\n"
	if string(decoded) != want {
		t.Errorf("unexpected decoded: %q (want %q)", string(decoded), want)
	}
}

func TestEncodeNetrcFromSecretsArg_MultipleSecrets_OnlyNetrcMachineIncluded(t *testing.T) {
	secrets := []ExtraSecret{
		{Path: "~/.contained/secrets/github_token", Env: "GITHUB_TOKEN", NetrcMachine: "github.com"},
		{Path: "~/.contained/secrets/other", Env: "OTHER_TOKEN"},
		{Path: "~/.contained/secrets/gitlab_token", Env: "GITLAB_TOKEN", NetrcMachine: "gitlab.com"},
	}
	encoded := EncodeNetrcFromSecretsArg(secrets)
	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatalf("decode error: %v", err)
	}
	lines := strings.Split(strings.TrimSuffix(string(decoded), "\n"), "\n")
	if len(lines) != 2 {
		t.Fatalf("expected 2 lines, got %d: %v", len(lines), lines)
	}
	if lines[0] != "github.com:GITHUB_TOKEN" {
		t.Errorf("line 0: got %q", lines[0])
	}
	if lines[1] != "gitlab.com:GITLAB_TOKEN" {
		t.Errorf("line 1: got %q", lines[1])
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
