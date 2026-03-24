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
runtime:
  docker:
    image: myimage:latest
    network: mynet
`
	m, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Runtime.Docker.Image != "myimage:latest" {
		t.Errorf("image: got %q, want %q", m.Runtime.Docker.Image, "myimage:latest")
	}
	if m.Runtime.Docker.Memory != "2g" {
		t.Errorf("memory default: got %q, want %q", m.Runtime.Docker.Memory, "2g")
	}
	if m.Runtime.Docker.CPUs != 2 {
		t.Errorf("cpus default: got %d, want 2", m.Runtime.Docker.CPUs)
	}
	if m.Runtime.Docker.AgentConfigVolume != "contAIned-agent-config" {
		t.Errorf("agent_config_volume default: got %q", m.Runtime.Docker.AgentConfigVolume)
	}
	if m.Policy.Sigstore.RekorURL != "https://rekor.sigstore.dev" {
		t.Errorf("rekor_url default: got %q", m.Policy.Sigstore.RekorURL)
	}
	if m.Policy.Sigstore.FulcioURL != "https://fulcio.sigstore.dev" {
		t.Errorf("fulcio_url default: got %q", m.Policy.Sigstore.FulcioURL)
	}
}

func TestParse_ExplicitValues_NotOverriddenByDefaults(t *testing.T) {
	yaml := `
runtime:
  docker:
    image: custom:v2
    memory: 4g
    cpus: 4
    network: mynet
    agent_config_volume: my-vol
policy:
  sigstore:
    rekor_url: https://custom.rekor.example
    fulcio_url: https://custom.fulcio.example
