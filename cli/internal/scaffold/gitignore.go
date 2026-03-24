package scaffold

import (
	"os"
	"path/filepath"
	"strings"
)

const gitignoreMarker = "# contAIned —"

// UpdateGitignore creates or updates the .gitignore in repoRoot to include
// the contAIned ignore block.
//
// Logic (mirrors init.py:_update_gitignore):
//   - No file              → write the full starter template.
//   - File exists, already contains .contAIned/ as its own line → "already configured".
//   - File exists with old partial block (.contAIned/audit/) → upgrade in-place.
//   - File exists, no contAIned section → append the block.
//
// Returns "created", "updated", or "already configured".
func UpdateGitignore(repoRoot string) (string, error) {
	gitignore := filepath.Join(repoRoot, ".gitignore")

	if _, err := os.Stat(gitignore); os.IsNotExist(err) {
		full, err := TemplateContent("templates/gitignore_full.txt")
		if err != nil {
			return "", err
		}
		if err := os.WriteFile(gitignore, []byte(full), 0o644); err != nil {
			return "", err
		}
		return "created", nil
	}

	existing, err := os.ReadFile(gitignore)
	if err != nil {
		return "", err
	}
	text := string(existing)

	// Already fully covered.
	for _, line := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == ".contAIned/" || trimmed == ".contAIned" {
			return "already configured", nil
		}
	}

	// Old partial block present — upgrade it.
	if strings.Contains(text, gitignoreMarker) {
		updated := strings.ReplaceAll(text, ".contAIned/audit/", ".contAIned/")
		if err := os.WriteFile(gitignore, []byte(updated), 0o644); err != nil {
			return "", err
		}
		return "updated", nil
	}

	// No contAIned section — append.
	block, err := TemplateContent("templates/gitignore_block.txt")
	if err != nil {
		return "", err
	}
	f, err := os.OpenFile(gitignore, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return "", err
	}
	defer f.Close()
	if _, err := f.WriteString(block); err != nil {
		return "", err
	}
	return "updated", nil
}
