package docker

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"contained.dev/cli/internal/manifest"
	"contained.dev/cli/internal/scaffold"
	ver "contained.dev/cli/internal/version"
	"gopkg.in/yaml.v3"
)

var version = ver.Version

// FindSource walks up from cwd looking for the contAIned Python source tree
// (identified by the presence of src/contained/runtime/Dockerfile).
// Returns the directory path if found, empty string otherwise.
func FindSource() string {
	cwd, err := os.Getwd()
	if err != nil {
		return ""
	}
	current := filepath.Clean(cwd)
	for {
		candidate := filepath.Join(current, "src", "contained", "runtime", "Dockerfile")
		if _, err := os.Stat(candidate); err == nil {
			return current
		}
		parent := filepath.Dir(current)
		if parent == current {
			break
		}
		current = parent
	}
	return ""
}

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
			"allow":                       allowRules,
			"ask":                         []string{"WebFetch", "WebSearch"},
			"disableBypassPermissionsMode": "disable",
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
			"SubagentStart":   []any{map[string]any{"hooks": []any{h("subagent_start")}}},
			"SubagentStop":    []any{map[string]any{"hooks": []any{h("subagent_stop")}}},
			"Stop":            []any{map[string]any{"hooks": []any{h("summarizer")}}},
			"UserPromptSubmit": []any{map[string]any{"hooks": []any{h("user_prompt_submit")}}},
			"PermissionRequest": []any{map[string]any{"hooks": []any{h("permission_request")}}},
		},
		"sandbox": map[string]any{
			"enabled":                  true,
			"enableWeakerNestedSandbox": true,
			"allowUnsandboxedCommands": false,
			"network": map[string]any{
				"allowedDomains":        allowedDomains,
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

	out, err := json.MarshalIndent(settings, "", "  ")
	if err != nil {
		return "", fmt.Errorf("marshalling managed-settings: %w", err)
	}
	return string(out), nil
}

// PolicyPull attempts to fetch policy_ref and policy_version from the
// Mainlined instance and write them back into the manifest YAML.
// Falls back to the original content on any error (network unavailable,
// missing fields) so offline workflows are unaffected.
func PolicyPull(manifestContent string) string {
	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(manifestContent), &parsed); err != nil {
		return manifestContent
	}

	mainlined, _ := nestedMap(parsed, "policy", "mainlined")
	url := strings.TrimRight(stringVal(mainlined["url"]), "/")
	policyName := strings.TrimSpace(stringVal(mainlined["policy_name"]))
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

	if mainlined == nil {
		mainlined = map[string]any{}
	}
	mainlined["policy_ref"] = policyRef
	mainlined["policy_version"] = policyVersion

	// Write back into the parsed map.
	if pol, ok := parsed["policy"].(map[string]any); ok {
		pol["mainlined"] = mainlined
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
// source is the path to the contAIned Python source tree; empty string means
// install from PyPI.
func DockerSetup(
	cfg manifest.DockerConfig,
	workspace string,
	source string,
	rebuild bool,
	manifestContent string,
	managedSettingsContent string,
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

		// Determine build context and CONTAINED_PACKAGE arg.
		buildContext, containedPkg, err := prepareBuildContext(source)
		if err != nil {
			return false, err
		}
		if buildContext != source && buildContext != "" {
			// Temp dir — clean up after build.
			defer os.RemoveAll(buildContext)
		}

		printf("  Building image %s …", image)

		uid, gid := hostUIDGID()
		buildArgs := []string{
			dockerBin, "build",
			"--build-arg", "HOST_UID=" + uid,
			"--build-arg", "HOST_GID=" + gid,
			"--build-arg", "CONTAINED_PACKAGE=" + containedPkg,
			"--label", "contAIned.version=" + version,
			"--label", "contAIned.manifest_hash=" + manifestHash,
			"--build-arg", "MANIFEST_CONTENT=" + manifestB64,
			"--build-arg", "MANAGED_SETTINGS_CONTENT=" + settingsB64,
			"-t", image,
		}

		// Use the Dockerfile written by prepareBuildContext.
		dockerfileName := "Dockerfile"
		if source != "" {
			dockerfileName = ".contained-build.Dockerfile"
		}
		dockerfilePath := filepath.Join(buildContext, dockerfileName)
		defer os.Remove(dockerfilePath) // clean up temp Dockerfile in source tree
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

// prepareBuildContext returns (contextDir, containedPackageArg, error).
// If source is non-empty (local source tree), it is used as the context.
// Otherwise a temp dir is created containing just the embedded Dockerfile,
// and containedPackageArg is "contained[dev]" for PyPI install.
func prepareBuildContext(source string) (string, string, error) {
	dockerfileContent, err := scaffold.TemplateContent("templates/Dockerfile")
	if err != nil {
		return "", "", err
	}

	if source != "" {
		// Local source mode: use the source tree as the build context so
		// COPY . /opt/contAIned works. Write the Dockerfile into it temporarily;
		// the caller is responsible for removing the temp file after the build.
		// We use a unique name to avoid colliding with any existing Dockerfile.
		dfPath := filepath.Join(source, ".contained-build.Dockerfile")
		if err := os.WriteFile(dfPath, []byte(dockerfileContent), 0o644); err != nil {
			return "", "", fmt.Errorf("writing Dockerfile to source: %w", err)
		}
		// Signal to caller: context is source (don't RemoveAll), but Dockerfile
		// is the temp file we just wrote.
		return source, "/opt/contAIned[dev]", nil
	}

	// PyPI mode: temp dir with just the Dockerfile.
	tmp, err := os.MkdirTemp("", "contained-build-")
	if err != nil {
		return "", "", fmt.Errorf("creating build temp dir: %w", err)
	}
	if err := os.WriteFile(filepath.Join(tmp, "Dockerfile"), []byte(dockerfileContent), 0o644); err != nil {
		os.RemoveAll(tmp)
		return "", "", err
	}
	return tmp, "contained[dev]", nil
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
