package pty

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"contained.dev/cli/internal/fileutil"
)

const (
	pasteStart = "\x1b[200~"
	pasteEnd   = "\x1b[201~"
)

var imageExtRe = regexp.MustCompile(`(?i)\.(png|jpe?g|gif|webp|bmp|tiff?|pdf)$`)

const ctrlV = 0x16

// termPrint writes msg to stderr with \r\n line endings, which is required
// when the host terminal is in raw mode (as it is during a PTY session).
func termPrint(msg string) {
	fmt.Fprintf(os.Stderr, "\r\n%s\r\n", msg)
}

// copyWithIntercept forwards bytes from src to dst, with two interceptions:
//
//  1. Bracketed paste sequences (\x1b[200~…\x1b[201~): if the pasted content
//     is an absolute host image path, the file is copied into
//     <workspace>/.images/ and the path is rewritten to the container-side
//     equivalent before forwarding.
//
//  2. Ctrl+V (0x16): if <workspace>/.images/clipboard.png exists, its
//     container path is injected as text; otherwise 0x16 is forwarded
//     unchanged so Claude Code can show its normal "no image" message.
func copyWithIntercept(dst *os.File, src io.Reader, workspace string) {
	buf := make([]byte, 4096)
	var pending []byte

	for {
		n, err := src.Read(buf)
		if n > 0 {
			pending = append(pending, buf[:n]...)
			pending = processBuffer(dst, pending, workspace)
		}
		if err != nil {
			break
		}
	}

	// Forward any remaining bytes (e.g. a partial escape sequence at EOF).
	if len(pending) > 0 {
		dst.Write(pending) //nolint:errcheck
	}
}

// processBuffer scans pending for Ctrl+V bytes and complete bracketed paste
// sequences, handles each one, and returns whatever could not yet be processed.
func processBuffer(dst *os.File, pending []byte, workspace string) []byte {
	startSeq := []byte(pasteStart)
	endSeq := []byte(pasteEnd)

	for {
		// Find the next interesting byte: Ctrl+V or the start of a bracketed paste.
		startIdx := bytes.Index(pending, startSeq)
		ctrlIdx := bytes.IndexByte(pending, ctrlV)

		// Handle Ctrl+V if it appears before any bracketed paste (or if there is none).
		if ctrlIdx >= 0 && (startIdx < 0 || ctrlIdx < startIdx) {
			// Forward bytes before Ctrl+V.
			if ctrlIdx > 0 {
				dst.Write(pending[:ctrlIdx]) //nolint:errcheck
				pending = pending[ctrlIdx:]
				continue
			}
			// pending[0] == ctrlV
			clipPath := filepath.Join(workspace, ".images", "clipboard.png")
			if _, err := os.Stat(clipPath); err == nil {
				dst.Write([]byte("/workspace/.images/clipboard.png")) //nolint:errcheck
				termPrint("[contAIned] clipboard image ready")
			} else {
				dst.Write([]byte{ctrlV}) //nolint:errcheck
			}
			pending = pending[1:]
			continue
		}

		if startIdx < 0 {
			// No paste start found. Forward everything, but hold back any
			// tail that is a valid prefix of pasteStart — i.e., from the
			// last ESC byte, if the bytes from there could be the beginning
			// of \x1b[200~. Any other bytes (including other escape sequences
			// like DA responses) are forwarded intact.
			safe := len(pending)
			if lastEsc := bytes.LastIndexByte(pending, 0x1b); lastEsc >= 0 {
				tail := pending[lastEsc:]
				ps := []byte(pasteStart)
				if len(tail) <= len(ps) && bytes.Equal(ps[:len(tail)], tail) {
					safe = lastEsc
				}
			}
			if safe > 0 {
				dst.Write(pending[:safe]) //nolint:errcheck
				pending = pending[safe:]
			}
			return pending
		}

		// Forward bytes before the paste sequence.
		if startIdx > 0 {
			dst.Write(pending[:startIdx]) //nolint:errcheck
			pending = pending[startIdx:]
		}

		// Look for the matching end sequence.
		endIdx := bytes.Index(pending[len(startSeq):], endSeq)
		if endIdx < 0 {
			// End not yet received; wait for more data.
			return pending
		}

		content := string(pending[len(startSeq) : len(startSeq)+endIdx])
		rewritten := rewritePasteContent(workspace, content)
		dst.Write([]byte(pasteStart + rewritten + pasteEnd)) //nolint:errcheck

		pending = pending[len(startSeq)+endIdx+len(endSeq):]
	}
}

// rewritePasteContent returns the container-side path if content is a single
// absolute host image path that can be copied into .images/; otherwise it
// returns content unchanged.
func rewritePasteContent(workspace, content string) string {
	path := strings.TrimSpace(content)
	if !filepath.IsAbs(path) || !imageExtRe.MatchString(path) {
		return content
	}
	if _, err := os.Stat(path); err != nil {
		return content
	}
	containerPath, ok := copyToImages(workspace, path)
	if !ok {
		return content
	}
	return containerPath
}

// copyToImages copies hostPath into <workspace>/.images/, choosing a
// collision-free filename, and returns the container-side path.
func copyToImages(workspace, hostPath string) (containerPath string, ok bool) {
	imagesDir := filepath.Join(workspace, ".images")
	dest := collisionFreePath(imagesDir, filepath.Base(hostPath))
	if err := fileutil.CopyFile(hostPath, dest); err != nil {
		return "", false
	}
	containerPath = "/workspace/.images/" + filepath.Base(dest)
	termPrint("[contAIned] image copied → " + containerPath)
	return containerPath, true
}

// collisionFreePath returns a path inside dir for base that does not yet exist,
// appending _1, _2, … before the extension as needed.
func collisionFreePath(dir, base string) string {
	candidate := filepath.Join(dir, base)
	if _, err := os.Stat(candidate); os.IsNotExist(err) {
		return candidate
	}
	ext := filepath.Ext(base)
	name := strings.TrimSuffix(base, ext)
	for i := 1; ; i++ {
		candidate = filepath.Join(dir, fmt.Sprintf("%s_%d%s", name, i, ext))
		if _, err := os.Stat(candidate); os.IsNotExist(err) {
			return candidate
		}
	}
}
