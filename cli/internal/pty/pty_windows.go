// PTY-based stdin interception is not supported on Windows. This stub provides
// the same Start/Wait interface so the rest of the CLI compiles unchanged;
// docker is run with stdio connected directly to the host terminal.
package pty

import (
	"os"
	"os/exec"
	"sync"
)

// Wrapper is a pass-through on Windows — no PTY or raw-mode handling.
type Wrapper struct {
	cmd  *exec.Cmd
	once sync.Once
}

// Start runs cmd with stdio wired directly to the host terminal. Bracketed
// paste interception and clipboard support are unavailable on Windows.
func Start(cmd *exec.Cmd, _ string) (*Wrapper, error) {
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return &Wrapper{cmd: cmd}, nil
}

// Wait blocks until the command exits.
func (w *Wrapper) Wait() error {
	err := w.cmd.Wait()
	w.cleanup()
	return err
}

func (w *Wrapper) cleanup() {
	w.once.Do(func() {}) // nothing to restore on Windows
}
