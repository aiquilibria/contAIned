package scaffold

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestUpdateGitignore_NoFile_Creates(t *testing.T) {
	dir := t.TempDir()

	status, err := UpdateGitignore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if status != "created" {
		t.Errorf("status: got %q, want %q", status, "created")
	}
	data, _ := os.ReadFile(filepath.Join(dir, ".gitignore"))
	if !strings.Contains(string(data), ".contAIned/") {
		t.Error(".gitignore should contain .contAIned/")
	}
}

func TestUpdateGitignore_AlreadyHasContAInedSlash_AlreadyConfigured(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, ".gitignore"), []byte("node_modules/\n.contAIned/\n"), 0o644)

	status, err := UpdateGitignore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if status != "already configured" {
		t.Errorf("status: got %q, want %q", status, "already configured")
	}
}

func TestUpdateGitignore_AlreadyHasContAInedNoSlash_AlreadyConfigured(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, ".gitignore"), []byte(".contAIned\n"), 0o644)

	status, err := UpdateGitignore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if status != "already configured" {
		t.Errorf("status: got %q, want %q", status, "already configured")
	}
}

func TestUpdateGitignore_AlreadyHasContAInedGlob_AlreadyConfigured(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, ".gitignore"), []byte(".contAIned/*\n"), 0o644)

	status, err := UpdateGitignore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if status != "already configured" {
		t.Errorf("status: got %q, want %q", status, "already configured")
	}
}

func TestUpdateGitignore_OldPartialBlock_Upgraded(t *testing.T) {
	dir := t.TempDir()
	old := "# contAIned —\n.contAIned/audit/\n"
	os.WriteFile(filepath.Join(dir, ".gitignore"), []byte(old), 0o644)

	status, err := UpdateGitignore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if status != "updated" {
		t.Errorf("status: got %q, want %q", status, "updated")
	}
	data, _ := os.ReadFile(filepath.Join(dir, ".gitignore"))
	if strings.Contains(string(data), ".contAIned/audit/") {
		t.Error("old partial path should have been upgraded to .contAIned/")
	}
	if !strings.Contains(string(data), ".contAIned/") {
		t.Error("upgraded .gitignore should contain .contAIned/")
	}
}

func TestUpdateGitignore_ExistingFileNoContAIned_BlockAppended(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, ".gitignore"), []byte("node_modules/\n*.log\n"), 0o644)

	status, err := UpdateGitignore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if status != "updated" {
		t.Errorf("status: got %q, want %q", status, "updated")
	}
	data, _ := os.ReadFile(filepath.Join(dir, ".gitignore"))
	content := string(data)
	if !strings.Contains(content, "node_modules/") {
		t.Error("existing content should be preserved")
	}
	if !strings.Contains(content, ".contAIned/") {
		t.Error("block should have been appended with .contAIned/")
	}
}
