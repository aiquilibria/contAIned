// Package manifest loads and parses the contAIned manifest.yaml.
package manifest

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// Manifest is the top-level structure of .contAIned/manifest.yaml.
type Manifest struct {
	Runtime              RuntimeConfig           `yaml:"runtime"`
	Agent                AgentConfig             `yaml:"agent"`
	Policy               PolicyConfig            `yaml:"policy"`
	EcosystemDefinitions map[string]EcosystemDef `yaml:"ecosystem_definitions,omitempty"`
	Mainlined            MainlinedSection        `yaml:"mainlined,omitempty"`
}

// MainlinedSection records the mAInlined registration result baked into the
// manifest so enforcement hooks can operate entirely offline.
type MainlinedSection struct {
	URL           string `yaml:"url,omitempty"`
	PolicyRef     string `yaml:"policy_ref,omitempty"`
	PolicyVersion string `yaml:"policy_version,omitempty"`
	PolicyYAML    string `yaml:"policy_yaml,omitempty"`
}

// EcosystemDef describes what an ecosystem label means in terms of toolchain
// installation and network access. Operators define these in the mAInlined
// manifest; repositories declare which ecosystems they use via RepoManifest.Ecosystems.
type EcosystemDef struct {
	// Toolchain is the runtime.docker.toolchains key to install (e.g. "go",
	// "node"). Empty means the runtime is pre-installed in the base image.
	Toolchain string `yaml:"toolchain,omitempty"`
	// Deps lists additional tools or packages to pre-install in the container
	// image for this ecosystem (e.g. ["golangci-lint"] for Go).
	Deps []string `yaml:"deps,omitempty"`
	// Install is a command run once in the workspace before QA checks execute,
	// each time the agent stops. Use for workspace-level setup that must happen
	// inside the container on the target platform (e.g. ["npm", "install"]).
	Install []string `yaml:"install,omitempty"`
	// NetworkDomains lists the outbound domains required for dependency
	// resolution and package fetching for this ecosystem.
	NetworkDomains []string `yaml:"network_domains,omitempty"`
	// Env holds environment variables that must be set inside the container
	// when this ecosystem is active. These are injected into managed-settings.json
	// so the Claude Code sandbox propagates them to every Bash subprocess.
	Env map[string]string `yaml:"env,omitempty"`
	// Plugins lists plugins to pre-install when this ecosystem is active.
	// Combined with policy.plugins.preinstall at contained init time and
	// deduplicated before baking into the image.
	Plugins []PluginRef `yaml:"plugins,omitempty"`
}

type RuntimeConfig struct {
	mAInlined mAInlinedRuntimeConfig `yaml:"mAInlined"`
	Docker    DockerConfig           `yaml:"docker"`
}

type mAInlinedRuntimeConfig struct {
	URL string `yaml:"url"`
}

// ExtraSecret declares a secret file on the host that is bind-mounted into
// the container and exported as an environment variable by the entrypoint.
// The file is mounted read-only at /run/contained/secrets-env/<Env> so the
// value never appears in docker run arguments or docker inspect output.
type ExtraSecret struct {
	// Path is the host-side file that holds the secret value.
	// A leading ~ is expanded to the operator's home directory.
	// Example: ~/.contained/secrets/github_token
	Path string `yaml:"path"`
	// Env is the environment variable name to export inside the container.
	// Example: GITHUB_PERSONAL_ACCESS_TOKEN
	Env string `yaml:"env"`
	// NetrcMachine, when non-empty, causes the entrypoint to write a ~/.netrc
	// entry for this machine using the exported env var as the password.
	// This covers git/curl HTTPS authentication without a separately mounted
	// .netrc file. Example: github.com
	NetrcMachine string `yaml:"netrc_machine,omitempty"`
}

