package manifest

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
)

var supportedToolchains = map[string]bool{
	"go":   true,
	"node": true,
	"ruby": true,
	"java": true,
}

// Validate checks that required fields are present and values are within
// allowed sets. Returns the first validation error encountered.
func Validate(m *Manifest) error {
	if m.Init.Container.Image == "" {
		return fmt.Errorf("init.container.image is required")
	}
	if m.Init.Container.Network == "" {
		return fmt.Errorf("init.container.network is required")
	}

	if err := validateToolchains(m.Init.Container.Toolchains); err != nil {
		return err
	}

	if m.Init.Sigstore.Enabled {
		if m.Init.Sigstore.RekorURL == "" {
			return fmt.Errorf("init.sigstore.rekor_url is required when sigstore is enabled")
		}
		if m.Init.Sigstore.FulcioURL == "" {
			return fmt.Errorf("init.sigstore.fulcio_url is required when sigstore is enabled")
		}
	}

	for i, check := range m.Runtime.QA.Checks {
		if check.Name == "" {
			return fmt.Errorf("runtime.qa.checks[%d].name is required", i)
		}
		if len(check.Command) == 0 {
			return fmt.Errorf("runtime.qa.checks[%d].command is required", i)
		}
	}

	return nil
}

// validateToolchains checks toolchain names are supported and versions non-empty.
func validateToolchains(toolchains map[string]string) error {
	for name, version := range toolchains {
		if !supportedToolchains[name] {
			return fmt.Errorf("init.container.toolchains: unsupported toolchain %q (supported: %s)",
				name, supportedToolchainNames())
		}
		if strings.TrimSpace(version) == "" {
			return fmt.Errorf("init.container.toolchains: version for %q must not be empty", name)
		}
	}
	return nil
}

// supportedToolchainNames returns a sorted, comma-joined list of supported toolchain names.
func supportedToolchainNames() string {
	names := make([]string, 0, len(supportedToolchains))
	for name := range supportedToolchains {
		names = append(names, name)
	}
	sort.Strings(names)
	return strings.Join(names, ", ")
}

// minVersionFromConstraint extracts the concrete floor version from a
// constraint string so it can be used as an install target.
// ">=1.22" → "1.22", "==1.22.5" → "1.22.5", bare "1.22.5" → "1.22.5".
func minVersionFromConstraint(constraint string) string {
	if strings.HasPrefix(constraint, ">=") {
		return strings.TrimSpace(constraint[2:])
	}
	if strings.HasPrefix(constraint, "==") {
		return strings.TrimSpace(constraint[2:])
	}
	return constraint
}

// satisfiesConstraint reports whether repoVersion satisfies constraint.
//
// Constraint formats (mAInlined): ">=X.Y.Z" (floor), "==X.Y.Z" or bare "X.Y.Z" (exact).
// Repo version formats: "==X.Y.Z" or bare "X.Y.Z".
func satisfiesConstraint(constraint, repoVersion string) (bool, error) {
	op := "=="
	constraintVer := constraint
	if strings.HasPrefix(constraint, ">=") {
		op = ">="
		constraintVer = strings.TrimSpace(constraint[2:])
	} else if strings.HasPrefix(constraint, "==") {
		constraintVer = strings.TrimSpace(constraint[2:])
	}

	repoVer := repoVersion
	if strings.HasPrefix(repoVersion, "==") {
		repoVer = strings.TrimSpace(repoVersion[2:])
	}

	cmp, err := compareVersions(repoVer, constraintVer)
	if err != nil {
		return false, err
	}

	switch op {
	case ">=":
		return cmp >= 0, nil
	default: // "=="
		return cmp == 0, nil
	}
}

// compareVersions compares two dot-separated version strings.
// Returns -1, 0, or 1 (a < b, a == b, a > b).
func compareVersions(a, b string) (int, error) {
	aParts := strings.Split(a, ".")
	bParts := strings.Split(b, ".")

	maxLen := len(aParts)
	if len(bParts) > maxLen {
		maxLen = len(bParts)
	}

	for i := 0; i < maxLen; i++ {
		var aNum, bNum int
		if i < len(aParts) {
			n, err := strconv.Atoi(strings.TrimSpace(aParts[i]))
			if err != nil {
				return 0, fmt.Errorf("invalid version segment %q in %q", aParts[i], a)
			}
			aNum = n
		}
		if i < len(bParts) {
			n, err := strconv.Atoi(strings.TrimSpace(bParts[i]))
			if err != nil {
				return 0, fmt.Errorf("invalid version segment %q in %q", bParts[i], b)
			}
			bNum = n
		}
		if aNum != bNum {
			if aNum > bNum {
				return 1, nil
			}
			return -1, nil
		}
	}
	return 0, nil
}
