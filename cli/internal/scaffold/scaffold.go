// Package scaffold writes contAIned workspace files from embedded templates.
package scaffold

import (
	"embed"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"
)

//go:embed all:templates
var Templates embed.FS

// WriteFile writes content to path, creating parent directories as needed.
// Returns "created", "updated", or "exists" (skipped when overwrite is false
// and the file already exists).
func WriteFile(path, content string, executable, overwrite bool) (string, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return "", err
	}

	if _, err := os.Stat(path); err == nil {
		if !overwrite {
			return "exists", nil
		}
		existing, _ := os.ReadFile(path)
		if string(existing) == content {
			return "exists", nil
		}
		if err := os.WriteFile(path, []byte(content), fileMode(executable)); err != nil {
			return "", err
		}
		return "updated", nil
	}

	if err := os.WriteFile(path, []byte(content), fileMode(executable)); err != nil {
		return "", err
	}
	return "created", nil
}

// Touch creates an empty file at path (directory marker).
// Returns "created" or "exists".
func Touch(path string) (string, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return "", err
	}
	if _, err := os.Stat(path); err == nil {
		return "exists", nil
	}
	f, err := os.Create(path)
	if err != nil {
		return "", err
	}
	f.Close()
	return "created", nil
}

// MigrateSettingsJSON renames .claude/settings.json and
// .claude/settings.local.json to timestamped backups.
// All hook registration and permissions are owned by managed-settings.json
// baked into the image; having a settings.json causes every hook to fire twice.
// Returns "migrated" or "exists" (nothing to migrate).
func MigrateSettingsJSON(target string) (string, error) {
	stamp := time.Now().UTC().Format("20060102_150405")
	migrated := false

	for _, name := range []string{"settings.json", "settings.local.json"} {
		p := filepath.Join(target, ".claude", name)
		if _, err := os.Stat(p); err == nil {
			backup := p + ".bak." + stamp
			if err := os.Rename(p, backup); err != nil {
				return "", fmt.Errorf("backing up %s: %w", p, err)
			}
			migrated = true
		}
	}

	if migrated {
		return "migrated", nil
	}
	return "exists", nil
}

// TemplateContent returns the content of a template file by its path within
// the embedded templates/ directory (e.g. "templates/hooks/audit.py").
func TemplateContent(path string) (string, error) {
	data, err := fs.ReadFile(Templates, path)
	if err != nil {
		return "", fmt.Errorf("reading embedded template %s: %w", path, err)
	}
	return string(data), nil
}

func fileMode(executable bool) os.FileMode {
	if executable {
		return 0o755
	}
	return 0o644
}
