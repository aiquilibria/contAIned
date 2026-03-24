package scaffold

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── WriteFile ─────────────────────────────────────────────────────────────────

func TestWriteFile_NewFile_Created(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "hello.txt")

	status, err := WriteFile(path, "hello", false, false)
	if err != nil {
		t.Fatal(err)
	}
	if status != "created" {
		t.Errorf("status: got %q, want %q", status, "created")
	}
	data, _ := os.ReadFile(path)
	if string(data) != "hello" {
		t.Errorf("content: got %q", data)
	}
}

func TestWriteFile_CreatesParentDirs(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "a", "b", "c.txt")

	if _, err := WriteFile(path, "x", false, false); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Errorf("file not created: %v", err)
	}
}

func TestWriteFile_ExistingFile_OverwriteFalse_SkipsWrite(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "f.txt")
	os.WriteFile(path, []byte("original"), 0o644)

	status, err := WriteFile(path, "changed", false, false)
	if err != nil {
		t.Fatal(err)
	}
	if status != "exists" {
		t.Errorf("status: got %q, want %q", status, "exists")
	}
	data, _ := os.ReadFile(path)
	if string(data) != "original" {
		t.Error("file should not have been modified")
	}
}

func TestWriteFile_ExistingFile_SameContent_OverwriteTrue_ReturnsExists(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "f.txt")
	os.WriteFile(path, []byte("same"), 0o644)

	status, err := WriteFile(path, "same", false, true)
	if err != nil {
		t.Fatal(err)
	}
	if status != "exists" {
		t.Errorf("status: got %q, want %q", status, "exists")
	}
}

func TestWriteFile_ExistingFile_DifferentContent_OverwriteTrue_Updated(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "f.txt")
	os.WriteFile(path, []byte("old"), 0o644)

	status, err := WriteFile(path, "new", false, true)
	if err != nil {
		t.Fatal(err)
	}
	if status != "updated" {
		t.Errorf("status: got %q, want %q", status, "updated")
	}
	data, _ := os.ReadFile(path)
	if string(data) != "new" {
		t.Errorf("content: got %q", data)
	}
}

func TestWriteFile_ExecutableBit(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "script.sh")

	if _, err := WriteFile(path, "#!/bin/sh\n", true, false); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode()&0o111 == 0 {
		t.Errorf("file should be executable, got mode %v", info.Mode())
	}
}

// ── Touch ─────────────────────────────────────────────────────────────────────

func TestTouch_NewFile_Created(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "marker")

	status, err := Touch(path)
	if err != nil {
		t.Fatal(err)
	}
	if status != "created" {
		t.Errorf("status: got %q, want %q", status, "created")
	}
	if _, err := os.Stat(path); err != nil {
		t.Errorf("file not found after Touch: %v", err)
	}
}

func TestTouch_ExistingFile_ReturnsExists(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "marker")
	os.WriteFile(path, []byte("x"), 0o644)

	status, err := Touch(path)
	if err != nil {
		t.Fatal(err)
	}
	if status != "exists" {
		t.Errorf("status: got %q, want %q", status, "exists")
	}
}

// ── MigrateSettingsJSON ───────────────────────────────────────────────────────

func TestMigrateSettingsJSON_NoFiles_ReturnsExists(t *testing.T) {
	dir := t.TempDir()
	status, err := MigrateSettingsJSON(dir)
	if err != nil {
		t.Fatal(err)
	}
	if status != "exists" {
		t.Errorf("status: got %q, want %q", status, "exists")
	}
}

func TestMigrateSettingsJSON_SettingsJSON_Migrated(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	os.MkdirAll(claudeDir, 0o755)
	os.WriteFile(filepath.Join(claudeDir, "settings.json"), []byte("{}"), 0o644)

	status, err := MigrateSettingsJSON(dir)
	if err != nil {
		t.Fatal(err)
	}
	if status != "migrated" {
		t.Errorf("status: got %q, want %q", status, "migrated")
	}
	// Original should be gone.
	if _, err := os.Stat(filepath.Join(claudeDir, "settings.json")); !os.IsNotExist(err) {
		t.Error("settings.json should have been renamed")
	}
	// A .bak file should exist.
	entries, _ := os.ReadDir(claudeDir)
	hasBak := false
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "settings.json.bak.") {
			hasBak = true
		}
	}
	if !hasBak {
		t.Error("expected a .bak backup file")
	}
}

func TestMigrateSettingsJSON_BothFiles_Migrated(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	os.MkdirAll(claudeDir, 0o755)
	os.WriteFile(filepath.Join(claudeDir, "settings.json"), []byte("{}"), 0o644)
	os.WriteFile(filepath.Join(claudeDir, "settings.local.json"), []byte("{}"), 0o644)

	status, err := MigrateSettingsJSON(dir)
	if err != nil {
		t.Fatal(err)
	}
	if status != "migrated" {
		t.Errorf("status: got %q, want %q", status, "migrated")
	}
	if _, err := os.Stat(filepath.Join(claudeDir, "settings.local.json")); !os.IsNotExist(err) {
		t.Error("settings.local.json should have been renamed")
	}
}

// ── TemplateContent ───────────────────────────────────────────────────────────

func TestTemplateContent_KnownFile_ReturnsContent(t *testing.T) {
	content, err := TemplateContent("templates/CLAUDE.md")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "contAIned") {
		t.Error("CLAUDE.md content should mention contAIned")
	}
}

func TestTemplateContent_UnknownFile_ReturnsError(t *testing.T) {
	_, err := TemplateContent("templates/nonexistent.txt")
	if err == nil {
		t.Fatal("expected error for nonexistent template")
	}
}

// ── ManagedFiles ──────────────────────────────────────────────────────────────

func TestManagedFiles_PolicyNotExecutable(t *testing.T) {
	for _, f := range ManagedFiles() {
		if strings.HasSuffix(f.RelPath, "_policy.py") && f.Executable {
			t.Error("_policy.py should not be executable")
		}
	}
}

func TestManagedFiles_HooksAreExecutable(t *testing.T) {
	for _, f := range ManagedFiles() {
		if strings.HasPrefix(f.RelPath, ".contAIned/hooks/") &&
			!strings.HasSuffix(f.RelPath, "_policy.py") &&
			!f.Executable {
			t.Errorf("hook %q should be executable", f.RelPath)
		}
	}
}

func TestManagedFiles_AllTemplatesEmbedded(t *testing.T) {
	for _, f := range ManagedFiles() {
		if _, err := TemplateContent(f.Template); err != nil {
			t.Errorf("template %q not found in embedded FS: %v", f.Template, err)
		}
	}
}