type DockerConfig struct {
	Image             string            `yaml:"image"`
	Memory            string            `yaml:"memory"`
	CPUs              int               `yaml:"cpus"`
	Network           string            `yaml:"network"`
	AgentConfigVolume string            `yaml:"agent_config_volume"`
	Toolchains        map[string]string `yaml:"toolchains,omitempty"`
	// ExtraMounts lists additional host:container[:options] bind mounts passed
	// to docker run. A leading ~ is expanded to the operator's home directory.
	// Example: ["~/.ssh:/home/agent/.ssh:ro"]
	ExtraMounts []string `yaml:"extra_mounts,omitempty"`
	// ExtraSecrets lists secrets to inject without exposing them as docker run
	// --env flags. Each secret file is bind-mounted read-only and exported as
	// an environment variable by the container entrypoint.
	ExtraSecrets []ExtraSecret `yaml:"extra_secrets,omitempty"`
	// Env holds merged environment variables collected from active ecosystems.
	// Populated by MergeRepoManifest; not intended for direct YAML authoring.
	Env map[string]string `yaml:"env,omitempty"`
	// Deps lists additional tools collected from active ecosystem definitions.
	// Populated by MergeRepoManifest; not intended for direct YAML authoring.
	Deps []string `yaml:"deps,omitempty"`
}

type AgentConfig struct {
	Model        string `yaml:"model"`
	BudgetTokens int    `yaml:"budget_tokens"`
}

type PolicyConfig struct {
	// Rules is the Phase 2+ unified policy rule list (Cedar-inspired YAML format).
	// When present, the legacy Secrets/Bash/Network sections are ignored by the
	// engine (they continue to be parsed for backwards-compat tooling).
	// When absent, the engine's compat adapter translates the legacy sections.
	Rules     []PolicyRule    `yaml:"rules,omitempty"`
	Sigstore  SigstoreConfig  `yaml:"sigstore"`
	Secrets   SecretsConfig   `yaml:"secrets"`
	Bash      BashConfig      `yaml:"bash"`
	Network   NetworkConfig   `yaml:"network"`
	Audit     AuditConfig     `yaml:"audit"`
	QA        QAConfig        `yaml:"qa"`
	MCP       MCPConfig       `yaml:"mcp"`
	Skills    SkillsConfig    `yaml:"skills"`
	Plugins   PluginsConfig   `yaml:"plugins,omitempty"`
	Sandbox   SandboxConfig   `yaml:"sandbox"`
	mAInlined mAInlinedPolicy `yaml:"mAInlined"`
}

// PolicyRule is a single Cedar-inspired rule in the unified policy.rules list.
// The unified format replaces the fragmented secrets/bash/network sections.
// Run `contained migrate` to convert an existing manifest to this format.
type PolicyRule struct {
	// ID uniquely identifies the rule. Use the form "v1:<section>:<name>"
	// (e.g. "v1:secrets:dotenv"). Must be unique across all rules.
	ID string `yaml:"id"`
	// Effect is one of "permit", "forbid", or "escalate".
	Effect string `yaml:"effect"`
	// Action lists the tool names this rule applies to (e.g. ["Read", "Glob"]).
	// Use "*" to match any action.
	Action interface{} `yaml:"action"` // string or []string
	// ResourceType restricts the rule to a specific entity type
	// (FilePath, GlobPattern, BashCommand, NetworkResource, or "*").
	ResourceType string `yaml:"resource_type"`
	// When is a list of conditions that must ALL hold for the rule to match.
	When []string `yaml:"when,omitempty"`
	// Unless is a list of conditions where ANY true value negates the rule.
	Unless []string `yaml:"unless,omitempty"`
	// Reason is a human-readable explanation shown when the rule triggers.
	Reason string `yaml:"reason,omitempty"`
	// Tags are arbitrary labels for filtering and audit queries.
	Tags []string `yaml:"tags,omitempty"`
	// Define holds the attribute patterns for effect:define classifier rules.
	// Preserved as a raw map so arbitrary attribute names (is_secret, in_tmp,
	// etc.) round-trip through parse→merge→serialise without loss.
	Define map[string]any `yaml:"define,omitempty"`
}