`
	m, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Runtime.Docker.Memory != "4g" {
		t.Errorf("memory: got %q, want 4g", m.Runtime.Docker.Memory)
	}
	if m.Runtime.Docker.CPUs != 4 {
		t.Errorf("cpus: got %d, want 4", m.Runtime.Docker.CPUs)
	}
	if m.Runtime.Docker.AgentConfigVolume != "my-vol" {
		t.Errorf("agent_config_volume: got %q", m.Runtime.Docker.AgentConfigVolume)
	}
	if m.Policy.Sigstore.RekorURL != "https://custom.rekor.example" {
		t.Errorf("rekor_url: got %q", m.Policy.Sigstore.RekorURL)
	}
}

func TestParse_EmptyYAML_AllDefaults(t *testing.T) {
	m, err := Parse([]byte(""))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Runtime.Docker.Image != "contained:latest" {
		t.Errorf("image default: got %q", m.Runtime.Docker.Image)
	}
	if m.Runtime.Docker.Network != "contAIned-net" {
		t.Errorf("network default: got %q", m.Runtime.Docker.Network)
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
runtime:
  docker:
    image: x
    network: n
policy:
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
	if len(m.Policy.QA.Checks) != 1 {
		t.Fatalf("checks: got %d, want 1", len(m.Policy.QA.Checks))
	}
	c := m.Policy.QA.Checks[0]
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
		Runtime: RuntimeConfig{
			Docker: DockerConfig{
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
	m.Runtime.Docker.Image = ""
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "runtime.docker.image") {
		t.Fatalf("expected image error, got: %v", err)
	}
}

func TestValidate_MissingNetwork(t *testing.T) {
	m := validManifest()
	m.Runtime.Docker.Network = ""
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "runtime.docker.network") {
		t.Fatalf("expected network error, got: %v", err)
	}
}

func TestValidate_Rule_MissingName(t *testing.T) {
	m := validManifest()
	m.Policy.Bash.Rules = []Rule{{Patterns: []string{"rm -rf"}, Action: "block"}}
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), ".name is required") {
		t.Fatalf("expected name error, got: %v", err)
	}
}

func TestValidate_Rule_EmptyPatterns(t *testing.T) {
	m := validManifest()
	m.Policy.Bash.Rules = []Rule{{Name: "no-patterns", Patterns: []string{}, Action: "block"}}
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), ".patterns must not be empty") {
		t.Fatalf("expected patterns error, got: %v", err)
	}
}

func TestValidate_Rule_InvalidAction(t *testing.T) {
	m := validManifest()
	m.Policy.Secrets.Rules = []Rule{{Name: "x", Patterns: []string{"secret"}, Action: "deny"}}
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), `"deny" is invalid`) {
		t.Fatalf("expected invalid action error, got: %v", err)
	}
}

func TestValidate_Rule_ValidActions(t *testing.T) {
	for _, action := range []string{"allow", "block", "escalate"} {
		m := validManifest()
		m.Policy.Bash.Rules = []Rule{{Name: "r", Patterns: []string{"x"}, Action: action}}
		if err := Validate(m); err != nil {
			t.Errorf("action %q: unexpected error: %v", action, err)
		}
	}
}

func TestValidate_Sigstore_EnabledWithoutURLs(t *testing.T) {
	m := validManifest()
	m.Policy.Sigstore.Enabled = true
	m.Policy.Sigstore.RekorURL = ""
	m.Policy.Sigstore.FulcioURL = ""
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "rekor_url") {
		t.Fatalf("expected rekor_url error, got: %v", err)
	}
}

func TestValidate_Sigstore_EnabledFulcioMissing(t *testing.T) {
	m := validManifest()
	m.Policy.Sigstore.Enabled = true
	m.Policy.Sigstore.RekorURL = "https://rekor.sigstore.dev"
	m.Policy.Sigstore.FulcioURL = ""
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "fulcio_url") {
		t.Fatalf("expected fulcio_url error, got: %v", err)
	}
}

func TestValidate_Sigstore_DisabledURLsNotRequired(t *testing.T) {
	m := validManifest()
	m.Policy.Sigstore.Enabled = false
	m.Policy.Sigstore.RekorURL = ""
	m.Policy.Sigstore.FulcioURL = ""
	if err := Validate(m); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestValidate_QACheck_MissingName(t *testing.T) {
	m := validManifest()
	m.Policy.QA.Checks = []QACheck{{Command: []string{"pytest"}}}
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "checks[0].name") {
		t.Fatalf("expected QA name error, got: %v", err)
	}
}

func TestValidate_QACheck_MissingCommand(t *testing.T) {
	m := validManifest()
	m.Policy.QA.Checks = []QACheck{{Name: "tests"}}
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
	content := "runtime:\n  docker:\n    image: test:v1\n    network: testnet\n"
	if err := os.WriteFile(filepath.Join(contained, "manifest.yaml"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	m, err := Load(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Runtime.Docker.Image != "test:v1" {
		t.Errorf("image: got %q", m.Runtime.Docker.Image)
	}
}

func TestLoad_LegacyPath(t *testing.T) {
	dir := t.TempDir()
	legacyDir := filepath.Join(dir, ".contAIned", "policy")
	if err := os.MkdirAll(legacyDir, 0o755); err != nil {
		t.Fatal(err)
	}
	content := "runtime:\n  docker:\n    image: legacy:v1\n    network: testnet\n"
	if err := os.WriteFile(filepath.Join(legacyDir, "manifest.yaml"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	m, err := Load(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Runtime.Docker.Image != "legacy:v1" {
		t.Errorf("image: got %q", m.Runtime.Docker.Image)
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
		m.Runtime.Docker.Toolchains = map[string]string{name: "1.0"}
		if err := Validate(m); err != nil {
			t.Errorf("toolchain %q: unexpected error: %v", name, err)
		}
	}
}

func TestValidate_UnsupportedToolchain_ReturnsError(t *testing.T) {
	m := validManifest()
	m.Runtime.Docker.Toolchains = map[string]string{"rust": "1.80"}
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "unsupported toolchain") {
		t.Fatalf("expected unsupported toolchain error, got: %v", err)
	}
}

func TestValidate_ToolchainEmptyVersion_ReturnsError(t *testing.T) {
	m := validManifest()
	m.Runtime.Docker.Toolchains = map[string]string{"go": ""}
	err := Validate(m)
	if err == nil || !strings.Contains(err.Error(), "must not be empty") {
		t.Fatalf("expected empty version error, got: %v", err)
	}
}

// ── Serialise ─────────────────────────────────────────────────────────────────

func TestSerialise_RoundTrip(t *testing.T) {
	original := validManifest()
	original.Agent.Model = "claude-sonnet-4-6"
	original.Policy.Audit.Enabled = true

	out, err := Serialise(original)
	if err != nil {
		t.Fatalf("serialise error: %v", err)
	}

	reparsed, err := Parse([]byte(out))
	if err != nil {
		t.Fatalf("reparse error: %v", err)
	}
	if reparsed.Agent.Model != "claude-sonnet-4-6" {
		t.Errorf("model round-trip: got %q", reparsed.Agent.Model)
	}
	if !reparsed.Policy.Audit.Enabled {
		t.Error("audit.enabled round-trip: got false")
	}
}
