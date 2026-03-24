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
	Runtime RuntimeConfig `yaml:"runtime"`
	Agent   AgentConfig   `yaml:"agent"`
	Policy  PolicyConfig  `yaml:"policy"`
	Sandbox SandboxConfig `yaml:"sandbox"`
}

type RuntimeConfig struct {
	Mainlined MainlinedRuntimeConfig `yaml:"mainlined"`
	Docker    DockerConfig           `yaml:"docker"`
}

type MainlinedRuntimeConfig struct {
	URL string `yaml:"url"`
}

type DockerConfig struct {
	Image             string `yaml:"image"`
	Memory            string `yaml:"memory"`
	CPUs              int    `yaml:"cpus"`
	Network           string `yaml:"network"`
	AgentConfigVolume string `yaml:"agent_config_volume"`
}

type AgentConfig struct {
	Model        string `yaml:"model"`
	BudgetTokens int    `yaml:"budget_tokens"`
}

type PolicyConfig struct {
	Sigstore  SigstoreConfig  `yaml:"sigstore"`
	Secrets   SecretsConfig   `yaml:"secrets"`
	Bash      BashConfig      `yaml:"bash"`
	Network   NetworkConfig   `yaml:"network"`
	Audit     AuditConfig     `yaml:"audit"`
	QA        QAConfig        `yaml:"qa"`
	MCP       MCPConfig       `yaml:"mcp"`
	Skills    SkillsConfig    `yaml:"skills"`
	Mainlined MainlinedPolicy `yaml:"mainlined"`
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
	Enabled     bool `yaml:"enabled"`
	JSONLExport bool `yaml:"jsonl_export"`
}

type QAConfig struct {
	Checks []QACheck `yaml:"checks"`
}

type QACheck struct {
	Name        string   `yaml:"name"`
	Command     []string `yaml:"command"`
	WhenChanged []string `yaml:"when_changed"`
}

type MCPConfig struct {
	ApprovedServers []string `yaml:"approved_servers"`
}

type SkillsConfig struct {
	ApprovedSkills []string `yaml:"approved_skills"`
}

type MainlinedPolicy struct {
	URL           string `yaml:"url"`
	PolicyName    string `yaml:"policy_name"`
	PolicyRef     string `yaml:"policy_ref"`
	PolicyVersion string `yaml:"policy_version"`
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
}
