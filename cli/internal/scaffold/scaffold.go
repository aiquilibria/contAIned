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

// ManagedFile describes a file written by `contained init` that is always
// refreshed on re-runs (managed by contAIned, not the operator).
type ManagedFile struct {
	// RelPath is the path relative to the workspace root.
	RelPath string
	// Template is the path within the embedded templates/ FS.
	Template string
	// Executable marks the file as chmod +x.
	Executable bool
}

// ManagedFiles returns the list of files that contained init writes (and
// refreshes on every re-run). User-owned files (manifest, .env) are excluded.
func ManagedFiles() []ManagedFile {
	hook := func(name string) ManagedFile {
		return ManagedFile{
			RelPath:    filepath.Join(".contAIned", "hooks", name),
			Template:   "templates/hooks/" + name,
			Executable: name != "_policy.py",
		}
	}
	return []ManagedFile{
		hook("_policy.py"),
		hook("restrict_reads.py"),
		hook("restrict_writes.py"),
		hook("restrict_bash.py"),
		hook("restrict_network.py"),
		hook("audit.py"),
		hook("permission_request.py"),
		hook("tracer_pre.py"),
		hook("tracer_post.py"),
		hook("subagent_start.py"),
		hook("subagent_stop.py"),
		hook("summarizer.py"),
		hook("qa.py"),
		hook("user_prompt_submit.py"),
		hook("push_hook.py"),
	}
}

func fileMode(executable bool) os.FileMode {
	if executable {
		return 0o755
	}
	return 0o644
}
