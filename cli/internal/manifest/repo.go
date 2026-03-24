package manifest

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// RepoManifest is the restricted manifest a repository may commit to
// .contAIned_manifest.yaml at the repo root. Only toolchains and QA checks are permitted;
// all policy fields are owned by the Mainlined manifest.
type RepoManifest struct {
	Runtime RepoRuntimeConfig `yaml:"runtime"`
	Policy  RepoPolicy        `yaml:"policy"`
}

// RepoRuntimeConfig is the runtime section of a repo manifest.
type RepoRuntimeConfig struct {
	Docker RepoDockerConfig `yaml:"docker"`
}

// RepoDockerConfig is the docker section of a repo manifest — toolchains only.
type RepoDockerConfig struct {
	Toolchains map[string]string `yaml:"toolchains"`
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
	if err := dec.Decode(&r); err != nil {
		return nil, fmt.Errorf("repo manifest contains disallowed fields "+
			"(only runtime.docker.toolchains and policy.qa.checks are permitted): %w", err)
	}

	return &r, nil
}

// ValidateRepoManifest checks toolchain names, versions, and QA check structure.
func ValidateRepoManifest(r *RepoManifest) error {
	for name, version := range r.Runtime.Docker.Toolchains {
		if !supportedToolchains[name] {
			return fmt.Errorf("runtime.docker.toolchains: unsupported toolchain %q (supported: %s)",
				name, supportedToolchainNames())
		}
		if strings.TrimSpace(version) == "" {
			return fmt.Errorf("runtime.docker.toolchains: version for %q must not be empty", name)
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

// MergeRepoManifest merges the repository manifest into the Mainlined manifest and
// returns the merged result. The original manifests are not modified.
//
// Toolchains: if Mainlined specifies a constraint for a toolchain the repo also
// declares, the repo version must satisfy that constraint — otherwise an error is
// returned. The repo's pinned version is used as the install target.
//
// QA checks: concatenated (Mainlined first, repo second). Either or both may be empty.
//
// If repo is nil the function returns a copy of mainlined unchanged.
func MergeRepoManifest(mainlined *Manifest, repo *RepoManifest) (*Manifest, error) {
	// Shallow-copy the mainlined manifest.
	merged := *mainlined

	// Deep-copy toolchains so we don't mutate the original.
	merged.Runtime.Docker.Toolchains = make(map[string]string)
	for k, v := range mainlined.Runtime.Docker.Toolchains {
		merged.Runtime.Docker.Toolchains[k] = v
	}

	if repo == nil {
		return &merged, nil
	}

	for name, repoVer := range repo.Runtime.Docker.Toolchains {
		if mainlinedConstraint, exists := mainlined.Runtime.Docker.Toolchains[name]; exists {
			ok, err := satisfiesConstraint(mainlinedConstraint, repoVer)
			if err != nil {
				return nil, fmt.Errorf("toolchain %q: %w", name, err)
			}
			if !ok {
				return nil, fmt.Errorf(
					"toolchain %q: repo version %q does not satisfy mainlined constraint %q",
					name, repoVer, mainlinedConstraint,
				)
			}
		}
		merged.Runtime.Docker.Toolchains[name] = repoVer
	}

	// Concatenate QA checks: mainlined (expected empty) then repo.
	merged.Policy.QA.Checks = append(
		append([]QACheck{}, mainlined.Policy.QA.Checks...),
		repo.Policy.QA.Checks...,
	)

	return &merged, nil
}
