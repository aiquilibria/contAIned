package docker

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"contained.dev/cli/internal/manifest"
	"contained.dev/cli/internal/pysource"
	"contained.dev/cli/internal/scaffold"
	ver "contained.dev/cli/internal/version"
	"gopkg.in/yaml.v3"
)

var version = ver.Version

// buildPluginMarketplaceSettings derives the strictKnownMarketplaces and
// extraKnownMarketplaces values from the manifest's plugin policy.
//
// Returns:
//   - strict: slice of source objects for strictKnownMarketplaces, or nil when
//     StrictMarketplaces is false (key should be omitted from output).
//   - extra: map of name→{source:…} for extraKnownMarketplaces, or nil when
//     ExtraMarketplaces is empty (key should be omitted from output).
func buildPluginMarketplaceSettings(m *manifest.Manifest) (strict, extra any) {
	p := m.Init.Plugins

	// extraKnownMarketplaces — registers marketplace name→source mappings so
	// that "name@marketplace" plugin references resolve without the user
	// running /plugin marketplace add first.
	//
	// Always include claude-plugins-official when builtin_marketplace is true
	// so that "plugin@claude-plugins-official" resolves during docker build
	// (fresh agent user has no ~/.claude/plugins/known_marketplaces.json).
	extraMap := make(map[string]any)
	if p.BuiltinMarketplace != nil && *p.BuiltinMarketplace {
		extraMap["claude-plugins-official"] = map[string]any{
			"source": map[string]any{
				"source": "github",
				"repo":   "anthropics/claude-plugins-official",
			},
		}
	}
	for _, mp := range p.ExtraMarketplaces {
		key := marketplaceKey(mp)
		extraMap[key] = map[string]any{
			"source": marketplaceSourceObject(mp),
		}
	}
	if len(extraMap) > 0 {
		extra = extraMap
	}

	// strictKnownMarketplaces — only emit when strict mode is enabled.
	if !p.StrictMarketplaces {
		return nil, extra
	}

	// Build the strictKnownMarketplaces allowlist. When builtin_marketplace is
	// true, include the Anthropic official marketplace by slug so it is not
	// blocked by the lockdown (strictKnownMarketplaces: [] blocks everything
	// including the auto-registered official marketplace).
	sources := make([]any, 0)
	if p.BuiltinMarketplace != nil && *p.BuiltinMarketplace {
		sources = append(sources, map[string]any{
			"source": "github",
			"repo":   "anthropics/claude-plugins-official",
		})
	}
	for _, mp := range p.ExtraMarketplaces {
		sources = append(sources, marketplaceSourceObject(mp))
	}
	return sources, extra
}

// marketplaceSourceObject converts a PluginMarketplace to the Claude Code
// source object format used in strictKnownMarketplaces entries.
func marketplaceSourceObject(mp manifest.PluginMarketplace) map[string]any {
	obj := map[string]any{"source": mp.Source}
	if mp.Repo != "" {
		obj["repo"] = mp.Repo
	}
	if mp.Ref != "" {
		obj["ref"] = mp.Ref
	}
	if mp.Path != "" {
		obj["path"] = mp.Path
	}
	if mp.HostPattern != "" {
		obj["hostPattern"] = mp.HostPattern
	}
	if mp.Package != "" {
		obj["package"] = mp.Package
	}
	return obj
}

// marketplaceKey derives a stable map key for extraKnownMarketplaces from a
// PluginMarketplace. Uses "repo" for github sources, "package" for npm,
// "hostPattern" for hostPattern, and falls back to the source value itself.
func marketplaceKey(mp manifest.PluginMarketplace) string {
	switch mp.Source {
	case "github":
		// "acme-corp/plugins" → "acme-corp-plugins"
		return strings.ReplaceAll(mp.Repo, "/", "-")
	case "npm":
		// "@acme/plugins" → "acme-plugins"
		return strings.Trim(strings.ReplaceAll(mp.Package, "/", "-"), "@-")
	case "hostPattern":
		// Use a truncated sanitised form of the pattern.
		key := strings.NewReplacer(
			"\\.", "-", "^", "", "$", "", ".", "-",
		).Replace(mp.HostPattern)
		if len(key) > 40 {
			key = key[:40]
		}
		return strings.Trim(key, "-")
	default:
		return mp.Source
	}
}

