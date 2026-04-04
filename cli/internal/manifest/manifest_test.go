package manifest

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── Parse ─────────────────────────────────────────────────────────────────────

func TestParse_MinimalYAML_AppliesDefaults(t *testing.T) {
	yaml := `
init:
  container:
    image: myimage:latest
    network: mynet
`
	m, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Init.Container.Image != "myimage:latest" {
		t.Errorf("image: got %q, want %q", m.Init.Container.Image, "myimage:latest")
	}
	if m.Init.Container.Memory != "2g" {
		t.Errorf("memory default: got %q, want %q", m.Init.Container.Memory, "2g")
	}
	if m.Init.Container.CPUs != 2 {
		t.Errorf("cpus default: got %d, want 2", m.Init.Container.CPUs)
	}
	if m.Init.Container.AgentConfigVolume != "contAIned-agent-config" {
		t.Errorf("agent_config_volume default: got %q", m.Init.Container.AgentConfigVolume)
	}
	if m.Init.Sigstore.RekorURL != "https://rekor.sigstore.dev" {
		t.Errorf("rekor_url default: got %q", m.Init.Sigstore.RekorURL)
	}
	if m.Init.Sigstore.FulcioURL != "https://fulcio.sigstore.dev" {
		t.Errorf("fulcio_url default: got %q", m.Init.Sigstore.FulcioURL)
	}
}

func TestParse_ExplicitValues_NotOverriddenByDefaults(t *testing.T) {
	yaml := `
init:
  container:
    image: custom:v2
    memory: 4g
    cpus: 4
    network: mynet
    agent_config_volume: my-vol
  sigstore:
    rekor_url: https://custom.rekor.example
    fulcio_url: https://custom.fulcio.example
`
	m, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Init.Container.Memory != "4g" {
		t.Errorf("memory: got %q, want 4g", m.Init.Container.Memory)
	}
	if m.Init.Container.CPUs != 4 {
		t.Errorf("cpus: got %d, want 4", m.Init.Container.CPUs)
	}
	if m.Init.Container.AgentConfigVolume != "my-vol" {
		t.Errorf("agent_config_volume: got %q", m.Init.Container.AgentConfigVolume)
	}
	if m.Init.Sigstore.RekorURL != "https://custom.rekor.example" {
		t.Errorf("rekor_url: got %q", m.Init.Sigstore.RekorURL)
	}
}

func TestParse_EmptyYAML_AllDefaults(t *testing.T) {
	m, err := Parse([]byte(""))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Init.Container.Image != "contained:latest" {
		t.Errorf("image default: got %q", m.Init.Container.Image)
	}
	if m.Init.Container.Network != "contAIned-net" {
		t.Errorf("network default: got %q", m.Init.Container.Network)
	}
}

func TestParse_InvalidYAML_ReturnsError(t *testing.T) {
	_, err := Parse([]byte("runtime: [invalid: yaml: :::"))
	if err == nil {
		t.Fatal("expected error for invalid YAML, got nil")
	}
}

func TestParse_QAChecks(t *testing.T) {
	yaml := `
init:
  container:
    image: x
    network: n
runtime:
  qa:
    checks:
      - name: lint
        command: [ruff, check, .]
        when_changed: ["*.py"]
`
	m, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(m.Runtime.QA.Checks) != 1 {
		t.Fatalf("checks: got %d, want 1", len(m.Runtime.QA.Checks))
	}
	c := m.Runtime.QA.Checks[0]
	if c.Name != "lint" {
		t.Errorf("check name: got %q", c.Name)
	}
	if len(c.Command) != 3 || c.Command[0] != "ruff" {
		t.Errorf("check command: got %v", c.Command)
	}
	if len(c.WhenChanged) != 1 || c.WhenChanged[0] != "*.py" {
		t.Errorf("when_changed: got %v", c.WhenChanged)
	}
}

// ── Validate ──────────────────────────────────────────────────────────────────

func validManifest() *Manifest {
	return &Manifest{
		Init: InitConfig{
			Container: ContainerConfig{
				Image:   "myimage:latest",
				Network: "mynet",
			},
		},
	}
}

func TestValidate_ValidManifest(t *testing.T) {
	if err := Validate(validManifest()); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestValidate_MissingImage(t *testing.T) {
	m := validManifest()
	m.Init.Container.Image = ""
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "init.container.image") {
		t.Fatalf("expected image error, got: %v", err)
	}
}

func TestValidate_MissingNetwork(t *testing.T) {
	m := validManifest()
	m.Init.Container.Network = ""
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "init.container.network") {
		t.Fatalf("expected network error, got: %v", err)
	}
}

func TestValidate_Sigstore_EnabledWithoutURLs(t *testing.T) {
	m := validManifest()
	m.Init.Sigstore.Enabled = true
	m.Init.Sigstore.RekorURL = ""
	m.Init.Sigstore.FulcioURL = ""
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "rekor_url") {
		t.Fatalf("expected rekor_url error, got: %v", err)
	}
}

