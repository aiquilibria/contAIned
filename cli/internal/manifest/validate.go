package manifest

import (
	"fmt"
	"strings"
)

var validActions = map[string]bool{
	"allow":    true,
	"block":    true,
	"escalate": true,
}

// Validate checks that required fields are present and values are within
// allowed sets. Returns the first validation error encountered.
func Validate(m *Manifest) error {
	if m.Runtime.Docker.Image == "" {
		return fmt.Errorf("runtime.docker.image is required")
	}
	if m.Runtime.Docker.Network == "" {
		return fmt.Errorf("runtime.docker.network is required")
	}

	if err := validateRules("policy.secrets.rules", m.Policy.Secrets.Rules); err != nil {
		return err
	}
	if err := validateRules("policy.bash.rules", m.Policy.Bash.Rules); err != nil {
		return err
	}

	if m.Policy.Sigstore.Enabled {
		if m.Policy.Sigstore.RekorURL == "" {
			return fmt.Errorf("policy.sigstore.rekor_url is required when sigstore is enabled")
		}
		if m.Policy.Sigstore.FulcioURL == "" {
			return fmt.Errorf("policy.sigstore.fulcio_url is required when sigstore is enabled")
		}
	}

	for i, check := range m.Policy.QA.Checks {
		if check.Name == "" {
			return fmt.Errorf("policy.qa.checks[%d].name is required", i)
		}
		if len(check.Command) == 0 {
			return fmt.Errorf("policy.qa.checks[%d].command is required", i)
		}
	}

	return nil
}

func validateRules(field string, rules []Rule) error {
	for i, r := range rules {
		if r.Name == "" {
			return fmt.Errorf("%s[%d].name is required", field, i)
		}
		if len(r.Patterns) == 0 {
			return fmt.Errorf("%s[%d].patterns must not be empty", field, i)
		}
		action := strings.ToLower(r.Action)
		if action == "" {
			return fmt.Errorf("%s[%d].action is required", field, i)
		}
		if !validActions[action] {
			return fmt.Errorf("%s[%d].action %q is invalid (must be allow, block, or escalate)", field, i, r.Action)
		}
	}
	return nil
}