// BuildManagedSettings generates the managed-settings.json content that is
// baked into the Docker image. The dynamic sections (domain allow-list, MCP
// server permissions, skill permissions) are derived from the manifest.
func BuildManagedSettings(m *manifest.Manifest) (string, error) {
	allowedDomains := m.Runtime.Network.AllowedDomains
	if len(allowedDomains) == 0 {
		allowedDomains = []string{
			"api.anthropic.com",
			"code.claude.com",
			"docs.anthropic.com",
		}
	}

	// Ensure the mAInlined host is in the allow-list. When the manifest already
	// has an explicit domain list, InjectMaInlinedDomain will have added it
	// before this call (via init.go), so the loop below is typically a no-op.
	// For manifests using the default empty list the domain must still be added
	// to the local allowedDomains slice (which holds the hardcoded defaults).
	if host := maInlinedHostname(m); host != "" {
		alreadyPresent := false
		for _, d := range allowedDomains {
			if d == host {
				alreadyPresent = true
				break
			}
		}
		if !alreadyPresent {
			allowedDomains = append(allowedDomains, host)
		}
	}

	allowRules := []string{
		"Read(/workspace/**)",
		"Glob(/workspace/**)",
		"Grep(/workspace/**)",
		"mcp__plugin_contained_tracer__*",
		"Skill(contained:tracer)",
		"Skill(contained:submit)",
	}
	for _, domain := range allowedDomains {
		allowRules = append(allowRules, "WebFetch(domain:"+domain+")")
	}
	for _, server := range m.Init.MCP.ApprovedServers {
		allowRules = append(allowRules, "mcp__"+server+"__*")
	}
	for _, skill := range m.Init.Skills.ApprovedSkills {
		allowRules = append(allowRules, "Skill("+skill+")")
	}

	hookCmd := "/opt/contained-venv/bin/python3 /etc/contained/hooks/%s.py"
	h := func(name string) map[string]any {
		return map[string]any{"type": "command", "command": fmt.Sprintf(hookCmd, name)}
	}

	settings := map[string]any{
		"permissions": map[string]any{
			"allow":                           allowRules,
			"disableBypassPermissionsMode":    "disable",
			"allowManagedPermissionRulesOnly": true,
		},
		"hooks": map[string]any{
			"PreToolUse": []any{
				map[string]any{"matcher": "Read|Glob|Grep", "hooks": []any{h("restrict_reads")}},
				map[string]any{"matcher": "Write|Edit|MultiEdit", "hooks": []any{h("restrict_writes"), h("tracer_pre")}},
				map[string]any{"matcher": "Bash", "hooks": []any{h("restrict_bash")}},
				map[string]any{"matcher": "WebFetch|WebSearch", "hooks": []any{h("restrict_network")}},
			},
			"PostToolUse": []any{
				map[string]any{"matcher": "Write|Edit|MultiEdit", "hooks": []any{h("tracer_post")}},
				map[string]any{"matcher": "Bash", "hooks": []any{h("push_hook")}},
				map[string]any{"matcher": "*", "hooks": []any{h("audit")}},
			},
			"SubagentStart":     []any{map[string]any{"hooks": []any{h("subagent_start")}}},
			"SubagentStop":      []any{map[string]any{"hooks": []any{h("subagent_stop")}}},
			"Stop":              []any{map[string]any{"hooks": []any{h("summarizer")}}},
			"PreCompact":        []any{map[string]any{"hooks": []any{h("pre_compact")}}},
			"UserPromptSubmit":  []any{map[string]any{"hooks": []any{h("user_prompt_submit")}}},
			"PermissionRequest": []any{map[string]any{"hooks": []any{h("permission_request")}}},
		},
		"sandbox": map[string]any{
			"enabled":                   true,
			"enableWeakerNestedSandbox": true,
			"allowUnsandboxedCommands":  false,
			"network": map[string]any{
				"allowedDomains":          allowedDomains,
				"allowManagedDomainsOnly": true,
			},
			"filesystem": map[string]any{
				"denyWrite": []string{".contAIned", ".claude/settings.json"},
			},
		},
		"allowManagedHooksOnly": true,
		"statusLine": map[string]any{
			"type":    "command",
			"command": "python3 /etc/contained/statusline.py",
		},
		"attribution": map[string]any{
			"commit": "Generated with Claude Code on cont[AI]ned",
			"pr":     "Generated with Claude Code on cont[AI]ned",
		},
	}

	// Inject ecosystem-derived env vars (e.g. GOCACHE for Go).
	// These are populated by MergeRepoManifest from the ecosystem_definitions
	// entries for each active ecosystem declared by the repo manifest.
	if len(m.Init.Container.Env) > 0 {
		settings["env"] = m.Init.Container.Env
	}

	// Inject plugin marketplace policy derived from policy.plugins.
	strictMarketplaces, extraMarketplaces := buildPluginMarketplaceSettings(m)
	if strictMarketplaces != nil {
		settings["strictKnownMarketplaces"] = strictMarketplaces
	}
	if extraMarketplaces != nil {
		settings["extraKnownMarketplaces"] = extraMarketplaces
	}

	out, err := json.MarshalIndent(settings, "", "  ")
	if err != nil {
		return "", fmt.Errorf("marshalling managed-settings: %w", err)
	}
	return string(out), nil
}