func TestValidate_Sigstore_EnabledFulcioMissing(t *testing.T) {
	m := validManifest()
	m.Init.Sigstore.Enabled = true
	m.Init.Sigstore.RekorURL = "https://rekor.sigstore.dev"
	m.Init.Sigstore.FulcioURL = ""
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "fulcio_url") {
		t.Fatalf("expected fulcio_url error, got: %v", err)
	}
}

func TestValidate_Sigstore_DisabledURLsNotRequired(t *testing.T) {
	m := validManifest()
	m.Init.Sigstore.Enabled = false
	m.Init.Sigstore.RekorURL = ""
	m.Init.Sigstore.FulcioURL = ""
	if err := Validate(m); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestValidate_QACheck_MissingName(t *testing.T) {
	m := validManifest()
	m.Runtime.QA.Checks = []QACheck{{Command: []string{"pytest"}}}
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "checks[0].name") {
		t.Fatalf("expected QA name error, got: %v", err)
	}
}

func TestValidate_QACheck_MissingCommand(t *testing.T) {
	m := validManifest()
	m.Runtime.QA.Checks = []QACheck{{Name: "tests"}}
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "checks[0].command") {
		t.Fatalf("expected QA command error, got: %v", err)
	}
}

// ── Load ──────────────────────────────────────────────────────────────────────

