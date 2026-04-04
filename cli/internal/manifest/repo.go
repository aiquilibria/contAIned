package manifest

import (
	"bytes"
	"encoding/base64"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// RepoManifest is the restricted manifest a repository may commit to
// .contAIned_manifest.yaml at the repo root. Only two top-level keys are
// permitted: ecosystems (version field only) and runtime.qa.checks. All other
// settings are owned by the operator manifest and cannot be overridden here.
//
// Ecosystems declares the language runtimes this repository needs. Each key is
// an ecosystem name defined in the operator manifest's ecosystems map. Only the
// Version field may be set; all other EcosystemDef fields are operator-only and
// are rejected by ValidateRepoManifest. At `contained init` time each declaration
// is resolved to a toolchain install target and the required network domains are
// added to the allowlist automatically.
type RepoManifest struct {
	Ecosystems map[string]EcosystemDef `yaml:"ecosystems,omitempty"`
	Runtime    RepoRuntime             `yaml:"runtime"`
}

// RepoRuntime is the runtime section of a repo manifest — QA checks only.
type RepoRuntime struct {
	QA QAConfig `yaml:"qa"`
}

// LoadRepoManifest reads the repo-level manifest from root/.contAIned_manifest.yaml.
// Returns nil (not an error) if the file does not exist — absence is valid.
// Returns an error if the file contains fields outside the permitted schema.
func LoadRepoManifest(root string) (*RepoManifest, error) {
	path := filepath.Join(root, ".contAIned_manifest.yaml")
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading repo manifest: %w", err)
	}

	var r RepoManifest
	dec := yaml.NewDecoder(bytes.NewReader(data))
	dec.KnownFields(true)
	if err := dec.Decode(&r); err != nil && err != io.EOF {
		return nil, fmt.Errorf("repo manifest contains disallowed fields "+
			"(only ecosystems.version and runtime.qa.checks are permitted): %w", err)
	}

	return &r, nil
}

// ValidateRepoManifest checks the structure of a repo manifest.
// Ecosystem name resolution against operator definitions happens in
// MergeRepoManifest, which has access to both manifests.
func ValidateRepoManifest(r *RepoManifest) error {
	for name, def := range r.Ecosystems {
		if strings.TrimSpace(name) == "" {
			return fmt.Errorf("ecosystems: ecosystem name must not be empty")
		}
		if err := validateRepoEcosystemDef(name, def); err != nil {
			return err
		}
	}

	for i, check := range r.Runtime.QA.Checks {
		if check.Name == "" {
			return fmt.Errorf("runtime.qa.checks[%d].name is required", i)
		}
		if len(check.Command) == 0 {
			return fmt.Errorf("runtime.qa.checks[%d].command is required", i)
		}
	}

	return nil
}

// validateRepoEcosystemDef rejects any EcosystemDef entry in a repo manifest
// that sets operator-only fields. Repos may only set Version; all other fields
// (Toolchain, Deps, NetworkDomains, Env, Install, Plugins) are operator-owned.
// Note: KnownFields(true) on the YAML decoder accepts these fields because they
// are valid EcosystemDef struct fields, so this explicit check is required.
func validateRepoEcosystemDef(name string, def EcosystemDef) error {
	if def.Toolchain != "" {
		return fmt.Errorf("ecosystems.%s: toolchain is operator-only and may not be set in a repo manifest", name)
	}
	if len(def.Deps) > 0 {
		return fmt.Errorf("ecosystems.%s: deps is operator-only and may not be set in a repo manifest", name)
	}
	if len(def.NetworkDomains) > 0 {
		return fmt.Errorf("ecosystems.%s: network_domains is operator-only and may not be set in a repo manifest", name)
	}
	if len(def.Env) > 0 {
		return fmt.Errorf("ecosystems.%s: env is operator-only and may not be set in a repo manifest", name)
	}
	if len(def.Install) > 0 {
		return fmt.Errorf("ecosystems.%s: install is operator-only and may not be set in a repo manifest", name)
	}
	if len(def.Plugins) > 0 {
		return fmt.Errorf("ecosystems.%s: plugins is operator-only and may not be set in a repo manifest", name)
	}
	return nil
}