// PolicyPull attempts to fetch policy_ref and policy_version from the
// mAInlined instance and write them back into the manifest YAML.
// Falls back to the original content on any error (network unavailable,
// missing fields) so offline workflows are unaffected.
func PolicyPull(manifestContent string) string {
	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(manifestContent), &parsed); err != nil {
		return manifestContent
	}

	mAInlined, _ := nestedMap(parsed, "init", "mainlined")
	url := strings.TrimRight(stringVal(mAInlined["url"]), "/")
	policyName := strings.TrimSpace(stringVal(mAInlined["policy_name"]))
	if url == "" || policyName == "" {
		return manifestContent
	}

	endpoint := url + "/policy/" + policyName
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Get(endpoint)
	if err != nil || resp.StatusCode != 200 {
		return manifestContent
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		return manifestContent
	}

	policyRef := stringVal(data["policy_ref"])
	policyVersion := stringVal(data["policy_version"])
	if policyRef == "" && policyVersion == "" {
		return manifestContent
	}

	if mAInlined == nil {
		mAInlined = map[string]any{}
	}
	mAInlined["policy_ref"] = policyRef
	mAInlined["policy_version"] = policyVersion

	// Write back into the parsed map.
	if initSection, ok := parsed["init"].(map[string]any); ok {
		initSection["mainlined"] = mAInlined
	}

	out, err := yaml.Marshal(parsed)
	if err != nil {
		return manifestContent
	}
	return string(out)
}

