package workspace

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
)

// HostConfigDir returns the operator-side config directory for a given
// workspace root: ~/.config/contained/<sha256(abs_workspace)[:16]>.
//
// The directory is keyed on the absolute workspace path, not the name, so
// two workspaces that happen to share the same base name still get distinct
// directories. The 16-hex-char prefix of the SHA-256 gives 64-bit collision
// resistance, which is sufficient for a local per-user store.
//
// The directory is not created by this function — callers that write into it
// must call os.MkdirAll themselves.
func HostConfigDir(workspaceRoot string) (string, error) {
	abs, err := filepath.Abs(workspaceRoot)
	if err != nil {
		return "", fmt.Errorf("resolving workspace path: %w", err)
	}
	h := sha256.Sum256([]byte(abs))
	id := hex.EncodeToString(h[:])[:16]
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolving home directory: %w", err)
	}
	return filepath.Join(home, ".config", "contained", id), nil
}

// FindRoot walks up from the current working directory looking for a
// .contAIned/ directory. Returns that directory as the workspace root.
// Falls back to cwd if no .contAIned/ ancestor is found (init will create
// it there).
func FindRoot() (string, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return "", err
	}

	current := filepath.Clean(cwd)
	for {
		if _, err := os.Stat(filepath.Join(current, ".contAIned")); err == nil {
			return current, nil
		}
		parent := filepath.Dir(current)
		if parent == current {
			break
		}
		current = parent
	}

	return cwd, nil
}
