package workspace

import (
	"os"
	"path/filepath"
)

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
