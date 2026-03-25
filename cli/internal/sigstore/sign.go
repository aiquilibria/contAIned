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

// SignImage signs the local Docker image digest as a blob using cosign sign-blob
// (keyless OIDC flow). Writes the bundle to bundleDest and returns a Provenance
// record parsed from the resulting bundle.
//
// stderr is passed through so the operator can complete the OIDC browser flow.
func SignImage(image, rekorURL, fulcioURL, bundleDest string) (*Provenance, error) {
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

	// Write the digest to a temp file — cosign signs file contents.
	digestFile, err := os.CreateTemp("", "contained-digest-*.txt")
	if err != nil {
		return nil, err
	}
	defer os.Remove(digestFile.Name())
	if _, err := digestFile.WriteString(imageDigest); err != nil {
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
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
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