// MergeRepoManifest merges the repository manifest into the operator manifest
// and returns the merged result. The original manifests are not modified.
//
// Ecosystems: each entry in repo.Ecosystems is resolved against the operator
// manifest's ecosystems map. The repo's Version pin must satisfy any constraint
// the operator set on that ecosystem, and must satisfy any floor constraint on
// the underlying toolchain. Required network domains are always added to the
// allowlist regardless of network.enabled.
//
// QA checks: concatenated (operator first, repo second). Either or both may be empty.
//
// If repo is nil the function returns a copy of the operator manifest unchanged.
func MergeRepoManifest(operator *Manifest, repo *RepoManifest) (*Manifest, error) {
	// Shallow-copy the operator manifest.
	merged := *operator

	// Deep-copy mutable fields so we don't mutate the originals.
	// operator.Init.Container.Toolchains holds floor *constraints* (e.g. ">=1.22"),
	// not install versions. Convert each to its minimum concrete version so that
	// toolchains are always installed with a valid version string even when no
	// repo manifest overrides the default.
	merged.Init.Container.Toolchains = make(map[string]string)
	for k, v := range operator.Init.Container.Toolchains {
		merged.Init.Container.Toolchains[k] = minVersionFromConstraint(v)
	}
	merged.Runtime.Network.AllowedDomains = append([]string{}, operator.Runtime.Network.AllowedDomains...)
	merged.Init.Container.Deps = append([]string{}, operator.Init.Container.Deps...)
	merged.Runtime.QA.Setup = append([][]string{}, operator.Runtime.QA.Setup...)

	if repo == nil {
		return &merged, nil
	}

	// Resolve ecosystem declarations to toolchain installs + network domains + env vars.
	existing := domainSet(merged.Runtime.Network.AllowedDomains)
	for ecoName, repoDef := range repo.Ecosystems {
		def, ok := operator.Ecosystems[ecoName]
		if !ok {
			return nil, fmt.Errorf(
				"ecosystem %q is not defined in the operator manifest "+
					"(add an ecosystems.%s entry to your manifest.yaml)",
				ecoName, ecoName,
			)
		}

		ver := strings.TrimSpace(repoDef.Version)

		// Enforce any ecosystem-level version constraint the operator set.
		if def.Version != "" && ver != "" {
			sat, err := satisfiesConstraint(def.Version, ver)
			if err != nil {
				return nil, fmt.Errorf("ecosystem %q: %w", ecoName, err)
			}
			if !sat {
				return nil, fmt.Errorf(
					"ecosystem %q: version %q does not satisfy operator constraint %q",
					ecoName, ver, def.Version,
				)
			}
		}

		// Install toolchain if this ecosystem has one and a version was specified.
		if def.Toolchain != "" && ver != "" {
			// Also enforce any floor constraint on the underlying toolchain.
			if constraint, exists := operator.Init.Container.Toolchains[def.Toolchain]; exists {
				sat, err := satisfiesConstraint(constraint, ver)
				if err != nil {
					return nil, fmt.Errorf("ecosystem %q toolchain %q: %w", ecoName, def.Toolchain, err)
				}
				if !sat {
					return nil, fmt.Errorf(
						"ecosystem %q: version %q does not satisfy operator toolchain constraint %q for %q",
						ecoName, ver, constraint, def.Toolchain,
					)
				}
			}
			merged.Init.Container.Toolchains[def.Toolchain] = ver
		}

		// Always add ecosystem network domains regardless of network.enabled.
		// The enabled flag gates hook enforcement; the domain list is also consumed
		// by the sandbox allowedDomains filter (OS-level), which must include
		// package-registry domains so that toolchain installs (pip, go get, …) work.
		for _, domain := range def.NetworkDomains {
			if !existing[domain] {
				merged.Runtime.Network.AllowedDomains = append(merged.Runtime.Network.AllowedDomains, domain)
				existing[domain] = true
			}
		}

		// Merge ecosystem env vars (last writer wins for duplicate keys).
		if len(def.Env) > 0 {
			if merged.Init.Container.Env == nil {
				merged.Init.Container.Env = make(map[string]string)
			}
			for k, v := range def.Env {
				merged.Init.Container.Env[k] = v
			}
		}

		// Collect ecosystem deps (deduplicated).
		if len(def.Deps) > 0 {
			seen := sliceSet(merged.Init.Container.Deps)
			for _, dep := range def.Deps {
				if !seen[dep] {
					merged.Init.Container.Deps = append(merged.Init.Container.Deps, dep)
					seen[dep] = true
				}
			}
		}

		// Collect ecosystem install command (deduplicated by command string).
		if len(def.Install) > 0 {
			key := strings.Join(def.Install, " ")
			already := false
			for _, cmd := range merged.Runtime.QA.Setup {
				if strings.Join(cmd, " ") == key {
					already = true
					break
				}
			}
			if !already {
				merged.Runtime.QA.Setup = append(merged.Runtime.QA.Setup, def.Install)
			}
		}
	}

	// Concatenate QA checks: operator first, then repo.
	merged.Runtime.QA.Checks = append(
		append([]QACheck{}, operator.Runtime.QA.Checks...),
		repo.Runtime.QA.Checks...,
	)

	return &merged, nil
}

// CollectPlugins returns the deduplicated list of plugins to pre-install at
// image build time. It combines:
//  1. operator.Policy.Plugins.Preinstall — the global always-installed set.
//  2. The Plugins list from each EcosystemDef declared in repo.Ecosystems.
//
// Deduplication is by "marketplace:plugin" key; first occurrence wins.
// repo may be nil, in which case only the preinstall list is returned.
func CollectPlugins(operator *Manifest, repo *RepoManifest) []PluginRef {
	seen := make(map[string]bool)
	var collected []PluginRef

	add := func(p PluginRef) {
		key := p.Marketplace + ":" + p.Plugin
		if !seen[key] {
			seen[key] = true
			collected = append(collected, p)
		}
	}

	for _, p := range operator.Init.Plugins.Preinstall {
		add(p)
	}

	if repo != nil {
		for ecoName := range repo.Ecosystems {
			if def, ok := operator.Ecosystems[ecoName]; ok {
				for _, p := range def.Plugins {
					add(p)
				}
			}
		}
	}

	return collected
}

