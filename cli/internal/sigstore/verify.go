package sigstore

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	sgbundle "github.com/sigstore/sigstore-go/pkg/bundle"
	"github.com/sigstore/sigstore-go/pkg/root"
	"github.com/sigstore/sigstore-go/pkg/verify"

	"contained.dev/cli/internal/docker"
	"contained.dev/cli/internal/manifest"
)

// VerifyWorkspace checks that the local Docker image still matches the signed
// digest in provenance.yaml and that the Sigstore bundle signature is valid.
//
// Returns (provenance, nil) on success.
// Returns (nil, nil) when Sigstore is disabled — not an error.
// Returns (nil, err) on any verification failure.
func VerifyWorkspace(root_ string) (*Provenance, error) {
	m, err := manifest.Load(root_)
	if err != nil {
		return nil, fmt.Errorf("loading manifest: %w", err)
	}

	if !m.Init.Sigstore.Enabled {
		return nil, nil // disabled — not an error
	}

	provPath := filepath.Join(root_, ".contAIned", "provenance.yaml")
	if _, err := os.Stat(provPath); os.IsNotExist(err) {
		return nil, fmt.Errorf(
			"provenance.yaml not found despite Sigstore being enabled — " +
				"re-run 'contained init' to generate provenance",
		)
	}

	prov, err := LoadProvenance(root_)
	if err != nil {
		return nil, err
	}

	// Check image digest against the running local image.
	dockerBin, err := docker.FindDockerBin()
	if err != nil {
		return nil, fmt.Errorf("locating docker: %w", err)
	}

	image := m.Init.Container.Image
	actualDigest, err := GetImageDigest(dockerBin, image)
	if err != nil {
		return nil, fmt.Errorf("inspecting image: %w", err)
	}

	if actualDigest != prov.ImageDigest {
		return nil, fmt.Errorf(
			"image digest mismatch — image has been replaced since init\n"+
				"  expected: %s\n"+
				"  actual:   %s",
			prov.ImageDigest, actualDigest,
		)
	}

	bundlePath := filepath.Join(root_, ".contAIned", "provenance.bundle")
	if _, err := os.Stat(bundlePath); os.IsNotExist(err) {
		return nil, fmt.Errorf(
			"provenance.bundle not found — re-run 'contained init' to regenerate",
		)
	}

	rekorURL := m.Init.Sigstore.RekorURL

	// For new bundles the signed artifact is a JSON payload (image_digest +
	// policy_ref + policy_version). Legacy bundles signed only the raw digest
	// string; fall back to that when signed_payload is absent.
	verifyPayload := prov.SignedPayload
	if verifyPayload == "" {
		verifyPayload = prov.ImageDigest
	}
	if err := VerifyBundle(bundlePath, verifyPayload, prov.OperatorIdentity, prov.OIDCIssuer, rekorURL); err != nil {
		return nil, fmt.Errorf("Sigstore verification failed: %w", err)
	}

	return prov, nil
}

// VerifyBundle verifies a Sigstore bundle file using sigstore-go.
// No cosign binary is required.
//
// bundlePath  — path to the .json bundle produced by cosign sign-blob
// payload     — the exact bytes that were signed; for new bundles this is a
//
//	JSON object (image_digest + policy_ref + policy_version);
//	for legacy bundles it is the raw image digest string
//
// identity    — expected certificate SAN (operator email or URI)
// oidcIssuer  — expected OIDC issuer URL
// rekorURL    — Rekor transparency log URL (used only for informational checks)
func VerifyBundle(bundlePath, payload, identity, oidcIssuer, _ string) error {
	b, err := sgbundle.LoadJSONFromPath(bundlePath)
	if err != nil {
		return fmt.Errorf("loading bundle: %w", err)
	}

	trustedRoot, err := root.FetchTrustedRoot()
	if err != nil {
		return fmt.Errorf("fetching Sigstore trusted root: %w", err)
	}

	v, err := verify.NewSignedEntityVerifier(
		trustedRoot,
		verify.WithSignedCertificateTimestamps(1),
		verify.WithTransparencyLog(1),
		verify.WithObserverTimestamps(1),
	)
	if err != nil {
		return fmt.Errorf("creating verifier: %w", err)
	}

	certID, err := verify.NewShortCertificateIdentity(oidcIssuer, "", identity, "")
	if err != nil {
		return fmt.Errorf("building certificate identity: %w", err)
	}

	policy := verify.NewPolicy(
		verify.WithArtifact(strings.NewReader(payload)),
		verify.WithCertificateIdentity(certID),
	)

	if _, err := v.Verify(b, policy); err != nil {
		return fmt.Errorf("bundle verification: %w", err)
	}

	return nil
}