type SigstoreConfig struct {
	Enabled   bool   `yaml:"enabled"`
	RekorURL  string `yaml:"rekor_url"`
	FulcioURL string `yaml:"fulcio_url"`
}

type SecretsConfig struct {
	Rules []Rule `yaml:"rules"`
}

type BashConfig struct {
	Rules []Rule `yaml:"rules"`
}

// Rule is a named pattern-action entry used in secrets and bash policy blocks.
type Rule struct {
	Name     string   `yaml:"name"`
	Patterns []string `yaml:"patterns"`
	Reason   string   `yaml:"reason,omitempty"`
	Action   string   `yaml:"action"` // "allow", "block", "escalate"
}

type NetworkConfig struct {
	Enabled        bool     `yaml:"enabled"`
	AllowedDomains []string `yaml:"allowed_domains"`
}

type AuditConfig struct {
	Enabled bool `yaml:"enabled"`
}

type QAConfig struct {
	// Setup lists commands run before any checks on every QA pass.
	// Populated by MergeRepoManifest from active ecosystem Install commands;
	// may also be authored directly in the operator manifest.
	Setup  [][]string `yaml:"setup,omitempty"`
	Checks []QACheck  `yaml:"checks"`
}

type QACheck struct {
	Name        string   `yaml:"name"`
	Command     []string `yaml:"command"`
	WhenChanged []string `yaml:"when_changed"`
}

type MCPConfig struct {
	ApprovedServers []string `yaml:"approved_servers"`
}

// PluginMarketplace identifies a single approved marketplace source.
// It maps directly to the Claude Code marketplace source object format.
type PluginMarketplace struct {
	// Source is the marketplace type: "builtin", "github", "npm", "hostPattern".
	Source string `yaml:"source"`
	// Repo is the GitHub repository (owner/name) when Source is "github".
	Repo string `yaml:"repo,omitempty"`
	// Ref is an optional git ref (branch, tag, commit) when Source is "github".
	Ref string `yaml:"ref,omitempty"`
	// Path is an optional subdirectory within the repo when Source is "github".
	Path string `yaml:"path,omitempty"`
	// HostPattern is a regex matched against the hostname when Source is "hostPattern".
	HostPattern string `yaml:"host_pattern,omitempty"`
	// Package is the npm package name when Source is "npm".
	Package string `yaml:"package,omitempty"`
}

// PluginRef identifies a single plugin within a named marketplace.
type PluginRef struct {
	// Marketplace is the name of the registered marketplace (matches an entry
	// in extraKnownMarketplaces or the builtin marketplace identifier).
	Marketplace string `yaml:"marketplace"`
	// Plugin is the plugin name within that marketplace.
	Plugin string `yaml:"plugin"`
}

// PluginsConfig is the policy.plugins section of the manifest.
type PluginsConfig struct {
	// StrictMarketplaces enables strictKnownMarketplaces enforcement in
	// managed-settings.json. When true, only sources in the resolved list
	// (builtin + ExtraMarketplaces) may be added; all others are blocked
	// before any network or filesystem operation. Default: false.
	StrictMarketplaces bool `yaml:"strict_marketplaces,omitempty"`
	// BuiltinMarketplace controls whether the Anthropic official marketplace
	// is included in the resolved source list. nil is treated as true.
	// Set to false to restrict the agent to operator-controlled sources only.
	BuiltinMarketplace *bool `yaml:"builtin_marketplace,omitempty"`
	// ExtraMarketplaces lists additional approved marketplace sources.
	// These are always pre-registered via extraKnownMarketplaces so they are
	// available without a manual /plugin marketplace add step.
	ExtraMarketplaces []PluginMarketplace `yaml:"extra_marketplaces,omitempty"`
	// Preinstall lists plugins to bake into the container image at
	// contained init time. Available from the first session without any
	// manual install step.
	Preinstall []PluginRef `yaml:"preinstall,omitempty"`
}

