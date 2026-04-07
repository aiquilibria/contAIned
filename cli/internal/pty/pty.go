// Package pty wraps an exec.Cmd in a host PTY, enabling raw-mode terminal
// passthrough with proper resize handling. It is used by the contAIned runner
// to sit between the operator's terminal and the docker process, allowing
// keystroke-level interception (Ctrl+V, bracketed paste) in later phases.
package pty

import (
	"os"
	"os/exec"
	"os/signal"
	"sync"
	"syscall"

	creackpty "github.com/creack/pty"
	"golang.org/x/term"
)

// Wrapper holds the state for a PTY-wrapped subprocess.
type Wrapper struct {
	ptm      *os.File
	cmd      *exec.Cmd
	oldState *term.State
	sigCh    chan os.Signal
	once     sync.Once
}

// Start wires cmd for interactive use with stdin interception. Only cmd.Stdin
// is routed through a PTY slave (so docker sees a TTY and enables -t mode);
// cmd.Stdout and cmd.Stderr go directly to the real host terminal, matching
// the native "docker run -it" output path and avoiding double-PTY processing
// that corrupts escape sequences.
//
// The PTY slave is placed in raw mode before the command starts so that
// docker's line discipline does not buffer, echo, or translate bytes.
//
// workspace is the host-side path to the contAIned workspace root; it is used
// by the paste interceptor to copy dropped image files into .images/.
//
// Call Wait to block until the process exits and restore the terminal.
func Start(cmd *exec.Cmd, workspace string) (*Wrapper, error) {
	ptm, pts, err := creackpty.Open()
	if err != nil {
		return nil, err
	}
	// pts is only needed by the child; close it in the parent once cmd.Start
	// has inherited it.
	defer pts.Close()

	// Sync terminal size from the host terminal to the new PTY.
	if sz, err := creackpty.GetsizeFull(os.Stdin); err == nil {
		_ = creackpty.Setsize(ptm, sz)
	}

	// Put the slave into raw mode: disables ICANON (input buffering), ECHO,
	// and ONLCR (LF→CRLF translation). Docker and the container then own all
	// terminal processing on their respective PTY layers.
	if _, err := term.MakeRaw(int(pts.Fd())); err != nil {
		ptm.Close()
		return nil, err
	}

	// Stdin through the PTY slave so docker detects a TTY and enables -t mode.
	// Stdout/stderr go directly to the host terminal — no extra PTY layer for
	// output, which is what "docker run -it" does natively.
	cmd.Stdin = pts
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setsid:  true,
		Setctty: true,
		Ctty:    0, // fd 0 (stdin = pts) becomes the controlling terminal
	}
	if err := cmd.Start(); err != nil {
		ptm.Close()
		return nil, err
	}

	// Put host stdin into raw mode so keystrokes reach us unprocessed.
	oldState, err := term.MakeRaw(int(os.Stdin.Fd()))
	if err != nil {
		ptm.Close()
		_ = cmd.Process.Kill()
		return nil, err
	}

	w := &Wrapper{
		ptm:      ptm,
		cmd:      cmd,
		oldState: oldState,
		sigCh:    make(chan os.Signal, 1),
	}

	// Enable bracketed paste mode on the host terminal so drag-and-drop and
	// paste events are wrapped in escape sequences we can intercept.
	os.Stdout.WriteString("\x1b[?2004h") //nolint:errcheck

	// Forward SIGWINCH so the container sees resize events.
	signal.Notify(w.sigCh, syscall.SIGWINCH)
	go func() {
		for range w.sigCh {
			w.syncSize()
		}
	}()

	// Host stdin → container stdin (pts), with bracketed paste interception.
	// Container output goes directly to os.Stdout — no goroutine needed.
	go copyWithIntercept(ptm, os.Stdin, workspace)

	return w, nil
}

// Wait blocks until the wrapped command exits, then restores the terminal.
// It returns the command's exit error (if any).
func (w *Wrapper) Wait() error {
	err := w.cmd.Wait()
	w.cleanup()
	return err
}

// cleanup stops signal forwarding, closes the PTY master, and restores the
// host terminal. Safe to call multiple times.
func (w *Wrapper) cleanup() {
	w.once.Do(func() {
		signal.Stop(w.sigCh)
		close(w.sigCh)
		w.ptm.Close()
		// Disable bracketed paste mode before restoring the terminal.
		os.Stdout.WriteString("\x1b[?2004l") //nolint:errcheck
		if w.oldState != nil {
			_ = term.Restore(int(os.Stdin.Fd()), w.oldState)
		}
	})
}

func (w *Wrapper) syncSize() {
	if sz, err := creackpty.GetsizeFull(os.Stdin); err == nil {
		_ = creackpty.Setsize(w.ptm, sz)
	}
}