// DockerSetup builds (or skips building) the contained Docker image, then
// ensures the named volume and network exist.
//
// Returns true if the image was (re)built, false if it was up to date.
func DockerSetup(
	cfg manifest.ContainerConfig,
	workspace string,
	rebuild bool,
	manifestContent string,
	managedSettingsContent string,
	claudeMdContent string,
	pluginsToInstall string,
	marketplaceClones string,
	netrcFromSecrets string,
	printf func(string, ...any),
) (bool, error) {
	dockerBin, err := findDocker()
	if err != nil {
		return false, err
	}

	image := cfg.Image
	manifestHash := shortHash(manifestContent)
	manifestB64 := base64.StdEncoding.EncodeToString([]byte(manifestContent))
	// Full SHA-256 of the manifest content in the same format used by mAInlined
	// registration. Stored as an image label so reRegister() can retrieve it
	// without needing a copy of the manifest in the workspace.
	h := sha256.Sum256([]byte(manifestContent))
	manifestSHA256 := hex.EncodeToString(h[:])
	settingsB64 := base64.StdEncoding.EncodeToString([]byte(managedSettingsContent))
	claudeMdB64 := base64.StdEncoding.EncodeToString([]byte(claudeMdContent))

	needsBuild := true
	if rebuild {
		printf("  Image %s — forced rebuild requested.\n", image)
	} else {
		labelFmt := `{{index .Config.Labels "contAIned.version"}}|{{index .Config.Labels "contAIned.manifest_hash"}}`
		out, err := exec.Command(dockerBin, "image", "inspect", "--format", labelFmt, image).Output()
		if err == nil {
			parts := strings.SplitN(strings.TrimSpace(string(out)), "|", 2)
			imageVersion := parts[0]
			imageHash := ""
			if len(parts) > 1 {
				imageHash = parts[1]
			}
			if imageVersion == version && imageHash == manifestHash {
				printf("  Image %s is up to date (%s) — skipping build.\n", image, version)
				needsBuild = false
			} else if imageVersion != version {
				label := imageVersion
				if label == "" {
					label = "unlabelled"
				}
				printf("  Image %s is stale (%s → %s) — rebuilding.\n", image, label, version)
			} else {
				printf("  Image %s policy has changed — rebuilding.\n", image)
			}
		}
	}

	if needsBuild {
		// Warn if a session is currently running.
		psOut, _ := exec.Command(
			dockerBin, "ps", "--filter",
			"name=contAIned-"+filepath.Base(workspace)+"-", "--quiet",
		).Output()
		if strings.TrimSpace(string(psOut)) != "" {
			printf("  Warning: a contAIned session for %s appears to be running.\n",
				filepath.Base(workspace))
		}

		buildContext, err := prepareBuildContext(cfg.Toolchains, cfg.Deps, marketplaceClones, printf)
		if err != nil {
			return false, err
		}
		defer os.RemoveAll(buildContext)

		printf("  Building image %s …", image)

		uid, gid := hostUIDGID()
		buildArgs := []string{
			dockerBin, "build",
			"--build-arg", "HOST_UID=" + uid,
			"--build-arg", "HOST_GID=" + gid,
			"--label", "contAIned.version=" + version,
			"--label", "contAIned.manifest_hash=" + manifestHash,
			"--label", "contAIned.manifest_sha256=" + manifestSHA256,
			"--build-arg", "MANIFEST_CONTENT=" + manifestB64,
			"--build-arg", "MANAGED_SETTINGS_CONTENT=" + settingsB64,
			"--build-arg", "CLAUDE_MD_CONTENT=" + claudeMdB64,
			"-t", image,
		}
		if pluginsToInstall != "" {
			buildArgs = append(buildArgs, "--build-arg", "PLUGINS_TO_INSTALL="+pluginsToInstall)
		}
		if netrcFromSecrets != "" {
			buildArgs = append(buildArgs, "--build-arg", "NETRC_FROM_SECRETS="+netrcFromSecrets)
		}

		dockerfilePath := filepath.Join(buildContext, "Dockerfile")
		// --progress=plain ensures RUN-step stdout (e.g. plugin install lines)
		// appears in CombinedOutput even when there is no TTY.
		buildArgs = append(buildArgs, "--progress=plain", "-f", dockerfilePath, buildContext)

		result, err := exec.Command(buildArgs[0], buildArgs[1:]...).CombinedOutput()
		if err != nil {
			printf(" failed\n")
			return false, fmt.Errorf("docker build failed:\n%s", string(result))
		}
		printf(" done\n")
		// Surface any [contained] diagnostic lines emitted during the build
		// (e.g. plugin install progress) so operators can confirm what happened.
		for _, line := range strings.Split(string(result), "\n") {
			trimmed := strings.TrimSpace(line)
			if strings.Contains(trimmed, "[contained]") && !strings.HasPrefix(trimmed, "#") {
				printf("  %s\n", trimmed)
			}
		}
	}

	// Create named volume (idempotent).
	vol := cfg.AgentConfigVolume
	if out, err := exec.Command(dockerBin, "volume", "create", vol).CombinedOutput(); err != nil {
		return false, fmt.Errorf("docker volume create %s: %s", vol, strings.TrimSpace(string(out)))
	}
	printf("  Volume %s ready.\n", vol)

	// Create bridge network (idempotent — "already exists" is not an error).
	net := cfg.Network
	out, err := exec.Command(dockerBin, "network", "create", "--driver", "bridge", net).CombinedOutput()
	if err != nil && !strings.Contains(string(out), "already exists") {
		return false, fmt.Errorf("docker network create %s: %s", net, strings.TrimSpace(string(out)))
	}
	printf("  Network %s ready.\n", net)

	return needsBuild, nil
}

