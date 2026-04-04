package manifest

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestFetchFromURL_ValidManifest(t *testing.T) {
	yaml := `
init:
  container:
    image: fetched:v1
    network: testnet
`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/yaml")
		w.Write([]byte(yaml))
	}))
	defer srv.Close()

	m, err := FetchFromURL(srv.URL, "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Init.Container.Image != "fetched:v1" {
		t.Errorf("image: got %q", m.Init.Container.Image)
	}
	// Defaults should still be applied.
	if m.Init.Container.Memory != "2g" {
		t.Errorf("memory default: got %q", m.Init.Container.Memory)
	}
}

func TestFetchFromURL_BearerTokenSent(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.Write([]byte("init:\n  container:\n    image: x\n    network: n\n"))
	}))
	defer srv.Close()

	if _, err := FetchFromURL(srv.URL, "mytoken"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if gotAuth != "Bearer mytoken" {
		t.Errorf("Authorization header: got %q, want %q", gotAuth, "Bearer mytoken")
	}
}

func TestFetchFromURL_NoToken_NoAuthHeader(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.Write([]byte("init:\n  container:\n    image: x\n    network: n\n"))
	}))
	defer srv.Close()

	if _, err := FetchFromURL(srv.URL, ""); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if gotAuth != "" {
		t.Errorf("expected no Authorization header, got %q", gotAuth)
	}
}

func TestFetchFromURL_Non200_ReturnsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "not found", http.StatusNotFound)
	}))
	defer srv.Close()

	_, err := FetchFromURL(srv.URL, "")
	if err == nil || !strings.Contains(err.Error(), "HTTP 404") {
		t.Fatalf("expected HTTP 404 error, got: %v", err)
	}
}

func TestFetchFromURL_InvalidYAML_ReturnsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("not: [valid: yaml: :::"))
	}))
	defer srv.Close()

	_, err := FetchFromURL(srv.URL, "")
	if err == nil {
		t.Fatal("expected parse error, got nil")
	}
}

func TestFetchFromURL_InvalidManifest_ReturnsError(t *testing.T) {
	// Unsupported toolchain → Validate must reject it.
	yaml := `
init:
  container:
    image: x
    network: n
    toolchains:
      rust: "1.80"
`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(yaml))
	}))
	defer srv.Close()

	_, err := FetchFromURL(srv.URL, "")
	if err == nil || !strings.Contains(err.Error(), "unsupported toolchain") {
		t.Fatalf("expected validation error, got: %v", err)
	}
}
