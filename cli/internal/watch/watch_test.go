package watch

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// mockTool returns a clipboardTool that cycles through the provided payloads
// on successive calls. An empty slice means "no image".
func mockTool(payloads [][]byte) (clipboardTool, *int) {
	call := 0
	return func() ([]byte, error) {
		if call >= len(payloads) {
			return nil, nil
		}
		data := payloads[call]
		call++
		return data, nil
	}, &call
}

func TestWatcherWritesClipboardPng(t *testing.T) {
	ws := t.TempDir()
	if err := os.MkdirAll(filepath.Join(ws, ".images"), 0o755); err != nil {
		t.Fatal(err)
	}

	payload := []byte("PNG\x00fake image data")
	tool, _ := mockTool([][]byte{payload})

	w := &Watcher{
		workspace: ws,
		tool:      tool,
		stop:      make(chan struct{}),
		done:      make(chan struct{}),
	}
	go w.run()

	dest := filepath.Join(ws, ".images", "clipboard.png")
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(dest); err == nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	close(w.stop)
	<-w.done

	data, err := os.ReadFile(dest)
	if err != nil {
		t.Fatalf("clipboard.png not written: %v", err)
	}
	if string(data) != string(payload) {
		t.Errorf("got %q, want %q", data, payload)
	}
}

func TestWatcherSkipsDuplicates(t *testing.T) {
	ws := t.TempDir()
	if err := os.MkdirAll(filepath.Join(ws, ".images"), 0o755); err != nil {
		t.Fatal(err)
	}

	payload := []byte("PNG\x00same data")
	// Same payload twice — second call should not overwrite.
	calls := 0
	tool := func() ([]byte, error) {
		calls++
		return payload, nil
	}

	w := &Watcher{
		workspace: ws,
		tool:      tool,
		stop:      make(chan struct{}),
		done:      make(chan struct{}),
	}
	go w.run()

	dest := filepath.Join(ws, ".images", "clipboard.png")
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(dest); err == nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	// Let it run a second poll cycle.
	time.Sleep(pollInterval + 100*time.Millisecond)
	info1, _ := os.Stat(dest)

	time.Sleep(pollInterval + 100*time.Millisecond)
	info2, _ := os.Stat(dest)

	close(w.stop)
	<-w.done

	// ModTime should not change after the first write.
	if info1 != nil && info2 != nil && info2.ModTime().After(info1.ModTime()) {
		t.Error("clipboard.png was overwritten despite identical content")
	}
}

func TestDetectToolError(t *testing.T) {
	// On any platform, if we call detectTool and no tool is found we should
	// get a descriptive error. We can't test the happy path without the real
	// binaries, but we can test the error contract.
	//
	// Swap PATH to empty so LookPath always fails.
	old := os.Getenv("PATH")
	os.Setenv("PATH", "")
	defer os.Setenv("PATH", old)

	_, err := detectTool()
	if err == nil {
		t.Error("expected error when no clipboard tool is available")
	}
}
