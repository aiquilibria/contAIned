// Package mainlined implements the mAInlined registration flow for contained init.
// It handles URL parsing, the agent registration API call, and secure storage of
// the returned API key.
package mainlined

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
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

// StoreWorkspaceAPIKey writes the API key JWT to <workspace>/.contAIned/mainlined_api_key
// with mode 0600. The .contAIned/ directory is created with mode 0700 if absent.
// Storing the key inside the workspace (rather than a shared user-home location) ensures
// that parallel sessions across different workspaces using the same org/scope never
// overwrite each other's credentials.
// Returns the absolute path where the key was written.
func StoreWorkspaceAPIKey(workspace, apiKey string) (string, error) {
	dir := filepath.Join(workspace, ".contAIned")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("creating .contAIned directory %s: %w", dir, err)
	}
	keyPath := filepath.Join(dir, "mainlined_api_key")
	if err := os.WriteFile(keyPath, []byte(apiKey), 0o600); err != nil {
		return "", fmt.Errorf("writing API key to %s: %w", keyPath, err)
	}
	return keyPath, nil
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

// JWTExpired reports whether the given JWT has expired (or will expire within
// the next 5 minutes). It decodes the payload segment without verifying the
// signature — the server will reject a tampered or revoked token; this check
// is purely to decide whether to attempt re-registration proactively.
//
// Returns false (treat as not expired) when key is not a structurally valid
// three-segment JWT, or when the payload contains no "exp" claim — callers
// should attempt to use the key as-is in those cases.
func JWTExpired(key string) bool {
	key = strings.TrimSpace(key)
	parts := strings.Split(key, ".")
	if len(parts) != 3 {
		return false
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return false
	}
	var claims struct {
		Exp int64 `json:"exp"`
	}
	if err := json.Unmarshal(payload, &claims); err != nil {
		return false
	}
	if claims.Exp == 0 {
		return false
	}
	// Treat as expired 5 minutes before the actual expiry to avoid races.
	return time.Now().Add(5*time.Minute).Unix() >= claims.Exp
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
