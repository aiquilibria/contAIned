package manifest

import (
	"fmt"
	"io"
	"net/http"
	"time"

	"gopkg.in/yaml.v3"
)

// FetchFromURL retrieves a manifest YAML from url, parses and validates it,
// and returns the result. token is passed as a Bearer token if non-empty
// (used for private mAInlined policy URLs via mAInlined_TOKEN env var).
func FetchFromURL(url, token string) (*Manifest, error) {
	client := &http.Client{Timeout: 15 * time.Second}

	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("building request: %w", err)
	}
	req.Header.Set("Accept", "application/yaml, text/yaml, text/plain")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetching manifest from %s: %w", url, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("fetching manifest from %s: HTTP %d", url, resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20)) // 1 MB limit
	if err != nil {
		return nil, fmt.Errorf("reading manifest response: %w", err)
	}

	var m Manifest
	if err := yaml.Unmarshal(body, &m); err != nil {
		return nil, fmt.Errorf("parsing manifest from %s: %w", url, err)
	}

	applyDefaults(&m)

	if err := Validate(&m); err != nil {
		return nil, fmt.Errorf("manifest from %s is invalid: %w", url, err)
	}

	return &m, nil
}
