// Package oidc provides a minimal OIDC authorization-code + PKCE browser flow
// for obtaining an ID token from the Sigstore public OIDC issuer.
package oidc

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"
)

const (
	// SigstoreIssuer is the Sigstore public OIDC issuer URL (Dex-based).
	SigstoreIssuer = "https://oauth2.sigstore.dev/auth"
	// SigstoreClientID is the public OAuth2 client ID registered with Sigstore.
	SigstoreClientID = "sigstore"
)

// GetIDToken returns an OIDC ID token for the given issuer. It checks the
// SIGSTORE_ID_TOKEN environment variable first; if that is non-empty the value
// is returned immediately without opening a browser. Otherwise an interactive
// authorization-code + PKCE flow is performed against issuerURL.
func GetIDToken(issuerURL, clientID string, scopes []string) (string, error) {
	if tok := os.Getenv("SIGSTORE_ID_TOKEN"); tok != "" {
		return tok, nil
	}
	return browserFlow(issuerURL, clientID, scopes)
}

// oidcDiscovery holds the endpoints we need from the OIDC discovery document.
type oidcDiscovery struct {
	AuthorizationEndpoint string `json:"authorization_endpoint"`
	TokenEndpoint         string `json:"token_endpoint"`
}

func discoverEndpoints(issuerURL string) (*oidcDiscovery, error) {
	discoveryURL := strings.TrimRight(issuerURL, "/") + "/.well-known/openid-configuration"
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Get(discoveryURL)
	if err != nil {
		return nil, fmt.Errorf("fetching OIDC discovery document: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("OIDC discovery returned HTTP %d", resp.StatusCode)
	}
	var d oidcDiscovery
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<16)).Decode(&d); err != nil {
		return nil, fmt.Errorf("parsing OIDC discovery document: %w", err)
	}
	if d.AuthorizationEndpoint == "" || d.TokenEndpoint == "" {
		return nil, fmt.Errorf("OIDC discovery missing authorization_endpoint or token_endpoint")
	}
	return &d, nil
}

// generatePKCE returns a (verifier, challenge) pair per RFC 7636.
// verifier is a high-entropy random string; challenge = base64url(sha256(verifier)).
func generatePKCE() (verifier, challenge string, err error) {
	b := make([]byte, 32)
	if _, err = rand.Read(b); err != nil {
		return "", "", fmt.Errorf("generating PKCE verifier: %w", err)
	}
	verifier = base64.RawURLEncoding.EncodeToString(b)
	h := sha256.Sum256([]byte(verifier))
	challenge = base64.RawURLEncoding.EncodeToString(h[:])
	return
}

func randomBase64(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

func openBrowser(u string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", u)
	case "windows":
		cmd = exec.Command("cmd", "/c", "start", u)
	default:
		cmd = exec.Command("xdg-open", u)
	}
	_ = cmd.Start()
}

// browserFlow runs the full authorization-code + PKCE flow and returns the
// raw ID token JWT string.
func browserFlow(issuerURL, clientID string, scopes []string) (string, error) {
	disc, err := discoverEndpoints(issuerURL)
	if err != nil {
		return "", err
	}

	verifier, challenge, err := generatePKCE()
	if err != nil {
		return "", err
	}
	state, err := randomBase64(16)
	if err != nil {
		return "", fmt.Errorf("generating OIDC state: %w", err)
	}

	// Start a local callback server on a random port.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", fmt.Errorf("starting OIDC callback server: %w", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	redirectURI := fmt.Sprintf("http://127.0.0.1:%d/auth/callback", port)

	type result struct {
		code string
		err  error
	}
	ch := make(chan result, 1)
	var once sync.Once

	mux := http.NewServeMux()
	mux.HandleFunc("/auth/callback", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if q.Get("state") != state {
			once.Do(func() { ch <- result{err: fmt.Errorf("OIDC state mismatch")} })
			http.Error(w, "state mismatch", http.StatusBadRequest)
			return
		}
		if errParam := q.Get("error"); errParam != "" {
			desc := q.Get("error_description")
			once.Do(func() { ch <- result{err: fmt.Errorf("OIDC error %s: %s", errParam, desc)} })
			http.Error(w, errParam, http.StatusBadRequest)
			return
		}
		code := q.Get("code")
		if code == "" {
			once.Do(func() { ch <- result{err: fmt.Errorf("OIDC callback: no code in response")} })
			http.Error(w, "missing code", http.StatusBadRequest)
			return
		}
		fmt.Fprintln(w, "<html><body><p>Authentication successful. You may close this tab.</p></body></html>")
		once.Do(func() { ch <- result{code: code} })
	})

	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(ln) }()
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	}()

	// Build and open the authorization URL.
	params := url.Values{
		"response_type":         {"code"},
		"client_id":             {clientID},
		"redirect_uri":          {redirectURI},
		"scope":                 {strings.Join(scopes, " ")},
		"state":                 {state},
		"code_challenge":        {challenge},
		"code_challenge_method": {"S256"},
	}
	authURL := disc.AuthorizationEndpoint + "?" + params.Encode()

	fmt.Fprintf(os.Stderr, "\nOpening browser for OIDC authentication…\n")
	fmt.Fprintf(os.Stderr, "If the browser does not open, visit:\n  %s\n\n", authURL)
	openBrowser(authURL)

	// Wait for callback (timeout after 5 minutes).
	select {
	case res := <-ch:
		if res.err != nil {
			return "", res.err
		}
		return exchangeCode(disc.TokenEndpoint, clientID, res.code, redirectURI, verifier)
	case <-time.After(5 * time.Minute):
		return "", fmt.Errorf("OIDC authentication timed out after 5 minutes")
	}
}

func exchangeCode(tokenEndpoint, clientID, code, redirectURI, verifier string) (string, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.PostForm(tokenEndpoint, url.Values{
		"grant_type":    {"authorization_code"},
		"client_id":     {clientID},
		"code":          {code},
		"redirect_uri":  {redirectURI},
		"code_verifier": {verifier},
	})
	if err != nil {
		return "", fmt.Errorf("exchanging OIDC code: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if err != nil {
		return "", fmt.Errorf("reading token response: %w", err)
	}

	var tok struct {
		IDToken string `json:"id_token"`
		Error   string `json:"error"`
		ErrDesc string `json:"error_description"`
	}
	if err := json.Unmarshal(body, &tok); err != nil {
		return "", fmt.Errorf("parsing token response: %w", err)
	}
	if tok.Error != "" {
		return "", fmt.Errorf("OIDC token exchange error %q: %s", tok.Error, tok.ErrDesc)
	}
	if tok.IDToken == "" {
		return "", fmt.Errorf("OIDC token response missing id_token")
	}
	return tok.IDToken, nil
}
