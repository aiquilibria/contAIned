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
		Init: manifest.InitConfig{
			Container: manifest.ContainerConfig{Image: "x", Network: "n"},
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
	m.Runtime.Network.AllowedDomains = []string{"example.com"}
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
	m.Init.MCP.ApprovedServers = []string{"myserver"}
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
	m.Init.Skills.ApprovedSkills = []string{"my-skill"}
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
	if !strings.Contains(script, "dl.google.com/go/") {
		t.Error("Go script should download from dl.google.com/go/")
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
	if !strings.Contains(script, "dl.google.com/go/") {
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

// ── extractPolicyMainlinedURL ─────────────────────────────────────────────────

func TestExtractPolicyMainlinedURL_Empty(t *testing.T) {
	if got := extractPolicyMainlinedURL(""); got != "" {
		t.Errorf("expected empty, got %q", got)
	}
}

func TestExtractPolicyMainlinedURL_Present(t *testing.T) {
	policyYAML := `
init:
  mainlined:
    url: "http://mainlined:8080"
    policy_name: "default"
`
	got := extractPolicyMainlinedURL(policyYAML)
	if got != "http://mainlined:8080" {
		t.Errorf("expected %q, got %q", "http://mainlined:8080", got)
	}
}

func TestExtractPolicyMainlinedURL_MissingSection(t *testing.T) {
	policyYAML := "runtime:\n  network:\n    enabled: true\n"
	if got := extractPolicyMainlinedURL(policyYAML); got != "" {
		t.Errorf("expected empty when section absent, got %q", got)
	}
}

func TestBuildManagedSettings_mAInlinedDomainFromPolicyYAML(t *testing.T) {
	m := baseManifest()
	m.Init.Mainlined.URL = "http://localhost:8080/aiquilibria/default" // should be skipped
	m.Init.Mainlined.PolicyYAML = "init:\n  mainlined:\n    url: \"http://mainlined:8080\"\n"

	settings := parsedSettings(t, m)

	// Check sandbox network allowedDomains.
	sandbox := settings["sandbox"].(map[string]any)
	network := sandbox["network"].(map[string]any)
	domains := network["allowedDomains"].([]any)
	found := false
	for _, d := range domains {
		if d.(string) == "mainlined" {
			found = true
			break
		}
	}
	if !found {
		t.Errorf("expected 'mainlined' in sandbox.network.allowedDomains, got %v", domains)
	}

	// Also check WebFetch allow rule.
	perms := settings["permissions"].(map[string]any)
	allow := perms["allow"].([]any)
	foundWF := false
	for _, r := range allow {
		if r.(string) == "WebFetch(domain:mainlined)" {
			foundWF = true
			break
		}
	}
	if !foundWF {
		t.Errorf("expected WebFetch(domain:mainlined) in permissions.allow, got %v", allow)
	}
}

func TestBuildManagedSettings_mAInlinedLocalhostNotAdded(t *testing.T) {
	m := baseManifest()
	m.Init.Mainlined.URL = "http://localhost:8080/path"
	// No policy_yaml — falls back to URL which is localhost, should be skipped.

	settings := parsedSettings(t, m)
	sandbox := settings["sandbox"].(map[string]any)
	network := sandbox["network"].(map[string]any)
	domains := network["allowedDomains"].([]any)
	for _, d := range domains {
		if d.(string) == "localhost" {
			t.Error("localhost should not appear in allowedDomains")
		}
	}
}

// ── InjectMaInlinedDomain ─────────────────────────────────────────────────────

func TestInjectMaInlinedDomain_EmptyList_NoOp(t *testing.T) {
	m := baseManifest()
	m.Init.Mainlined.PolicyYAML = "init:\n  mainlined:\n    url: \"http://mainlined:8080\"\n"
	InjectMaInlinedDomain(m)
	if len(m.Runtime.Network.AllowedDomains) != 0 {
		t.Error("should not modify empty domain list (defaults path)")
	}
}

func TestInjectMaInlinedDomain_AddsHostname(t *testing.T) {
	m := baseManifest()
	m.Runtime.Network.AllowedDomains = []string{"api.anthropic.com"}
	m.Init.Mainlined.URL = "http://localhost:8080/path"
	m.Init.Mainlined.PolicyYAML = "init:\n  mainlined:\n    url: \"http://mainlined:8080\"\n"
	InjectMaInlinedDomain(m)
	last := m.Runtime.Network.AllowedDomains[len(m.Runtime.Network.AllowedDomains)-1]
	if last != "mainlined" {
		t.Errorf("expected 'mainlined' appended, got %q", last)
	}
}

func TestInjectMaInlinedDomain_Idempotent(t *testing.T) {
	m := baseManifest()
	m.Runtime.Network.AllowedDomains = []string{"api.anthropic.com", "mainlined"}
	m.Init.Mainlined.PolicyYAML = "init:\n  mainlined:\n    url: \"http://mainlined:8080\"\n"
	before := len(m.Runtime.Network.AllowedDomains)
	InjectMaInlinedDomain(m)
	if len(m.Runtime.Network.AllowedDomains) != before {
		t.Error("should not add duplicate domain")
	}
}

func TestInjectMaInlinedDomain_LocalhostSkipped(t *testing.T) {
	m := baseManifest()
	m.Runtime.Network.AllowedDomains = []string{"api.anthropic.com"}
	m.Init.Mainlined.URL = "http://localhost:8080/path"
	before := len(m.Runtime.Network.AllowedDomains)
	InjectMaInlinedDomain(m)
	if len(m.Runtime.Network.AllowedDomains) != before {
		t.Error("localhost URL should not add any domain")
	}
}

// ── PolicyPull ────────────────────────────────────────────────────────────────

func TestPolicyPull_NomAInlinedURL_ReturnsOriginal(t *testing.T) {
	original := "init:\n  container:\n    image: x\n    network: n\n"
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

	original := "init:\n  mainlined:\n    url: " + srv.URL + "\n    policy_name: mypolicy\n"
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

	original := "init:\n  mainlined:\n    url: " + srv.URL + "\n    policy_name: mypolicy\n"
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

	original := "init:\n  mainlined:\n    url: " + srv.URL + "\n    policy_name: mypolicy\n"
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

// ── Plugin marketplace settings ───────────────────────────────────────────────

func boolPtr(b bool) *bool { return &b }

func TestBuildManagedSettings_Plugins_StrictFalse_KeyAbsent(t *testing.T) {
	// strict_marketplaces: false (default) → key must not appear in output.
	m := baseManifest()
	settings := parsedSettings(t, m)
	if _, ok := settings["strictKnownMarketplaces"]; ok {
		t.Error("strictKnownMarketplaces should be absent when StrictMarketplaces is false")
	}
}

func TestBuildManagedSettings_Plugins_NoExtra_ExtraKeyAbsent(t *testing.T) {
	// No extra marketplaces → extraKnownMarketplaces must not appear.
	m := baseManifest()
	settings := parsedSettings(t, m)
	if _, ok := settings["extraKnownMarketplaces"]; ok {
		t.Error("extraKnownMarketplaces should be absent when ExtraMarketplaces is empty")
	}
}

func TestBuildManagedSettings_Plugins_StrictTrue_BuiltinTrue_IncludesOfficial(t *testing.T) {
	// strict_marketplaces: true + builtin_marketplace: true →
	// strictKnownMarketplaces contains the official github source (not locked out),
	// and extraKnownMarketplaces registers the name→source mapping so
	// "plugin@claude-plugins-official" resolves during docker build.
	m := baseManifest()
	m.Init.Plugins.StrictMarketplaces = true
	m.Init.Plugins.BuiltinMarketplace = boolPtr(true)

	settings := parsedSettings(t, m)

	raw, ok := settings["strictKnownMarketplaces"]
	if !ok {
		t.Fatal("strictKnownMarketplaces should be present")
	}
	sources := raw.([]any)
	if len(sources) != 1 {
		t.Fatalf("expected 1 entry (official marketplace), got %d: %v", len(sources), sources)
	}
	entry := sources[0].(map[string]any)
	if entry["source"] != "github" || entry["repo"] != "anthropics/claude-plugins-official" {
		t.Errorf("expected official marketplace github entry, got %v", entry)
	}

	// extraKnownMarketplaces must register the name so docker build can resolve it.
	extraRaw, ok := settings["extraKnownMarketplaces"]
	if !ok {
		t.Fatal("extraKnownMarketplaces should be present when builtin_marketplace: true")
	}
	extraMap := extraRaw.(map[string]any)
	official, ok := extraMap["claude-plugins-official"]
	if !ok {
		t.Fatal("extraKnownMarketplaces should contain 'claude-plugins-official'")
	}
	officialSrc := official.(map[string]any)["source"].(map[string]any)
	if officialSrc["source"] != "github" || officialSrc["repo"] != "anthropics/claude-plugins-official" {
		t.Errorf("claude-plugins-official source: got %v", officialSrc)
	}
}

func TestBuildManagedSettings_Plugins_BuiltinTrue_NoStrict_ExtraKeyPresent(t *testing.T) {
	// builtin_marketplace: true without strict mode → extraKnownMarketplaces
	// still registers the name so preinstall resolution works.
	m := baseManifest()
	m.Init.Plugins.BuiltinMarketplace = boolPtr(true)

	settings := parsedSettings(t, m)

	if _, ok := settings["strictKnownMarketplaces"]; ok {
		t.Error("strictKnownMarketplaces should be absent when StrictMarketplaces is false")
	}
	extraRaw, ok := settings["extraKnownMarketplaces"]
	if !ok {
		t.Fatal("extraKnownMarketplaces should be present when builtin_marketplace: true")
	}
	extraMap := extraRaw.(map[string]any)
	if _, ok := extraMap["claude-plugins-official"]; !ok {
		t.Error("extraKnownMarketplaces should contain 'claude-plugins-official'")
	}
}

func TestBuildManagedSettings_Plugins_StrictTrue_BuiltinFalse_EmptyList(t *testing.T) {
	// builtin_marketplace: false with no extra_marketplaces → same result: empty list.
	m := baseManifest()
	m.Init.Plugins.StrictMarketplaces = true
	m.Init.Plugins.BuiltinMarketplace = boolPtr(false)

	settings := parsedSettings(t, m)
	raw, ok := settings["strictKnownMarketplaces"]
	if !ok {
		t.Fatal("strictKnownMarketplaces should be present")
	}
	sources := raw.([]any)
	if len(sources) != 0 {
		t.Errorf("expected empty list, got %v", sources)
	}
}

func TestBuildManagedSettings_Plugins_ExtraMarketplaces_ExtraKeyPresent(t *testing.T) {
	// extra_marketplaces populated → extraKnownMarketplaces present with correct structure.
	m := baseManifest()
	m.Init.Plugins.ExtraMarketplaces = []manifest.PluginMarketplace{
		{Source: "github", Repo: "acme-corp/plugins", Ref: "main"},
	}

	settings := parsedSettings(t, m)
	raw, ok := settings["extraKnownMarketplaces"]
	if !ok {
		t.Fatal("extraKnownMarketplaces should be present")
	}
	extraMap := raw.(map[string]any)
	if len(extraMap) != 1 {
		t.Fatalf("expected 1 entry, got %d", len(extraMap))
	}
	entry, ok := extraMap["acme-corp-plugins"]
	if !ok {
		t.Fatalf("expected key 'acme-corp-plugins', got keys %v", extraMap)
	}
	entryMap := entry.(map[string]any)
	src := entryMap["source"].(map[string]any)
	if src["source"] != "github" {
		t.Errorf("source.source: got %v", src["source"])
	}
	if src["repo"] != "acme-corp/plugins" {
		t.Errorf("source.repo: got %v", src["repo"])
	}
	if src["ref"] != "main" {
		t.Errorf("source.ref: got %v", src["ref"])
	}
}

func TestBuildManagedSettings_Plugins_StrictAndExtra_BothKeysPresent(t *testing.T) {
	// strict + builtin + extra → strictKnownMarketplaces has 2 entries: the
	// official marketplace slug and the extra source; extraKnownMarketplaces
	// is also present.
	m := baseManifest()
	m.Init.Plugins.StrictMarketplaces = true
	m.Init.Plugins.BuiltinMarketplace = boolPtr(true)
	m.Init.Plugins.ExtraMarketplaces = []manifest.PluginMarketplace{
		{Source: "github", Repo: "acme-corp/plugins"},
	}

	settings := parsedSettings(t, m)

	strictRaw, ok := settings["strictKnownMarketplaces"]
	if !ok {
		t.Fatal("strictKnownMarketplaces should be present")
	}
	sources := strictRaw.([]any)
	if len(sources) != 2 {
		t.Fatalf("expected 2 entries (official + extra), got %d", len(sources))
	}
	// First entry: official marketplace github source.
	first := sources[0].(map[string]any)
	if first["source"] != "github" || first["repo"] != "anthropics/claude-plugins-official" {
		t.Errorf("first entry should be official marketplace, got %v", first)
	}
	// Second entry: extra github source.
	second := sources[1].(map[string]any)
	if second["source"] != "github" {
		t.Errorf("second entry source: got %v", second["source"])
	}

	extraRaw, ok := settings["extraKnownMarketplaces"]
	if !ok {
		t.Fatal("extraKnownMarketplaces should be present")
	}
	// Both claude-plugins-official (from builtin) and acme-corp-plugins (from extra).
	extraMap := extraRaw.(map[string]any)
	if len(extraMap) != 2 {
		t.Fatalf("expected 2 entries in extraKnownMarketplaces, got %d: %v", len(extraMap), extraMap)
	}
	if _, ok := extraMap["claude-plugins-official"]; !ok {
		t.Error("extraKnownMarketplaces should contain 'claude-plugins-official'")
	}
	if _, ok := extraMap["acme-corp-plugins"]; !ok {
		t.Error("extraKnownMarketplaces should contain 'acme-corp-plugins'")
	}
}
