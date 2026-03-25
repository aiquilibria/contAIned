package manifest

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// RepoManifest is the restricted manifest a repository may commit to
// .contAIned_manifest.yaml at the repo root. Only two top-level keys are
// permitted: ecosystems and policy.qa.checks. All other policy settings
// (network, secrets, bash rules, sigstore, etc.) are owned by the operator
// manifest and cannot be overridden here.
//
// Ecosystems declares the language runtimes this repository needs. Each key is
// an ecosystem name defined in the operator manifest's ecosystem_definitions;
// the value is the version to install (empty string for pre-installed runtimes
// such as Python). At `contained init` time each declaration is resolved to a
// toolchain install target and the required network domains are added to the
// allowlist automatically.
type RepoManifest struct {
	Ecosystems map[string]string `yaml:"ecosystems,omitempty"`
	Policy     RepoPolicy        `yaml:"policy"`
}

// RepoPolicy is the policy section of a repo manifest — QA checks only.
type RepoPolicy struct {
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
			"(only ecosystems and policy.qa.checks are permitted): %w", err)
	}

	return &r, nil
}

// ValidateRepoManifest checks the structure of a repo manifest.
// Ecosystem name resolution against operator definitions happens in
// MergeRepoManifest, which has access to both manifests.
func ValidateRepoManifest(r *RepoManifest) error {
	for name := range r.Ecosystems {
		if strings.TrimSpace(name) == "" {
			return fmt.Errorf("ecosystems: ecosystem name must not be empty")
		}
	}

	for i, check := range r.Policy.QA.Checks {
		if check.Name == "" {
			return fmt.Errorf("policy.qa.checks[%d].name is required", i)
		}
		if len(check.Command) == 0 {
			return fmt.Errorf("policy.qa.checks[%d].command is required", i)
		}
	}

	return nil
}

// MergeRepoManifest merges the repository manifest into the operator manifest
// and returns the merged result. The original manifests are not modified.
//
// Ecosystems: each entry in repo.Ecosystems is resolved against the operator
// manifest's ecosystem_definitions. The resolved toolchain version is installed
// and the required network domains are added to the allowlist. The repo version
// must satisfy any floor constraint the operator has set on the underlying toolchain.
//
// QA checks: concatenated (operator first, repo second). Either or both may be empty.
//
// If repo is nil the function returns a copy of the operator manifest unchanged.
func MergeRepoManifest(operator *Manifest, repo *RepoManifest) (*Manifest, error) {
	// Shallow-copy the operator manifest.
	merged := *operator

	// Deep-copy mutable fields so we don't mutate the originals.
	// operator.Runtime.Docker.Toolchains holds floor *constraints* (e.g. ">=1.22"),
	// not install versions. Convert each to its minimum concrete version so that
	// toolchains are always installed with a valid version string even when no
	// repo manifest overrides the default.
	merged.Runtime.Docker.Toolchains = make(map[string]string)
	for k, v := range operator.Runtime.Docker.Toolchains {
		merged.Runtime.Docker.Toolchains[k] = minVersionFromConstraint(v)
	}
	merged.Policy.Network.AllowedDomains = append([]string{}, operator.Policy.Network.AllowedDomains...)

	if repo == nil {
		return &merged, nil
	}

	// Resolve ecosystem declarations to toolchain installs + network domains + env vars.
	existing := domainSet(merged.Policy.Network.AllowedDomains)
	for ecoName, version := range repo.Ecosystems {
		def, ok := operator.EcosystemDefinitions[ecoName]
		if !ok {
			return nil, fmt.Errorf(
				"ecosystem %q is not defined in the operator manifest "+
					"(add an ecosystem_definitions.%s entry to your manifest.yaml)",
				ecoName, ecoName,
			)
		}

		// Install toolchain if this ecosystem has one and a version was specified.
		if def.Toolchain != "" {
			ver := strings.TrimSpace(version)
			if ver != "" {
				// Enforce any floor constraint the operator set on this toolchain.
				if constraint, exists := operator.Runtime.Docker.Toolchains[def.Toolchain]; exists {
					sat, err := satisfiesConstraint(constraint, ver)
					if err != nil {
						return nil, fmt.Errorf("ecosystem %q toolchain %q: %w", ecoName, def.Toolchain, err)
					}
					if !sat {
						return nil, fmt.Errorf(
							"ecosystem %q: version %q does not satisfy operator constraint %q for toolchain %q",
							ecoName, ver, constraint, def.Toolchain,
						)
					}
				}
				merged.Runtime.Docker.Toolchains[def.Toolchain] = ver
			}
		}

		// Add network domains when network policy is enabled.
		if merged.Policy.Network.Enabled {
			for _, domain := range def.NetworkDomains {
				if !existing[domain] {
					merged.Policy.Network.AllowedDomains = append(merged.Policy.Network.AllowedDomains, domain)
					existing[domain] = true
				}
			}
		}

		// Merge ecosystem env vars (last writer wins for duplicate keys).
		if len(def.Env) > 0 {
			if merged.Runtime.Docker.Env == nil {
				merged.Runtime.Docker.Env = make(map[string]string)
			}
			for k, v := range def.Env {
				merged.Runtime.Docker.Env[k] = v
			}
		}
	}

	// Concatenate QA checks: operator first, then repo.
	merged.Policy.QA.Checks = append(
		append([]QACheck{}, operator.Policy.QA.Checks...),
		repo.Policy.QA.Checks...,
	)

	return &merged, nil
}

func domainSet(domains []string) map[string]bool {
	s := make(map[string]bool, len(domains))
	for _, d := range domains {
		s[d] = true
	}
	return s
}
