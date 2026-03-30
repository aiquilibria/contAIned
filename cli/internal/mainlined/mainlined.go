// Package mainlined implements the mAInlined registration flow for contained init.
// It handles URL parsing, the agent registration API call, and secure storage of
// the returned API key.
package mainlined

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// ManifestHashEmpty is the SHA-256 of an empty string (hex-encoded).
// Used as the manifest_hash in bootstrap registrations where no operator
// manifest exists yet — the caller sends this value and the server's
// policy_yaml response becomes the actual manifest.
const ManifestHashEmpty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

// ParsedURL holds the three components extracted from a mAInlined scope URL.
type ParsedURL struct {
	Server string // e.g. "https://mainlined.example.com"
	Org    string // e.g. "acme"
	Scope  string // e.g. "backend-api"
}

// ParseURL splits a mAInlined scope URL into server, org, and scope.
// The URL must have exactly two non-empty path segments after the host.
// Example: https://mainlined.example.com/acme/backend-api
func ParseURL(rawURL string) (ParsedURL, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return ParsedURL{}, fmt.Errorf("invalid URL: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return ParsedURL{}, fmt.Errorf("URL scheme must be http or https, got %q", u.Scheme)
	}
	if u.Host == "" {
		return ParsedURL{}, fmt.Errorf("URL is missing a host")
	}
	path := strings.Trim(u.Path, "/")
	parts := strings.Split(path, "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return ParsedURL{}, fmt.Errorf(
			"URL path must have exactly two segments (org/scope), got %q — "+
				"example: https://mainlined.example.com/acme/backend-api", rawURL,
		)
	}
	return ParsedURL{
		Server: u.Scheme + "://" + u.Host,
		Org:    parts[0],
		Scope:  parts[1],
	}, nil
}

// RegistrationRequest is the JSON body sent to the agents/register endpoint.
type RegistrationRequest struct {
	SystemURI    string `json:"system_uri"`
	ManifestHash string `json:"manifest_hash"`
}

// RegistrationResponse is the JSON body returned by the agents/register endpoint.
type RegistrationResponse struct {
	APIKey        string `json:"api_key"`
	PolicyRef     string `json:"policy_ref"`
	PolicyVersion string `json:"policy_version"`
	PolicyYAML    string `json:"policy_yaml"`
}

// Register calls POST {server}/{org}/{scope}/agents/register with the given OIDC
// ID token and returns the registration response containing the API key and policy.
// systemURI is the stable identity of this container; if empty it defaults to
// "contained://{org}/{scope}".
func Register(p ParsedURL, idToken, systemURI, manifestHash string) (*RegistrationResponse, error) {
	if systemURI == "" {
		systemURI = SystemURI(p.Org, p.Scope)
	}
	endpoint := fmt.Sprintf("%s/%s/%s/agents/register", p.Server, p.Org, p.Scope)

	body, err := json.Marshal(RegistrationRequest{
		SystemURI:    systemURI,
		ManifestHash: manifestHash,
	})
	if err != nil {
		return nil, fmt.Errorf("marshalling registration request: %w", err)
	}

	client := &http.Client{Timeout: 30 * time.Second}
	req, err := http.NewRequest(http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("building registration request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+idToken)

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("calling mAInlined register endpoint: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	switch resp.StatusCode {
	case http.StatusOK:
		// success — fall through to decode
	case http.StatusUnauthorized:
		return nil, fmt.Errorf(
			"mAInlined rejected the OIDC token (HTTP 401) — " +
				"the token may have expired; re-run contained init to retry",
		)
	case http.StatusForbidden:
		return nil, fmt.Errorf(
			"mAInlined access denied (HTTP 403) — you may not be a member of "+
				"org %q or may lack a grant for scope %q; "+
				"provision access via the mAInlined UI first",
			p.Org, p.Scope,
		)
	case http.StatusNotFound:
		return nil, fmt.Errorf(
			"mAInlined org or scope not found (HTTP 404) — "+
				"verify the --mainlined URL: %s/%s/%s",
			p.Server, p.Org, p.Scope,
		)
	default:
		return nil, fmt.Errorf(
			"mAInlined registration failed (HTTP %d): %s",
			resp.StatusCode, strings.TrimSpace(string(respBody)),
		)
	}

	var reg RegistrationResponse
	if err := json.Unmarshal(respBody, &reg); err != nil {
		return nil, fmt.Errorf("parsing mAInlined registration response: %w", err)
	}
	if reg.APIKey == "" {
		return nil, fmt.Errorf("mAInlined registration response is missing api_key")
	}
	return &reg, nil
}

// StoreAPIKey writes the API key JWT to ~/.contained/secrets/<org>-<scope>
// with mode 0600. Parent directories are created with mode 0700 if absent.
// Returns the absolute path where the key was written.
func StoreAPIKey(org, scope, apiKey string) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolving home directory: %w", err)
	}
	secretsDir := filepath.Join(home, ".contained", "secrets")
	if err := os.MkdirAll(secretsDir, 0o700); err != nil {
		return "", fmt.Errorf("creating secrets directory %s: %w", secretsDir, err)
	}
	secretsPath := filepath.Join(secretsDir, org+"-"+scope)
	if err := os.WriteFile(secretsPath, []byte(apiKey), 0o600); err != nil {
		return "", fmt.Errorf("writing API key to %s: %w", secretsPath, err)
	}
	return secretsPath, nil
}

// SecretPath returns the expected host-side path for the mAInlined API key.
func SecretPath(org, scope string) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolving home directory: %w", err)
	}
	return filepath.Join(home, ".contained", "secrets", org+"-"+scope), nil
}

// SystemURI returns the stable identity URI for a container scope.
func SystemURI(org, scope string) string {
	return fmt.Sprintf("contained://%s/%s", org, scope)
}

// HashManifest computes the lowercase hex SHA-256 digest of content.
func HashManifest(content string) string {
	h := sha256.Sum256([]byte(content))
	return fmt.Sprintf("%x", h)
}

// IntimateProvenance sends image-signing provenance to the mAInlined server
// after a successful Sigstore signing. The call is fire-and-forget: errors
// are printed to stderr but do not abort contained init.
func IntimateProvenance(
	p ParsedURL,
	apiKey string,
	operatorIdentity string,
	hostWorkspace string,
	mainlinedURL string,
	policyRef string,
	policyVersion string,
	imageName string,
	imageDigest string,
	rekorLogIndex int,
	rekorURL string,
) {
	payload, _ := json.Marshal(map[string]any{
		"operator_identity": operatorIdentity,
		"host_workspace":    hostWorkspace,
		"mainlined_url":     mainlinedURL,
		"policy_ref":        policyRef,
		"policy_version":    policyVersion,
		"image_name":        imageName,
		"image_digest":      imageDigest,
		"rekor_log_index":   rekorLogIndex,
		"rekor_url":         rekorURL,
	})

	endpoint := fmt.Sprintf("%s/%s/%s/provenance", p.Server, p.Org, p.Scope)
	req, err := http.NewRequest(http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		fmt.Fprintf(os.Stderr, "  provenance intimation: build request: %v\n", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+apiKey)

	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "  provenance intimation: %v\n", err)
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		fmt.Fprintf(os.Stderr, "  provenance intimation: server returned %d: %s\n", resp.StatusCode, body)
	}
}
