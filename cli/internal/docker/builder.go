package docker

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net/http"
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

// BuildManagedSettings generates the managed-settings.json content that is
// baked into the Docker image. The dynamic sections (domain allow-list, MCP
// server permissions, skill permissions) are derived from the manifest.
func BuildManagedSettings(m *manifest.Manifest) (string, error) {
	allowedDomains := m.Policy.Network.AllowedDomains
	if len(allowedDomains) == 0 {
		allowedDomains = []string{
			"api.anthropic.com",
			"code.claude.com",
			"docs.anthropic.com",
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
	for _, server := range m.Policy.MCP.ApprovedServers {
		allowRules = append(allowRules, "mcp__"+server+"__*")
	}
	for _, skill := range m.Policy.Skills.ApprovedSkills {
		allowRules = append(allowRules, "Skill("+skill+")")
	}

	hookCmd := "/opt/contained-venv/bin/python3 /workspace/.contAIned/hooks/%s.py"
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
	if len(m.Runtime.Docker.Env) > 0 {
		settings["env"] = m.Runtime.Docker.Env
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

	mAInlined, _ := nestedMap(parsed, "policy", "mAInlined")
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
	if pol, ok := parsed["policy"].(map[string]any); ok {
		pol["mAInlined"] = mAInlined
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
	cfg manifest.DockerConfig,
	workspace string,
	rebuild bool,
	manifestContent string,
	managedSettingsContent string,
	claudeMdContent string,
	printf func(string, ...any),
) (bool, error) {
	dockerBin, err := findDocker()
	if err != nil {
		return false, err
	}

	image := cfg.Image
	manifestHash := shortHash(manifestContent)
	manifestB64 := base64.StdEncoding.EncodeToString([]byte(manifestContent))
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

		buildContext, err := prepareBuildContext(cfg.Toolchains)
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
			"--build-arg", "MANIFEST_CONTENT=" + manifestB64,
			"--build-arg", "MANAGED_SETTINGS_CONTENT=" + settingsB64,
			"--build-arg", "CLAUDE_MD_CONTENT=" + claudeMdB64,
			"-t", image,
		}

		dockerfilePath := filepath.Join(buildContext, "Dockerfile")
		buildArgs = append(buildArgs, "-f", dockerfilePath, buildContext)

		result, err := exec.Command(buildArgs[0], buildArgs[1:]...).CombinedOutput()
		if err != nil {
			printf(" failed\n")
			return false, fmt.Errorf("docker build failed:\n%s", string(result))
		}
		printf(" done\n")
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
		fmt.Fprintf(&b, "# Go %s\n", ver)
		fmt.Fprintf(&b, "curl -fsSL \"https://go.dev/dl/go%s.linux-${ARCH}.tar.gz\" | tar -C /usr/local -xzf -\n\n", ver)
	}

	if ver, ok := toolchains["node"]; ok {
		// NodeSource uses the major version number only.
		major := strings.SplitN(ver, ".", 2)[0]
		fmt.Fprintf(&b, "# Node.js %s\n", ver)
		fmt.Fprintf(&b, "curl -fsSL \"https://deb.nodesource.com/setup_%s.x\" | bash -\n", major)
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

// prepareBuildContext creates a temp directory containing the full Python
// source tree (embedded in the binary via pysource.Source), the generated
// Dockerfile, and the toolchains install script. The caller is responsible
// for removing the directory when done.
func prepareBuildContext(toolchains map[string]string) (string, error) {
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
	return tmp, nil
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

func stringVal(v any) string {
	if v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}
