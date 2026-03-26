package docker

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"contained.dev/cli/internal/manifest"
)

// ── BuildManagedSettings ──────────────────────────────────────────────────────

func baseManifest() *manifest.Manifest {
	return &manifest.Manifest{
		Runtime: manifest.RuntimeConfig{
			Docker: manifest.DockerConfig{Image: "x", Network: "n"},
		},
	}
}

func parsedSettings(t *testing.T, m *manifest.Manifest) map[string]any {
	t.Helper()
	out, err := BuildManagedSettings(m)
	if err != nil {
		t.Fatalf("BuildManagedSettings: %v", err)
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(out), &parsed); err != nil {
		t.Fatalf("output is not valid JSON: %v\n%s", err, out)
	}
	return parsed
}

func TestBuildManagedSettings_ValidJSON(t *testing.T) {
	out, err := BuildManagedSettings(baseManifest())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var v map[string]any
	if err := json.Unmarshal([]byte(out), &v); err != nil {
		t.Fatalf("output is not valid JSON: %v", err)
	}
}

func TestBuildManagedSettings_DefaultDomains(t *testing.T) {
	// No allowed_domains in manifest → defaults should be used.
	m := baseManifest()
	settings := parsedSettings(t, m)

	perms := settings["permissions"].(map[string]any)
	allow := perms["allow"].([]any)

	var domainRules []string
	for _, r := range allow {
		s := r.(string)
		if strings.HasPrefix(s, "WebFetch(domain:") {
			domainRules = append(domainRules, s)
		}
	}

	defaults := []string{
		"WebFetch(domain:api.anthropic.com)",
		"WebFetch(domain:code.claude.com)",
		"WebFetch(domain:docs.anthropic.com)",
	}
	for _, want := range defaults {
		found := false
		for _, got := range domainRules {
			if got == want {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("missing default domain rule %q in allow list", want)
		}
	}
}

func TestBuildManagedSettings_CustomDomains_OverrideDefaults(t *testing.T) {
	m := baseManifest()
	m.Policy.Network.AllowedDomains = []string{"example.com"}
	settings := parsedSettings(t, m)

	perms := settings["permissions"].(map[string]any)
	allow := perms["allow"].([]any)

	for _, r := range allow {
		s := r.(string)
		if strings.HasPrefix(s, "WebFetch(domain:") && s != "WebFetch(domain:example.com)" {
			t.Errorf("unexpected default domain still present: %q", s)
		}
	}

	found := false
	for _, r := range allow {
		if r.(string) == "WebFetch(domain:example.com)" {
			found = true
			break
		}
	}
	if !found {
		t.Error("custom domain not found in allow list")
	}
}

func TestBuildManagedSettings_MCPServers_AddedToAllow(t *testing.T) {
	m := baseManifest()
	m.Policy.MCP.ApprovedServers = []string{"myserver"}
	settings := parsedSettings(t, m)

	perms := settings["permissions"].(map[string]any)
	allow := perms["allow"].([]any)

	found := false
	for _, r := range allow {
		if r.(string) == "mcp__myserver__*" {
			found = true
			break
		}
	}
	if !found {
		t.Error("MCP server permission not found in allow list")
	}
}

func TestBuildManagedSettings_Skills_AddedToAllow(t *testing.T) {
	m := baseManifest()
	m.Policy.Skills.ApprovedSkills = []string{"my-skill"}
	settings := parsedSettings(t, m)

	perms := settings["permissions"].(map[string]any)
	allow := perms["allow"].([]any)

	found := false
	for _, r := range allow {
		if r.(string) == "Skill(my-skill)" {
			found = true
			break
		}
	}
	if !found {
		t.Error("skill permission not found in allow list")
	}
}

func TestBuildManagedSettings_HooksReferenceVenvPython(t *testing.T) {
	out, err := BuildManagedSettings(baseManifest())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(out, "/opt/contained-venv/bin/python3") {
		t.Error("hook commands should reference /opt/contained-venv/bin/python3")
	}
}

func TestBuildManagedSettings_PreCompactHookRegistered(t *testing.T) {
	out, err := BuildManagedSettings(baseManifest())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(out, "PreCompact") {
		t.Error("managed-settings should register a PreCompact hook")
	}
	if !strings.Contains(out, "pre_compact") {
		t.Error("PreCompact hook should reference pre_compact.py")
	}
}

func TestBuildManagedSettings_ContainsRequiredTopLevelKeys(t *testing.T) {
	settings := parsedSettings(t, baseManifest())
	for _, key := range []string{"permissions", "hooks", "sandbox", "allowManagedHooksOnly"} {
		if _, ok := settings[key]; !ok {
			t.Errorf("missing top-level key %q", key)
		}
	}
}

// ── GenerateToolchainsScript ──────────────────────────────────────────────────

func TestGenerateToolchainsScript_EmptyToolchains_NoOp(t *testing.T) {
	script := GenerateToolchainsScript(nil)
	if !strings.HasPrefix(script, "#!/bin/sh") {
		t.Error("script should start with shebang")
	}
	if strings.Contains(script, "curl") || strings.Contains(script, "apt-get") {
		t.Error("empty toolchains should produce a no-op script with no install commands")
	}
}

func TestGenerateToolchainsScript_Go_ContainsTarballURL(t *testing.T) {
	script := GenerateToolchainsScript(map[string]string{"go": "1.22.5"})
	if !strings.Contains(script, "go1.22.5") {
		t.Error("Go script should reference version 1.22.5")
	}
	if !strings.Contains(script, "go.dev/dl/") {
		t.Error("Go script should download from go.dev")
	}
	if !strings.Contains(script, "/usr/local") {
		t.Error("Go script should extract to /usr/local")
	}
}

func TestGenerateToolchainsScript_Go_NormalizesTwoPartVersion(t *testing.T) {
	// Go 1.21+ uses explicit patch-zero in archive names; "1.24" must become "1.24.0".
	script := GenerateToolchainsScript(map[string]string{"go": "1.24"})
	if !strings.Contains(script, "go1.24.0") {
		t.Error("Go script should normalize two-part version 1.24 to 1.24.0 in download URL")
	}
	if strings.Contains(script, "go1.24.linux") {
		t.Error("Go script must not use unnormalized go1.24 in the URL")
	}
}

func TestGenerateToolchainsScript_Go_ThreePartVersionUnchanged(t *testing.T) {
	script := GenerateToolchainsScript(map[string]string{"go": "1.24.1"})
	if !strings.Contains(script, "go1.24.1") {
		t.Error("Go script should preserve three-part version 1.24.1 unchanged")
	}
}

func TestGenerateToolchainsScript_Node_UsesNodeSource(t *testing.T) {
	script := GenerateToolchainsScript(map[string]string{"node": "20.11.0"})
	if !strings.Contains(script, "nodesource.com/setup_20.x") {
		t.Error("Node script should use NodeSource major version setup script")
	}
}

func TestGenerateToolchainsScript_Node_MajorVersionOnly(t *testing.T) {
	// A bare major version (e.g., "20") should also work.
	script := GenerateToolchainsScript(map[string]string{"node": "20"})
	if !strings.Contains(script, "setup_20.x") {
		t.Error("Node script should use major version 20")
	}
}

func TestGenerateToolchainsScript_Ruby_UsesRubyBuild(t *testing.T) {
	script := GenerateToolchainsScript(map[string]string{"ruby": "3.3.0"})
	if !strings.Contains(script, "ruby-build") {
		t.Error("Ruby script should use ruby-build")
	}
	if !strings.Contains(script, "3.3.0") {
		t.Error("Ruby script should reference version 3.3.0")
	}
}

func TestGenerateToolchainsScript_Java_UsesTemurin(t *testing.T) {
	script := GenerateToolchainsScript(map[string]string{"java": "21.0.3"})
	if !strings.Contains(script, "temurin-21-jdk") {
		t.Error("Java script should install temurin-21-jdk")
	}
	if !strings.Contains(script, "adoptium.net") {
		t.Error("Java script should use Adoptium repository")
	}
}

func TestGenerateToolchainsScript_MultipleToolchains(t *testing.T) {
	script := GenerateToolchainsScript(map[string]string{
		"go":   "1.22.5",
		"node": "20",
	})
	if !strings.Contains(script, "go.dev") {
		t.Error("script should contain Go installation")
	}
	if !strings.Contains(script, "nodesource") {
		t.Error("script should contain Node installation")
	}
}

func TestGenerateToolchainsScript_StartsWithShebang(t *testing.T) {
	for _, tc := range []map[string]string{
		nil,
		{"go": "1.22.5"},
		{"node": "20"},
	} {
		script := GenerateToolchainsScript(tc)
		if !strings.HasPrefix(script, "#!/bin/sh") {
			t.Errorf("script should start with #!/bin/sh, got: %.20q", script)
		}
	}
}

// ── GenerateDepsScript ────────────────────────────────────────────────────────

func TestGenerateDepsScript_EmptyDeps_NoOp(t *testing.T) {
	script := GenerateDepsScript(nil)
	if !strings.HasPrefix(script, "#!/bin/sh") {
		t.Error("script should start with shebang")
	}
	if strings.Contains(script, "go install") || strings.Contains(script, "curl") {
		t.Error("empty deps should produce a no-op script with no install commands")
	}
}

func TestGenerateDepsScript_GolangciLint_UsesGoInstall(t *testing.T) {
	script := GenerateDepsScript([]string{"golangci-lint"})
	if !strings.Contains(script, "go install") {
		t.Error("golangci-lint should be installed via go install")
	}
	if !strings.Contains(script, "golangci-lint") {
		t.Error("script should reference golangci-lint")
	}
	if !strings.Contains(script, "GOBIN=/usr/local/bin") {
		t.Error("golangci-lint should be installed to /usr/local/bin")
	}
}

func TestGenerateDepsScript_GolangciLint_SetsGoPath(t *testing.T) {
	script := GenerateDepsScript([]string{"golangci-lint"})
	if !strings.Contains(script, "/usr/local/go/bin") {
		t.Error("script should prepend /usr/local/go/bin to PATH")
	}
}

func TestGenerateDepsScript_UnknownDep_PrintsWarning(t *testing.T) {
	script := GenerateDepsScript([]string{"unknown-tool"})
	if !strings.Contains(script, "WARNING") {
		t.Error("unknown dep should produce a WARNING line")
	}
	if !strings.Contains(script, "unknown-tool") {
		t.Error("warning should name the unknown dep")
	}
}

func TestGenerateDepsScript_StartsWithShebang(t *testing.T) {
	for _, deps := range [][]string{nil, {"golangci-lint"}, {"unknown"}} {
		script := GenerateDepsScript(deps)
		if !strings.HasPrefix(script, "#!/bin/sh") {
			t.Errorf("script should start with #!/bin/sh for deps %v, got: %.20q", deps, script)
		}
	}
}

// ── PolicyPull ────────────────────────────────────────────────────────────────

func TestPolicyPull_NomAInlinedURL_ReturnsOriginal(t *testing.T) {
	original := "runtime:\n  docker:\n    image: x\n    network: n\n"
	got := PolicyPull(original)
	if got != original {
		t.Errorf("expected original content unchanged, got: %q", got)
	}
}

func TestPolicyPull_ServerReturnsRefs_MergedIntoYAML(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"policy_ref":"abc123","policy_version":"v2"}`))
	}))
	defer srv.Close()

	original := "policy:\n  mAInlined:\n    url: " + srv.URL + "\n    policy_name: mypolicy\n"
	got := PolicyPull(original)

	if !strings.Contains(got, "abc123") {
		t.Errorf("policy_ref not merged into output: %q", got)
	}
	if !strings.Contains(got, "v2") {
		t.Errorf("policy_version not merged into output: %q", got)
	}
}

func TestPolicyPull_ServerError_ReturnsOriginal(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "internal error", http.StatusInternalServerError)
	}))
	defer srv.Close()

	original := "policy:\n  mAInlined:\n    url: " + srv.URL + "\n    policy_name: mypolicy\n"
	got := PolicyPull(original)
	if got != original {
		t.Errorf("expected original on server error, got: %q", got)
	}
}

func TestPolicyPull_InvalidJSON_ReturnsOriginal(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("not json"))
	}))
	defer srv.Close()

	original := "policy:\n  mAInlined:\n    url: " + srv.URL + "\n    policy_name: mypolicy\n"
	got := PolicyPull(original)
	if got != original {
		t.Errorf("expected original on invalid JSON, got: %q", got)
	}
}

func TestPolicyPull_InvalidYAMLInput_ReturnsOriginal(t *testing.T) {
	bad := "not: [valid: yaml: :::"
	got := PolicyPull(bad)
	if got != bad {
		t.Errorf("expected original on invalid YAML input, got: %q", got)
	}
}
