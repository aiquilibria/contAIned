package sigstore

import (
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"os"
	"os/exec"
	"time"

	"contained.dev/cli/internal/docker"
)

// signingPayload is the structured content written to disk and signed by cosign
// sign-blob. Including policyRef/policyVersion in this JSON blob means the
// mAInlined policy version is cryptographically bound to the image digest in
// the Rekor transparency log entry.
type signingPayload struct {
	ImageDigest   string `json:"image_digest"`
	PolicyRef     string `json:"policy_ref,omitempty"`
	PolicyVersion string `json:"policy_version,omitempty"`
}

// SignImage signs the local Docker image digest as a blob using cosign sign-blob
// (keyless OIDC flow). Writes the bundle to bundleDest and returns a Provenance
// record parsed from the resulting bundle.
//
// policyRef and policyVersion are included in the signed JSON payload so the
// mAInlined policy version is part of the Rekor entry. Pass empty strings when
// mAInlined is not configured.
//
// If idToken is non-empty it is passed to cosign via the SIGSTORE_ID_TOKEN
// environment variable so the OIDC browser flow is skipped — useful when a
// token was already obtained during mAInlined registration.
//
// Requires the cosign binary on the operator's PATH or a well-known install
// location. This is a known limitation; see the package comment for context.
//
// stderr is passed through so the operator can complete the OIDC browser flow.
func SignImage(image, rekorURL, fulcioURL, bundleDest, idToken, policyRef, policyVersion string) (*Provenance, error) {
	cosignBin, err := FindCosign()
	if err != nil {
		return nil, err
	}

	dockerBin, err := docker.FindDockerBin()
	if err != nil {
		return nil, err
	}

	imageDigest, err := GetImageDigest(dockerBin, image)
	if err != nil {
		return nil, fmt.Errorf("getting image digest: %w", err)
	}

	// Build the JSON signing payload — cosign signs the file contents and the
	// hash is recorded in the Rekor entry. Including policy_ref/policy_version
	// here cryptographically binds the mAInlined policy to the image digest.
	payload := signingPayload{
		ImageDigest:   imageDigest,
		PolicyRef:     policyRef,
		PolicyVersion: policyVersion,
	}
	payloadJSON, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshalling signing payload: %w", err)
	}

	digestFile, err := os.CreateTemp("", "contained-digest-*.txt")
	if err != nil {
		return nil, err
	}
	defer os.Remove(digestFile.Name())
	if _, err := digestFile.Write(payloadJSON); err != nil {
		return nil, err
	}
	digestFile.Close()

	// Temp bundle destination (copy to bundleDest after signing).
	bundleFile, err := os.CreateTemp("", "contained-bundle-*.json")
	if err != nil {
		return nil, err
	}
	bundleFile.Close()
	defer os.Remove(bundleFile.Name())

	cmd := exec.Command(
		cosignBin, "sign-blob",
		"--yes",
		"--rekor-url="+rekorURL,
		"--fulcio-url="+fulcioURL,
		"--bundle="+bundleFile.Name(),
		digestFile.Name(),
	)
	// Pass stdin/stdout/stderr through so the OIDC browser/device flow works.
	// When an ID token is available (e.g. from mAInlined registration), inject
	// it via SIGSTORE_ID_TOKEN so cosign skips its own browser prompt.
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = os.Environ()
	if idToken != "" {
		cmd.Env = append(cmd.Env, "SIGSTORE_ID_TOKEN="+idToken)
	}
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("cosign sign-blob failed: %w", err)
	}

	bundleJSON, err := os.ReadFile(bundleFile.Name())
	if err != nil {
		return nil, fmt.Errorf("reading bundle: %w", err)
	}

	// Copy bundle to permanent destination.
	if bundleDest != "" {
		if err := os.WriteFile(bundleDest, bundleJSON, 0o644); err != nil {
			return nil, fmt.Errorf("writing bundle to %s: %w", bundleDest, err)
		}
	}

	prov, err := parseBundle(imageDigest, rekorURL, bundleJSON)
	if err != nil {
		return nil, fmt.Errorf("parsing bundle: %w", err)
	}
	prov.PolicyRef = policyRef
	prov.PolicyVersion = policyVersion
	prov.SignedPayload = string(payloadJSON)
	return prov, nil
}