// MarketplaceClone holds the data needed to pre-clone a marketplace repo
// during Docker build so that plugins with relative-path sources resolve.
type MarketplaceClone struct {
	Name string // marketplace slug, e.g. "claude-plugins-official"
	Repo string // GitHub "owner/repo", e.g. "anthropics/claude-plugins-official"
}

// CollectMarketplaceClones returns the deduplicated set of GitHub-hosted
// marketplace repos that need to be cloned into the image so that
// "plugin@marketplace" references with relative-path sources (e.g.
// external_plugins/github) resolve during docker build.
//
// Rules:
//   - claude-plugins-official is included when builtin_marketplace is true
//     and at least one plugin referencing it is in the install list.
//   - Each extra_marketplace with source "github" is included when at least
//     one plugin referencing it is in the install list.
func CollectMarketplaceClones(operator *Manifest, plugins []PluginRef) []MarketplaceClone {
	// Build a set of marketplace names actually used by the plugin list.
	used := make(map[string]bool, len(plugins))
	for _, p := range plugins {
		used[p.Marketplace] = true
	}

	seen := make(map[string]bool)
	var clones []MarketplaceClone
	add := func(c MarketplaceClone) {
		if !seen[c.Name] {
			seen[c.Name] = true
			clones = append(clones, c)
		}
	}

	p := operator.Init.Plugins
	if p.BuiltinMarketplace != nil && *p.BuiltinMarketplace && used["claude-plugins-official"] {
		add(MarketplaceClone{
			Name: "claude-plugins-official",
			Repo: "anthropics/claude-plugins-official",
		})
	}
	for _, mp := range p.ExtraMarketplaces {
		if mp.Source != "github" || mp.Repo == "" {
			continue
		}
		name := marketplaceCloneKey(mp)
		if used[name] {
			add(MarketplaceClone{Name: name, Repo: mp.Repo})
		}
	}
	return clones
}

// marketplaceCloneKey derives the marketplace slug from a PluginMarketplace,
// matching the key used in extraKnownMarketplaces / known_marketplaces.json.
func marketplaceCloneKey(mp PluginMarketplace) string {
	// Reuse the same derivation as marketplaceKey in builder.go.
	switch mp.Source {
	case "github":
		return strings.ReplaceAll(mp.Repo, "/", "-")
	default:
		return mp.Source
	}
}

// encodeLinesArg encodes a non-empty slice of strings as a base64-encoded
// newline-delimited value suitable for Docker build args. Returns "" when
// lines is empty. trailingNewline should be true for args consumed by
// "while IFS=… read" loops in the Dockerfile — POSIX read silently skips a
// final line with no terminating newline.
func encodeLinesArg(lines []string, trailingNewline bool) string {
	if len(lines) == 0 {
		return ""
	}
	raw := strings.Join(lines, "\n")
	if trailingNewline {
		raw += "\n"
	}
	return base64.StdEncoding.EncodeToString([]byte(raw))
}

// EncodeMarketplaceClonesArg encodes a MarketplaceClone list as a
// base64-encoded newline-delimited "name:owner/repo" string suitable for
// passing as the MARKETPLACE_CLONES Docker build arg. Returns "" when empty.
func EncodeMarketplaceClonesArg(clones []MarketplaceClone) string {
	lines := make([]string, len(clones))
	for i, c := range clones {
		lines[i] = c.Name + ":" + c.Repo
	}
	return encodeLinesArg(lines, true)
}

// EncodeNetrcFromSecretsArg encodes the subset of extra_secrets that have a
// NetrcMachine field as a base64-encoded newline-delimited "machine:ENV_VAR"
// string suitable for passing as the NETRC_FROM_SECRETS Docker build arg.
// Returns "" when no secrets have NetrcMachine set.
func EncodeNetrcFromSecretsArg(secrets []ExtraSecret) string {
	var lines []string
	for _, s := range secrets {
		if s.NetrcMachine != "" && s.Env != "" {
			lines = append(lines, s.NetrcMachine+":"+s.Env)
		}
	}
	return encodeLinesArg(lines, true)
}

// EncodePluginsArg encodes a plugin list as a base64-encoded newline-delimited
// "marketplace:plugin" string suitable for passing as the PLUGINS_TO_INSTALL
// Docker build arg. Returns "" when the list is empty.
func EncodePluginsArg(plugins []PluginRef) string {
	lines := make([]string, len(plugins))
	for i, p := range plugins {
		lines[i] = p.Marketplace + ":" + p.Plugin
	}
	return encodeLinesArg(lines, false)
}

func domainSet(domains []string) map[string]bool {
	return sliceSet(domains)
}

func sliceSet(items []string) map[string]bool {
	s := make(map[string]bool, len(items))
	for _, v := range items {
		s[v] = true
	}
	return s
}