func TestLoad_NewPath(t *testing.T) {
	dir := t.TempDir()
	contained := filepath.Join(dir, ".contAIned")
	if err := os.MkdirAll(contained, 0o755); err != nil {
		t.Fatal(err)
	}
	content := "init:\n  container:\n    image: test:v1\n    network: testnet\n"
	if err := os.WriteFile(filepath.Join(contained, "manifest.yaml"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	m, err := Load(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Init.Container.Image != "test:v1" {
		t.Errorf("image: got %q", m.Init.Container.Image)
	}
}

func TestLoad_LegacyPath(t *testing.T) {
	dir := t.TempDir()
	legacyDir := filepath.Join(dir, ".contAIned", "policy")
	if err := os.MkdirAll(legacyDir, 0o755); err != nil {
		t.Fatal(err)
	}
	content := "init:\n  container:\n    image: legacy:v1\n    network: testnet\n"
	if err := os.WriteFile(filepath.Join(legacyDir, "manifest.yaml"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	m, err := Load(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Init.Container.Image != "legacy:v1" {
		t.Errorf("image: got %q", m.Init.Container.Image)
	}
}

func TestLoad_MissingFile_ReturnsError(t *testing.T) {
	dir := t.TempDir()
	_, err := Load(dir)
	if err == nil || !strings.Contains(err.Error(), "no manifest found") {
		t.Fatalf("expected 'no manifest found' error, got: %v", err)
	}
}

// ── Validate: toolchains ──────────────────────────────────────────────────────

func TestValidate_SupportedToolchains_Valid(t *testing.T) {
	for _, name := range []string{"go", "node", "ruby", "java"} {
		m := validManifest()
		m.Init.Container.Toolchains = map[string]string{name: "1.0"}
		if err := Validate(m); err != nil {
			t.Errorf("toolchain %q: unexpected error: %v", name, err)
		}
	}
}

func TestValidate_UnsupportedToolchain_ReturnsError(t *testing.T) {
	m := validManifest()
	m.Init.Container.Toolchains = map[string]string{"rust": "1.80"}
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "unsupported toolchain") {
		t.Fatalf("expected unsupported toolchain error, got: %v", err)
	}
}

func TestValidate_ToolchainEmptyVersion_ReturnsError(t *testing.T) {
	m := validManifest()
	m.Init.Container.Toolchains = map[string]string{"go": ""}
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "must not be empty") {
		t.Fatalf("expected empty version error, got: %v", err)
	}
}

// ── policy.plugins ────────────────────────────────────────────────────────────

func TestParse_Plugins_BuiltinMarketplaceDefaultsTrue(t *testing.T) {
	// When builtin_marketplace is omitted, applyDefaults sets it to true.
	yaml := `
init:
  container:
    image: x
    network: n
`
	m, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Init.Plugins.BuiltinMarketplace == nil {
		t.Fatal("BuiltinMarketplace should not be nil after applyDefaults")
	}
	if !*m.Init.Plugins.BuiltinMarketplace {
		t.Error("BuiltinMarketplace default: want true, got false")
	}
}

func TestParse_Plugins_BuiltinMarketplaceExplicitFalse(t *testing.T) {
	yaml := `
init:
  container:
    image: x
    network: n
  plugins:
    builtin_marketplace: false
`
	m, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Init.Plugins.BuiltinMarketplace == nil {
		t.Fatal("BuiltinMarketplace should not be nil")
	}
	if *m.Init.Plugins.BuiltinMarketplace {
		t.Error("BuiltinMarketplace explicit false: want false, got true")
	}
}

func TestParse_Plugins_StrictMarketplacesFalseIsZeroValue(t *testing.T) {
	// Omitting strict_marketplaces should leave it as false (zero value).
	yaml := `
init:
  container:
    image: x
    network: n
`
	m, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Init.Plugins.StrictMarketplaces {
		t.Error("StrictMarketplaces should be false when omitted")
	}
}

func TestParse_Plugins_RoundTrip(t *testing.T) {
	bFalse := false
	original := validManifest()
	original.Init.Plugins = PluginsConfig{
		StrictMarketplaces: true,
		BuiltinMarketplace: &bFalse,
		ExtraMarketplaces: []PluginMarketplace{
			{Source: "github", Repo: "acme/plugins", Ref: "main"},
			{Source: "npm", Package: "@acme/plugins"},
		},
		Preinstall: []PluginRef{
			{Marketplace: "acme-plugins", Plugin: "eslint-plugin"},
		},
	}

	out, err := Serialise(original)
	if err != nil {
		t.Fatalf("serialise error: %v", err)
	}

	reparsed, err := Parse([]byte(out))
	if err != nil {
		t.Fatalf("reparse error: %v", err)
	}

	p := reparsed.Init.Plugins
	if !p.StrictMarketplaces {
		t.Error("StrictMarketplaces round-trip: want true")
	}
	if p.BuiltinMarketplace == nil || *p.BuiltinMarketplace {
		t.Error("BuiltinMarketplace round-trip: want false")
	}
	if len(p.ExtraMarketplaces) != 2 {
		t.Fatalf("ExtraMarketplaces round-trip: got %d, want 2", len(p.ExtraMarketplaces))
	}
	if p.ExtraMarketplaces[0].Repo != "acme/plugins" {
		t.Errorf("ExtraMarketplaces[0].Repo: got %q", p.ExtraMarketplaces[0].Repo)
	}
	if p.ExtraMarketplaces[1].Package != "@acme/plugins" {
		t.Errorf("ExtraMarketplaces[1].Package: got %q", p.ExtraMarketplaces[1].Package)
	}
	if len(p.Preinstall) != 1 || p.Preinstall[0].Plugin != "eslint-plugin" {
		t.Errorf("Preinstall round-trip: got %v", p.Preinstall)
	}
}

func TestParse_Plugins_EmptySectionParsesWithoutError(t *testing.T) {
	yaml := `
init:
  container:
    image: x
    network: n
  plugins: {}
`
	m, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Init.Plugins.StrictMarketplaces {
		t.Error("StrictMarketplaces should be false for empty section")
	}
	if len(m.Init.Plugins.ExtraMarketplaces) != 0 {
		t.Error("ExtraMarketplaces should be empty")
	}
	if len(m.Init.Plugins.Preinstall) != 0 {
		t.Error("Preinstall should be empty")
	}
}

func TestParse_EcosystemDef_Plugins_RoundTrip(t *testing.T) {
	yaml := `
init:
  container:
    image: x
    network: n
ecosystems:
  python:
    plugins:
      - marketplace: claude-plugins-official
        plugin: python-lsp
`
	m, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	def, ok := m.Ecosystems["python"]
	if !ok {
		t.Fatal("python ecosystem definition not found")
	}
	if len(def.Plugins) != 1 {
		t.Fatalf("ecosystem plugins: got %d, want 1", len(def.Plugins))
	}
	if def.Plugins[0].Marketplace != "claude-plugins-official" {
		t.Errorf("marketplace: got %q", def.Plugins[0].Marketplace)
	}
	if def.Plugins[0].Plugin != "python-lsp" {
		t.Errorf("plugin: got %q", def.Plugins[0].Plugin)
	}
}

// ── Serialise ─────────────────────────────────────────────────────────────────

func TestSerialise_RoundTrip(t *testing.T) {
	original := validManifest()
	original.Init.Agent.Model = "claude-sonnet-4-6"
	original.Runtime.QA.Checks = []QACheck{{Name: "lint", Command: []string{"ruff", "check", "."}}}

	out, err := Serialise(original)
	if err != nil {
		t.Fatalf("serialise error: %v", err)
	}

	reparsed, err := Parse([]byte(out))
	if err != nil {
		t.Fatalf("reparse error: %v", err)
	}
	if reparsed.Init.Agent.Model != "claude-sonnet-4-6" {
		t.Errorf("model round-trip: got %q", reparsed.Init.Agent.Model)
	}
	if len(reparsed.Runtime.QA.Checks) != 1 || reparsed.Runtime.QA.Checks[0].Name != "lint" {
		t.Errorf("qa.checks round-trip: got %v", reparsed.Runtime.QA.Checks)
	}
}
