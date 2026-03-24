// Package sigstore provides image signing and provenance verification for
// contAIned workspaces.
//
// Verification uses github.com/sigstore/sigstore-go directly — no cosign
// binary is required on the verify path.
//
// Signing (contained init) still shells out to cosign sign-blob; eliminating
// that dependency requires a full Fulcio OIDC flow and is deferred.
package sigstore

import (
	"fmt"
	"os"
	"os/exec"
	"strings"

	"gopkg.in/yaml.v3"
)

// Provenance is the contents of .contAIned/provenance.yaml.
type Provenance struct {
	SchemaVersion    int    `yaml:"schema_version"`
	ImageDigest      string `yaml:"image_digest"`
	RekorLogIndex    int    `yaml:"rekor_log_index"`
	RekorEntryURL    string `yaml:"rekor_entry_url"`
	OperatorIdentity string `yaml:"operator_identity"`
	OIDCIssuer       string `yaml:"oidc_issuer"`
	SignedAt         string `yaml:"signed_at"`
}

// LoadProvenance reads and parses .contAIned/provenance.yaml.
func LoadProvenance(root string) (*Provenance, error) {
	path := root + "/.contAIned/provenance.yaml"
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading provenance.yaml: %w", err)
	}
	var p Provenance
	if err := yaml.Unmarshal(data, &p); err != nil {
		return nil, fmt.Errorf("parsing provenance.yaml: %w", err)
	}
	return &p, nil
}

// WriteProvenance writes a Provenance struct to .contAIned/provenance.yaml.
func WriteProvenance(root string, p *Provenance) error {
	path := root + "/.contAIned/provenance.yaml"
	out, err := yaml.Marshal(p)
	if err != nil {
		return err
	}
	return os.WriteFile(path, out, 0o644)
}

// GetImageDigest returns the sha256 image ID for a local Docker image.
func GetImageDigest(dockerBin, image string) (string, error) {
	out, err := exec.Command(
		dockerBin, "image", "inspect", "--format", "{{.Id}}", image,
	).Output()
	if err != nil {
		return "", fmt.Errorf("docker image inspect %s: %w", image, err)
	}
	id := strings.TrimSpace(string(out))
	if !strings.HasPrefix(id, "sha256:") {
		return "", fmt.Errorf("unexpected image ID format: %q", id)
	}
	return id, nil
}

var cosignSearchPaths = []string{
	"/usr/local/bin/cosign",
	"/usr/bin/cosign",
	"/opt/homebrew/bin/cosign",
}

// FindCosign locates the cosign v2 executable.
func FindCosign() (string, error) {
	if p, err := exec.LookPath("cosign"); err == nil {
		return p, nil
	}
	for _, p := range cosignSearchPaths {
		if info, err := os.Stat(p); err == nil && !info.IsDir() {
			return p, nil
		}
	}
	return "", fmt.Errorf(
		"cosign not found — cosign v2 is required for image signing\n" +
			"  Install: https://docs.sigstore.dev/cosign/system_config/installation/",
	)
}
