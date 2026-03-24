package docker

import (
	"os"
	"path/filepath"
	"testing"
)

func writeEnvFile(t *testing.T, content string) string {
	t.Helper()
	f := filepath.Join(t.TempDir(), ".env")
	if err := os.WriteFile(f, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return f
}

func TestParseEnvFile_BasicAssignments(t *testing.T) {
	f := writeEnvFile(t, "FOO=bar\nBAZ=qux\n")
	m, err := ParseEnvFile(f)
	if err != nil {
		t.Fatal(err)
	}
	if m["FOO"] != "bar" {
		t.Errorf("FOO: got %q", m["FOO"])
	}
	if m["BAZ"] != "qux" {
		t.Errorf("BAZ: got %q", m["BAZ"])
	}
}

func TestParseEnvFile_CommentsAndBlankLinesSkipped(t *testing.T) {
	f := writeEnvFile(t, "# this is a comment\n\nKEY=value\n")
	m, err := ParseEnvFile(f)
	if err != nil {
		t.Fatal(err)
	}
	if len(m) != 1 {
		t.Errorf("expected 1 entry, got %d: %v", len(m), m)
	}
	if m["KEY"] != "value" {
		t.Errorf("KEY: got %q", m["KEY"])
	}
}

func TestParseEnvFile_ExportPrefix_Stripped(t *testing.T) {
	f := writeEnvFile(t, "export TOKEN=abc123\n")
	m, err := ParseEnvFile(f)
	if err != nil {
		t.Fatal(err)
	}
	if m["TOKEN"] != "abc123" {
		t.Errorf("TOKEN: got %q", m["TOKEN"])
	}
}

func TestParseEnvFile_DoubleQuotedValue_Unquoted(t *testing.T) {
	f := writeEnvFile(t, `KEY="hello world"`)
	m, err := ParseEnvFile(f)
	if err != nil {
		t.Fatal(err)
	}
	if m["KEY"] != "hello world" {
		t.Errorf("KEY: got %q", m["KEY"])
	}
}

func TestParseEnvFile_SingleQuotedValue_Unquoted(t *testing.T) {
	f := writeEnvFile(t, "KEY='hello world'\n")
	m, err := ParseEnvFile(f)
	if err != nil {
		t.Fatal(err)
	}
	if m["KEY"] != "hello world" {
		t.Errorf("KEY: got %q", m["KEY"])
	}
}

func TestParseEnvFile_ValueContainsEquals(t *testing.T) {
	// Only the first '=' is the delimiter; the rest is part of the value.
	f := writeEnvFile(t, "URL=https://example.com?a=1&b=2\n")
	m, err := ParseEnvFile(f)
	if err != nil {
		t.Fatal(err)
	}
	if m["URL"] != "https://example.com?a=1&b=2" {
		t.Errorf("URL: got %q", m["URL"])
	}
}

func TestParseEnvFile_EmptyValue(t *testing.T) {
	f := writeEnvFile(t, "EMPTY=\n")
	m, err := ParseEnvFile(f)
	if err != nil {
		t.Fatal(err)
	}
	if v, ok := m["EMPTY"]; !ok || v != "" {
		t.Errorf("EMPTY: got %q, present=%v", v, ok)
	}
}

func TestParseEnvFile_LineWithoutEquals_Skipped(t *testing.T) {
	f := writeEnvFile(t, "NOEQUALS\nKEY=val\n")
	m, err := ParseEnvFile(f)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := m["NOEQUALS"]; ok {
		t.Error("expected NOEQUALS to be skipped")
	}
}

func TestParseEnvFile_MissingFile_ReturnsError(t *testing.T) {
	_, err := ParseEnvFile("/nonexistent/.env")
	if err == nil {
		t.Fatal("expected error for missing file, got nil")
	}
}