// GenerateToolchainsScript returns a shell script that installs each toolchain
// in the provided map using its official method. Returns a no-op script when
// the map is empty. The script runs as root inside the Docker build context.
//
// Supported toolchains: go, node, ruby, java.
func GenerateToolchainsScript(toolchains map[string]string) string {
	if len(toolchains) == 0 {
		return "#!/bin/sh\n# no toolchains declared\n"
	}

	var b strings.Builder
	b.WriteString("#!/bin/sh\nset -eu\n\n")
	b.WriteString("# Detect architecture once.\n")
	b.WriteString("ARCH=$(uname -m)\n")
	b.WriteString("case \"$ARCH\" in\n")
	b.WriteString("  x86_64)  ARCH=amd64 ;;\n")
	b.WriteString("  aarch64) ARCH=arm64 ;;\n")
	b.WriteString("  *) echo \"Unsupported architecture: $ARCH\" >&2; exit 1 ;;\n")
	b.WriteString("esac\n\n")

	if ver, ok := toolchains["go"]; ok {
		// Go 1.21+ uses explicit patch-zero in archive names (go1.24.0, not go1.24).
		// Normalize two-part versions so the download URL is always valid.
		if strings.Count(ver, ".") == 1 {
			ver += ".0"
		}
		fmt.Fprintf(&b, "# Go %s\n", ver)
		fmt.Fprintf(&b, "curl -fsSL --retry 3 --retry-delay 5 \"https://dl.google.com/go/go%s.linux-${ARCH}.tar.gz\" | tar -C /usr/local -xzf -\n\n", ver)
	}

	if ver, ok := toolchains["node"]; ok {
		// NodeSource uses the major version number only.
		major := strings.SplitN(ver, ".", 2)[0]
		fmt.Fprintf(&b, "# Node.js %s\n", ver)
		fmt.Fprintf(&b, "curl -fsSL --retry 3 --retry-delay 5 \"https://deb.nodesource.com/setup_%s.x\" | bash -\n", major)
		b.WriteString("apt-get install -y --no-install-recommends nodejs\n")
		b.WriteString("rm -rf /var/lib/apt/lists/*\n\n")
	}

	if ver, ok := toolchains["ruby"]; ok {
		fmt.Fprintf(&b, "# Ruby %s via ruby-build\n", ver)
		b.WriteString("apt-get update && apt-get install -y --no-install-recommends ruby-build\n")
		b.WriteString("rm -rf /var/lib/apt/lists/*\n")
		fmt.Fprintf(&b, "ruby-build \"%s\" /usr/local/ruby\n\n", ver)
	}

	if ver, ok := toolchains["java"]; ok {
		// Eclipse Temurin; use the major version (e.g., "21" from "21.0.3").
		major := strings.SplitN(ver, ".", 2)[0]
		fmt.Fprintf(&b, "# Java %s (Eclipse Temurin)\n", ver)
		b.WriteString("apt-get update && apt-get install -y --no-install-recommends wget apt-transport-https gnupg\n")
		b.WriteString("mkdir -p /etc/apt/keyrings\n")
		b.WriteString("wget -qO /etc/apt/keyrings/adoptium.asc https://packages.adoptium.net/artifactory/api/gpg/key/public\n")
		b.WriteString("echo \"deb [signed-by=/etc/apt/keyrings/adoptium.asc] " +
			"https://packages.adoptium.net/artifactory/deb " +
			"$(awk -F= '/^VERSION_CODENAME/{print$2}' /etc/os-release) main\" " +
			"| tee /etc/apt/sources.list.d/adoptium.list\n")
		fmt.Fprintf(&b, "apt-get update && apt-get install -y --no-install-recommends temurin-%s-jdk\n", major)
		b.WriteString("rm -rf /var/lib/apt/lists/*\n\n")
	}

	return b.String()
}