type SkillsConfig struct {
	ApprovedSkills []string `yaml:"approved_skills"`
}

type mAInlinedPolicy struct {
	URL           string `yaml:"url"`
	PolicyName    string `yaml:"policy_name"`
	PolicyRef     string `yaml:"policy_ref"`
	PolicyVersion string `yaml:"policy_version"`
}

// ExtractPolicyVersion extracts policy.mAInlined.policy_version from a raw
// manifest YAML string via unstructured map access. This is necessary because
// PolicyConfig.mAInlined is an unexported field that yaml.v3 cannot unmarshal
// into via reflection.
func ExtractPolicyVersion(yamlContent string) string {
	var raw map[string]any
	if err := yaml.Unmarshal([]byte(yamlContent), &raw); err != nil {
		return ""
	}
	policy, _ := raw["policy"].(map[string]any)
	mAInlined, _ := policy["mAInlined"].(map[string]any)
	v, _ := mAInlined["policy_version"].(string)
	return v
}

type SandboxConfig struct {
	Enabled    bool            `yaml:"enabled"`
	Filesystem FilesystemRules `yaml:"filesystem"`
}

type FilesystemRules struct {
	DenyWrite []string `yaml:"denyWrite"`
}

// Parse unmarshals raw YAML bytes into a Manifest, applies defaults, but does
// not validate. Use Validate separately when you want to report errors.
func Parse(data []byte) (*Manifest, error) {
	var m Manifest
	if err := yaml.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("parsing manifest: %w", err)
	}
	applyDefaults(&m)
	return &m, nil
}

// Serialise marshals a Manifest back to YAML bytes.
func Serialise(m *Manifest) (string, error) {
	out, err := yaml.Marshal(m)
	if err != nil {
		return "", fmt.Errorf("serialising manifest: %w", err)
	}
	return string(out), nil
}

// Load reads and parses the manifest from root/.contAIned/manifest.yaml.
// It also accepts the legacy path root/.contAIned/policy/manifest.yaml.
// Returns an error if no manifest file is found.
func Load(root string) (*Manifest, error) {
	newPath := filepath.Join(root, ".contAIned", "manifest.yaml")
	oldPath := filepath.Join(root, ".contAIned", "policy", "manifest.yaml")

	path := newPath
	if _, err := os.Stat(newPath); os.IsNotExist(err) {
		if _, err2 := os.Stat(oldPath); err2 == nil {
			path = oldPath
		} else {
			return nil, fmt.Errorf("no manifest found at %s (run 'contained init --manifest <path>')", newPath)
		}
	}

	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading manifest: %w", err)
	}

	var m Manifest
	if err := yaml.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("parsing manifest: %w", err)
	}

	applyDefaults(&m)
	return &m, nil
}

// applyDefaults fills in zero-value fields with sensible defaults so callers
// don't need to guard against empty strings.
func applyDefaults(m *Manifest) {
	if m.Runtime.Docker.Image == "" {
		m.Runtime.Docker.Image = "contained:latest"
	}
	if m.Runtime.Docker.Memory == "" {
		m.Runtime.Docker.Memory = "2g"
	}
	if m.Runtime.Docker.CPUs == 0 {
		m.Runtime.Docker.CPUs = 2
	}
	if m.Runtime.Docker.Network == "" {
		m.Runtime.Docker.Network = "contAIned-net"
	}
	if m.Runtime.Docker.AgentConfigVolume == "" {
		m.Runtime.Docker.AgentConfigVolume = "contAIned-agent-config"
	}
	if m.Policy.Sigstore.RekorURL == "" {
		m.Policy.Sigstore.RekorURL = "https://rekor.sigstore.dev"
	}
	if m.Policy.Sigstore.FulcioURL == "" {
		m.Policy.Sigstore.FulcioURL = "https://fulcio.sigstore.dev"
	}
	if m.Policy.Plugins.BuiltinMarketplace == nil {
		t := true
		m.Policy.Plugins.BuiltinMarketplace = &t
	}
}
