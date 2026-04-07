// Package fileutil provides small file-operation helpers shared across
// internal packages.
package fileutil

import (
	"io"
	"os"
)

// CopyFile copies the file at src to dst, creating dst if needed.
func CopyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	if _, err = io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