// GenerateDepsScript returns a shell script that installs each named dep in
// the provided list. Returns a no-op script when the list is empty. The script
// runs as root inside the Docker build context, after toolchains have been
// installed, so toolchain binaries (e.g. /usr/local/go/bin) are available.
//
// Supported deps: golangci-lint.
func GenerateDepsScript(deps []string) string {
	if len(deps) == 0 {
		return "#!/bin/sh\n# no deps declared\n"
	}

	var b strings.Builder
	b.WriteString("#!/bin/sh\nset -eu\n\n")
	// Make toolchain binaries installed by toolchains.sh visible.
	b.WriteString("export PATH=/usr/local/go/bin:${PATH}\n\n")

	for _, dep := range deps {
		switch dep {
		case "golangci-lint":
			b.WriteString("# golangci-lint\n")
			b.WriteString("n=0; until [ $n -ge 3 ]; do GOBIN=/usr/local/bin go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest && break; n=$((n+1)); echo \"go install failed, retry $n/3…\" >&2; sleep 5; done\n\n")
		default:
			fmt.Fprintf(&b, "echo \"WARNING: unknown dep %q — skipping\" >&2\n\n", dep)
		}
	}

	return b.String()
}

// prepareBuildContext creates a temp directory containing the full Python
// source tree (embedded in the binary via pysource.Source), the generated
// Dockerfile, and the toolchains install script. The caller is responsible
// for removing the directory when done.
func prepareBuildContext(toolchains map[string]string, deps []string, marketplaceClones string, printf func(string, ...any)) (string, error) {
	dockerfileContent, err := scaffold.TemplateContent("templates/Dockerfile")
	if err != nil {
		return "", err
	}

	tmp, err := os.MkdirTemp("", "contained-build-")
	if err != nil {
		return "", fmt.Errorf("creating build temp dir: %w", err)
	}

	// Write the embedded Python source tree (pyproject.toml + src/) to the
	// build context so the Dockerfile can COPY them into the image.
	if err := fs.WalkDir(pysource.Source, ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		dest := filepath.Join(tmp, path)
		if d.IsDir() {
			return os.MkdirAll(dest, 0o755)
		}
		data, err := fs.ReadFile(pysource.Source, path)
		if err != nil {
			return err
		}
		return os.WriteFile(dest, data, 0o644)
	}); err != nil {
		os.RemoveAll(tmp)
		return "", fmt.Errorf("writing python source to build context: %w", err)
	}

	if err := os.WriteFile(filepath.Join(tmp, "Dockerfile"), []byte(dockerfileContent), 0o644); err != nil {
		os.RemoveAll(tmp)
		return "", err
	}
	toolchainsScript := GenerateToolchainsScript(toolchains)
	if err := os.WriteFile(filepath.Join(tmp, "toolchains.sh"), []byte(toolchainsScript), 0o755); err != nil {
		os.RemoveAll(tmp)
		return "", err
	}
	depsScript := GenerateDepsScript(deps)
	if err := os.WriteFile(filepath.Join(tmp, "deps.sh"), []byte(depsScript), 0o755); err != nil {
		os.RemoveAll(tmp)
		return "", err
	}

	// Write hook scripts to the build context so the Dockerfile can COPY them
	// into /etc/contained/hooks/ in the image. Only .py files are included;
	// __pycache__ bytecode and other artefacts are excluded.
	if err := os.MkdirAll(filepath.Join(tmp, "hooks"), 0o755); err != nil {
		os.RemoveAll(tmp)
		return "", fmt.Errorf("creating hooks dir in build context: %w", err)
	}
	if err := fs.WalkDir(scaffold.Templates, "templates/hooks", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || filepath.Ext(path) != ".py" {
			return nil
		}
		data, err := fs.ReadFile(scaffold.Templates, path)
		if err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(tmp, "hooks", filepath.Base(path)), data, 0o644)
	}); err != nil {
		os.RemoveAll(tmp)
		return "", fmt.Errorf("writing hook scripts to build context: %w", err)
	}

	// Marketplace repos: clone on the host so Docker build needs no network.
	// The Dockerfile COPYs marketplaces/ and known_marketplaces.json from here.
	if err := os.MkdirAll(filepath.Join(tmp, "marketplaces"), 0o755); err != nil {
		os.RemoveAll(tmp)
		return "", fmt.Errorf("creating marketplaces dir: %w", err)
	}
	if marketplaceClones != "" {
		if err := prepareMarketplaces(tmp, marketplaceClones, printf); err != nil {
			os.RemoveAll(tmp)
			return "", fmt.Errorf("preparing marketplace repos: %w", err)
		}
	} else {
		// No marketplace clones — write empty map so the Dockerfile COPY always
		// has a known_marketplaces.json to pick up.
		if err := os.WriteFile(filepath.Join(tmp, "known_marketplaces.json"), []byte("{}"), 0o644); err != nil {
			os.RemoveAll(tmp)
			return "", fmt.Errorf("writing known_marketplaces.json: %w", err)
		}
	}

	return tmp, nil
}

