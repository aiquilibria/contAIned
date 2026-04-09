// Package watch provides a host-side clipboard watcher that captures image
// data from the system clipboard and writes it to <workspace>/.images/clipboard.png.
// It is started by the contAIned runner alongside the container session and
// stopped automatically when the session exits.
package watch

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"

	"contained.dev/cli/internal/hostdeps"
)

const pollInterval = 500 * time.Millisecond
const toolTimeout = 400 * time.Millisecond

// Watcher polls the host clipboard for image changes.
type Watcher struct {
	workspace string
	tool      clipboardTool
	stop      chan struct{}
	done      chan struct{}
}

// clipboardTool reads the current clipboard image into a byte slice.
// Returns nil, nil when the clipboard contains no image.
type clipboardTool func() ([]byte, error)

// Start detects the available clipboard tool, then begins polling. It returns
// an error (and no Watcher) only when no clipboard tool is found; callers
// should treat this as a non-fatal warning and continue the session.
func Start(workspace string) (*Watcher, error) {
	tool, err := detectTool()
	if err != nil {
		return nil, err
	}
	w := &Watcher{
		workspace: workspace,
		tool:      tool,
		stop:      make(chan struct{}),
		done:      make(chan struct{}),
	}
	go w.run()
	return w, nil
}

// Stop signals the watcher to exit and waits for it to finish.
func (w *Watcher) Stop() {
	close(w.stop)
	<-w.done
}

func (w *Watcher) run() {
	defer close(w.done)
	var lastHash [32]byte
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-w.stop:
			return
		case <-ticker.C:
			data, err := w.tool()
			if err != nil || len(data) == 0 {
				continue
			}
			h := sha256.Sum256(data)
			if h == lastHash {
				continue
			}
			lastHash = h
			dest := filepath.Join(w.workspace, ".images", "clipboard.png")
			if err := os.WriteFile(dest, data, 0o644); err != nil {
				fmt.Fprintf(os.Stderr, "[contAIned] clipboard: write failed: %v\n", err)
			}
		}
	}
}

// EnsureDeps checks whether the host clipboard tool is available and, where
// possible, installs it automatically (macOS via Homebrew). It prints inline
// progress when an install is attempted. The returned string is a human-readable
// status suitable for display in the contained init result table. This function
// never returns an error — missing clipboard support is always non-fatal.
func EnsureDeps() string {
	switch runtime.GOOS {
	case "darwin":
		return ensureDepsDarwin()
	case "linux":
		return ensureDepsLinux()
	default:
		return "n/a"
	}
}

func ensureDepsDarwin() string {
	if _, err := exec.LookPath("pngpaste"); err == nil {
		return "ok"
	}
	return hostdeps.BrewInstall("pngpaste", "pngpaste")
}

func ensureDepsLinux() string {
	if _, err := exec.LookPath("wl-paste"); err == nil {
		return "ok"
	}
	if _, err := exec.LookPath("xclip"); err == nil {
		return "ok"
	}
	return hostdeps.AptHint("xclip")
}

// detectTool returns the first available clipboard tool for the current
// platform, or an error describing what is missing.
func detectTool() (clipboardTool, error) {
	switch runtime.GOOS {
	case "darwin":
		return detectDarwin()
	default:
		return detectLinux()
	}
}

func detectDarwin() (clipboardTool, error) {
	if path, err := exec.LookPath("pngpaste"); err == nil {
		return darwinTool(path), nil
	}
	return nil, fmt.Errorf(
		"clipboard support requires pngpaste (brew install pngpaste)",
	)
}

// darwinTool reads clipboard image data by writing to a temp file via pngpaste.
// pngpaste does not support stdout, so we use a temp file.
func darwinTool(pngpaste string) clipboardTool {
	return func() ([]byte, error) {
		tmp, err := os.CreateTemp("", "contained-clip-*.png")
		if err != nil {
			return nil, err
		}
		tmp.Close()
		defer os.Remove(tmp.Name())

		ctx, cancel := context.WithTimeout(context.Background(), toolTimeout)
		defer cancel()
		cmd := exec.CommandContext(ctx, pngpaste, tmp.Name())
		if err := cmd.Run(); err != nil {
			// pngpaste exits non-zero (or times out) when clipboard has no image.
			return nil, nil
		}
		return os.ReadFile(tmp.Name())
	}
}

func detectLinux() (clipboardTool, error) {
	// Prefer wl-paste (Wayland) then xclip (X11).
	if path, err := exec.LookPath("wl-paste"); err == nil {
		return linuxTool(path, "--type", "image/png"), nil
	}
	if path, err := exec.LookPath("xclip"); err == nil {
		return linuxTool(path, "-selection", "clipboard", "-t", "image/png", "-o"), nil
	}
	return nil, fmt.Errorf(
		"clipboard support requires wl-paste (Wayland) or xclip (X11)",
	)
}

func linuxTool(bin string, args ...string) clipboardTool {
	return func() ([]byte, error) {
		ctx, cancel := context.WithTimeout(context.Background(), toolTimeout)
		defer cancel()
		out, err := exec.CommandContext(ctx, bin, args...).Output()
		if err != nil {
			// Tool exits non-zero (or times out) when clipboard has no image.
			return nil, nil
		}
		return out, nil
	}
}
