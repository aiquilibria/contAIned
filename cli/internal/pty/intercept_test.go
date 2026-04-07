package pty

import (
	"os"
	"path/filepath"
	"testing"
)

func TestRewritePasteContent(t *testing.T) {
	ws := t.TempDir()
	imagesDir := filepath.Join(ws, ".images")
	if err := os.MkdirAll(imagesDir, 0o755); err != nil {
		t.Fatal(err)
	}

	// Helper: create a real file in a temp source dir.
	srcDir := t.TempDir()
	makeFile := func(name string) string {
		p := filepath.Join(srcDir, name)
		if err := os.WriteFile(p, []byte("img"), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}

	t.Run("image path is rewritten to container path", func(t *testing.T) {
		src := makeFile("photo.png")
		got := rewritePasteContent(ws, src)
		want := "/workspace/.images/photo.png"
		if got != want {
			t.Errorf("got %q, want %q", got, want)
		}
		// File should be present in .images/.
		if _, err := os.Stat(filepath.Join(imagesDir, "photo.png")); err != nil {
			t.Errorf("copied file not found: %v", err)
		}
	})

	t.Run("non-image path forwarded unchanged", func(t *testing.T) {
		src := makeFile("readme.txt")
		got := rewritePasteContent(ws, src)
		if got != src {
			t.Errorf("got %q, want %q (unchanged)", got, src)
		}
	})

	t.Run("nonexistent file forwarded unchanged", func(t *testing.T) {
		src := filepath.Join(srcDir, "ghost.png")
		got := rewritePasteContent(ws, src)
		if got != src {
			t.Errorf("got %q, want %q (unchanged)", got, src)
		}
	})

	t.Run("plain text forwarded unchanged", func(t *testing.T) {
		content := "hello world"
		got := rewritePasteContent(ws, content)
		if got != content {
			t.Errorf("got %q, want %q (unchanged)", got, content)
		}
	})

	t.Run("collision gets numeric suffix", func(t *testing.T) {
		src := makeFile("shot.png")
		// First drop creates shot.png.
		first := rewritePasteContent(ws, src)
		if first != "/workspace/.images/shot.png" {
			t.Fatalf("first rewrite: got %q", first)
		}
		// Second drop of the same name should get shot_1.png.
		second := rewritePasteContent(ws, src)
		if second != "/workspace/.images/shot_1.png" {
			t.Errorf("second rewrite: got %q, want /workspace/.images/shot_1.png", second)
		}
		// Third drop → shot_2.png.
		third := rewritePasteContent(ws, src)
		if third != "/workspace/.images/shot_2.png" {
			t.Errorf("third rewrite: got %q, want /workspace/.images/shot_2.png", third)
		}
	})

	t.Run("all supported extensions are rewritten", func(t *testing.T) {
		for _, ext := range []string{".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".pdf"} {
			src := makeFile("file" + ext)
			got := rewritePasteContent(ws, src)
			if got == src {
				t.Errorf("extension %s: expected rewrite, got unchanged", ext)
			}
		}
	})

	t.Run("relative path forwarded unchanged", func(t *testing.T) {
		content := "relative/path/image.png"
		got := rewritePasteContent(ws, content)
		if got != content {
			t.Errorf("got %q, want %q (unchanged)", got, content)
		}
	})
}

func TestCtrlV(t *testing.T) {
	ws := t.TempDir()
	if err := os.MkdirAll(filepath.Join(ws, ".images"), 0o755); err != nil {
		t.Fatal(err)
	}

	t.Run("Ctrl+V with clipboard.png injects container path", func(t *testing.T) {
		clip := filepath.Join(ws, ".images", "clipboard.png")
		if err := os.WriteFile(clip, []byte("img"), 0o644); err != nil {
			t.Fatal(err)
		}
		r, w, _ := os.Pipe()
		input := []byte{ctrlV}
		processBuffer(w, input, ws)
		w.Close()
		buf := make([]byte, 256)
		n, _ := r.Read(buf)
		got := string(buf[:n])
		want := "/workspace/.images/clipboard.png"
		if got != want {
			t.Errorf("got %q, want %q", got, want)
		}
	})

	t.Run("Ctrl+V without clipboard.png forwards 0x16 unchanged", func(t *testing.T) {
		ws2 := t.TempDir()
		if err := os.MkdirAll(filepath.Join(ws2, ".images"), 0o755); err != nil {
			t.Fatal(err)
		}
		r, w, _ := os.Pipe()
		input := []byte{ctrlV}
		processBuffer(w, input, ws2)
		w.Close()
		buf := make([]byte, 8)
		n, _ := r.Read(buf)
		if n != 1 || buf[0] != ctrlV {
			t.Errorf("got %v, want [0x16]", buf[:n])
		}
	})
}

func TestProcessBuffer(t *testing.T) {
	ws := t.TempDir()
	if err := os.MkdirAll(filepath.Join(ws, ".images"), 0o755); err != nil {
		t.Fatal(err)
	}

	srcDir := t.TempDir()
	imgPath := filepath.Join(srcDir, "test.png")
	if err := os.WriteFile(imgPath, []byte("img"), 0o644); err != nil {
		t.Fatal(err)
	}

	t.Run("non-paste bytes forwarded as-is", func(t *testing.T) {
		r, w, _ := os.Pipe()
		input := []byte("hello")
		remaining := processBuffer(w, input, ws)
		w.Close()
		buf := make([]byte, 64)
		n, _ := r.Read(buf)
		written := string(buf[:n])
		// No ESC in "hello" so all bytes are forwarded immediately.
		if written != "hello" {
			t.Errorf("written = %q, want %q", written, "hello")
		}
		if len(remaining) != 0 {
			t.Errorf("remaining = %q, want empty", remaining)
		}
	})

	t.Run("bracketed paste with image path is rewritten", func(t *testing.T) {
		r, w, _ := os.Pipe()
		input := []byte(pasteStart + imgPath + pasteEnd)
		remaining := processBuffer(w, input, ws)
		w.Close()
		if len(remaining) != 0 {
			t.Errorf("unexpected remaining bytes: %q", remaining)
		}
		buf := make([]byte, 256)
		n, _ := r.Read(buf)
		got := string(buf[:n])
		want := pasteStart + "/workspace/.images/test.png" + pasteEnd
		if got != want {
			t.Errorf("got %q, want %q", got, want)
		}
	})

	t.Run("bracketed paste with plain text forwarded unchanged", func(t *testing.T) {
		r, w, _ := os.Pipe()
		content := "just some text"
		input := []byte(pasteStart + content + pasteEnd)
		processBuffer(w, input, ws)
		w.Close()
		buf := make([]byte, 256)
		n, _ := r.Read(buf)
		got := string(buf[:n])
		want := pasteStart + content + pasteEnd
		if got != want {
			t.Errorf("got %q, want %q", got, want)
		}
	})
}