// prepareMarketplaces clones each marketplace repo from GitHub into the build
// context, merges external_plugins/ into plugins/, strips .git/, and writes
// known_marketplaces.json so Claude Code can resolve plugin installs.
func prepareMarketplaces(buildContext, marketplaceClonesB64 string, printf func(string, ...any)) error {
	raw, err := base64.StdEncoding.DecodeString(marketplaceClonesB64)
	if err != nil {
		return fmt.Errorf("decoding marketplace clones: %w", err)
	}

	type mpEntry struct{ Name, Repo string }
	var entries []mpEntry
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		parts := strings.SplitN(line, ":", 2)
		if len(parts) == 2 && parts[0] != "" && parts[1] != "" {
			entries = append(entries, mpEntry{parts[0], parts[1]})
		}
	}

	knownMarketplaces := map[string]any{}
	mpBase := filepath.Join(buildContext, "marketplaces")

	for _, e := range entries {
		mpDir := filepath.Join(mpBase, e.Name)
		printf("  Cloning marketplace %s …\n", e.Name)
		cmd := exec.Command("git", "clone", "--depth", "1", "--quiet",
			"https://github.com/"+e.Repo+".git", mpDir)
		if out, cloneErr := cmd.CombinedOutput(); cloneErr != nil {
			return fmt.Errorf("cloning %s: %s", e.Repo, strings.TrimSpace(string(out)))
		}

		// Remove .git/ to keep build context small.
		_ = os.RemoveAll(filepath.Join(mpDir, ".git"))

		// Merge external_plugins/ into plugins/ so claude plugin install finds them.
		extDir := filepath.Join(mpDir, "external_plugins")
		pluginsDir := filepath.Join(mpDir, "plugins")
		if err := os.MkdirAll(pluginsDir, 0o755); err != nil {
			return fmt.Errorf("mkdir plugins for %s: %w", e.Name, err)
		}
		if info, err := os.Stat(extDir); err == nil && info.IsDir() {
			items, err := os.ReadDir(extDir)
			if err != nil {
				return fmt.Errorf("reading external_plugins for %s: %w", e.Name, err)
			}
			for _, item := range items {
				dst := filepath.Join(pluginsDir, item.Name())
				if _, statErr := os.Stat(dst); os.IsNotExist(statErr) {
					if err := copyDirTree(filepath.Join(extDir, item.Name()), dst); err != nil {
						return fmt.Errorf("copying %s: %w", item.Name(), err)
					}
				}
			}
		}

		knownMarketplaces[e.Name] = map[string]any{
			"source":          map[string]any{"source": "github", "repo": e.Repo},
			"installLocation": "/home/agent/.claude/plugins/marketplaces/" + e.Name,
			"lastUpdated":     "2026-01-01T00:00:00.000Z",
		}
	}

	data, err := json.Marshal(knownMarketplaces)
	if err != nil {
		return fmt.Errorf("marshalling known_marketplaces: %w", err)
	}
	return os.WriteFile(filepath.Join(buildContext, "known_marketplaces.json"), data, 0o644)
}