// parseBundle extracts provenance fields from a cosign bundle JSON.
func parseBundle(imageDigest, rekorURL string, bundleJSON []byte) (*Provenance, error) {
	var bundle struct {
		VerificationMaterial struct {
			Certificate struct {
				RawBytes string `json:"rawBytes"`
			} `json:"certificate"`
			TlogEntries []struct {
				LogIndex       string `json:"logIndex"`
				IntegratedTime string `json:"integratedTime"`
			} `json:"tlogEntries"`
		} `json:"verificationMaterial"`
	}
	if err := json.Unmarshal(bundleJSON, &bundle); err != nil {
		return nil, fmt.Errorf("unmarshalling bundle JSON: %w", err)
	}

	tlog := bundle.VerificationMaterial.TlogEntries
	if len(tlog) == 0 {
		return nil, fmt.Errorf("bundle contains no transparency log entries")
	}

	var logIndex int
	fmt.Sscanf(tlog[0].LogIndex, "%d", &logIndex)
	var integratedTime int64
	fmt.Sscanf(tlog[0].IntegratedTime, "%d", &integratedTime)
	signedAt := time.Unix(integratedTime, 0).UTC().Format(time.RFC3339)
	rekorEntryURL := fmt.Sprintf("%s/api/v1/log/entries?logIndex=%d", rekorURL, logIndex)

	// Parse the Fulcio certificate for operator identity and OIDC issuer.
	certDER, err := base64.StdEncoding.DecodeString(bundle.VerificationMaterial.Certificate.RawBytes)
	if err != nil {
		return nil, fmt.Errorf("decoding certificate: %w", err)
	}
	identity, oidcIssuer := parseCertificate(certDER)

	return &Provenance{
		SchemaVersion:    1,
		ImageDigest:      imageDigest,
		RekorLogIndex:    logIndex,
		RekorEntryURL:    rekorEntryURL,
		OperatorIdentity: identity,
		OIDCIssuer:       oidcIssuer,
		SignedAt:         signedAt,
	}, nil
}

// parseCertificate extracts the operator identity (SAN) and OIDC issuer
// from a Fulcio DER-encoded certificate.
func parseCertificate(der []byte) (identity, oidcIssuer string) {
	identity = "unknown"
	oidcIssuer = "unknown"

	// Try to parse as DER directly; fall back to PEM.
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		// Maybe it's PEM-wrapped.
		block, _ := pem.Decode(der)
		if block == nil {
			return
		}
		cert, err = x509.ParseCertificate(block.Bytes)
		if err != nil {
			return
		}
	}

	// Subject Alternative Name: prefer email, fall back to URI SANs.
	if len(cert.EmailAddresses) > 0 {
		identity = cert.EmailAddresses[0]
	} else if len(cert.URIs) > 0 {
		identity = cert.URIs[0].String()
	}

	// OIDC issuer is in Sigstore OID 1.3.6.1.4.1.57264.1.1
	sigstoreIssuerOID := []int{1, 3, 6, 1, 4, 1, 57264, 1, 1}
	for _, ext := range cert.Extensions {
		if intSliceEqual(ext.Id, sigstoreIssuerOID) {
			// Value is a UTF-8 string, possibly with a leading ASN.1 tag byte.
			val := ext.Value
			if len(val) > 2 && val[0] == 0x0c {
				// UTF8String tag — skip tag+length bytes.
				val = val[2:]
			}
			oidcIssuer = string(val)
			break
		}
	}

	return identity, oidcIssuer
}

func intSliceEqual(a []int, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