// copyDirTree recursively copies the directory tree rooted at src to dst.
func copyDirTree(src, dst string) error {
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			info, err := d.Info()
			if err != nil {
				return err
			}
			return os.MkdirAll(target, info.Mode())
		}
		return copyOneFile(path, target)
	})
}

// copyOneFile copies src to dst, preserving the source file's permission bits.
func copyOneFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	info, err := in.Stat()
	if err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, info.Mode())
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}

// ── helpers ──────────────────────────────────────────────────────────────────

func hostUIDGID() (string, string) {
	uid := fmt.Sprintf("%d", os.Getuid())
	gid := fmt.Sprintf("%d", os.Getgid())
	return uid, gid
}

func shortHash(s string) string {
	// Simple djb2-style hash — same length as the Python sha256[:16] approach.
	var h uint64 = 5381
	for _, c := range []byte(s) {
		h = h*33 + uint64(c)
	}
	return fmt.Sprintf("%016x", h)
}

func nestedMap(m map[string]any, keys ...string) (map[string]any, bool) {
	cur := m
	for _, k := range keys {
		v, ok := cur[k]
		if !ok {
			return nil, false
		}
		next, ok := v.(map[string]any)
		if !ok {
			return nil, false
		}
		cur = next
	}
	return cur, true
}

// InjectMaInlinedDomain appends the mAInlined hostname to
// m.Policy.Network.AllowedDomains when an explicit domain list is already
// configured, keeping the on-disk manifest in sync with managed-settings.json.
// No-op when the list is empty (BuildManagedSettings applies its own defaults
// and handles the injection there), when the domain is already present, or
// when the resolved URL is loopback.
func InjectMaInlinedDomain(m *manifest.Manifest) {
	if len(m.Runtime.Network.AllowedDomains) == 0 {
		return
	}
	host := maInlinedHostname(m)
	if host == "" {
		return
	}
	for _, d := range m.Runtime.Network.AllowedDomains {
		if d == host {
			return
		}
	}
	m.Runtime.Network.AllowedDomains = append(m.Runtime.Network.AllowedDomains, host)
}

// maInlinedHostname returns the non-loopback hostname from the mAInlined URL,
// preferring policy_yaml's policy.mAInlined.url (Docker-network alias) over
// mainlined.url (host-side bootstrap URL that may be localhost). Returns "".
func maInlinedHostname(m *manifest.Manifest) string {
	rawURL := extractPolicyMainlinedURL(m.Init.Mainlined.PolicyYAML)
	if rawURL == "" {
		rawURL = m.Init.Mainlined.URL
	}
	if rawURL == "" {
		return ""
	}
	u, err := url.Parse(rawURL)
	if err != nil {
		return ""
	}
	host := u.Hostname()
	if host == "" || host == "localhost" || host == "127.0.0.1" || host == "::1" {
		return ""
	}
	return host
}

// extractPolicyMainlinedURL parses the policy_yaml field of the manifest and
// returns the mAInlined URL declared inside it (policy.mAInlined.url), or ""
// if absent or unparseable. This URL is the Docker-network-reachable address
// (e.g. "http://mainlined:8080") as opposed to mainlined.url which is the
// host-side bootstrap URL and may point to localhost.
func extractPolicyMainlinedURL(policyYAML string) string {
	if policyYAML == "" {
		return ""
	}
	var raw map[string]any
	if err := yaml.Unmarshal([]byte(policyYAML), &raw); err != nil {
		return ""
	}
	pol, _ := raw["init"].(map[string]any)
	ml, _ := pol["mainlined"].(map[string]any)
	u, _ := ml["url"].(string)
	return u
}

func stringVal(v any) string {
	if v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}
