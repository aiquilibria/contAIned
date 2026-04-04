package cmd

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/fatih/color"
	"github.com/spf13/cobra"

	"contained.dev/cli/internal/docker"
	"contained.dev/cli/internal/manifest"
	"contained.dev/cli/internal/sigstore"
	ver "contained.dev/cli/internal/version"
	"contained.dev/cli/internal/workspace"
)

var version = ver.Version

var rootCmd = &cobra.Command{
	Use:     "contained",
	Short:   "contAIned — a contAIned coding agent CLI",
	Version: version,
	// No subcommand → start a session.
	RunE: runRepl,
	// Don't print usage on runtime errors (only on flag/arg errors).
	SilenceUsage: true,
}

func init() {
	// Cobra adds --version automatically when Version is set.
	// Hide the auto-generated completion command — it is not part of the
	// contAIned UX and would confuse operators seeing it in `--help`.
	rootCmd.CompletionOptions.DisableDefaultCmd = true
}

// Execute is the entry point called from main.
func Execute() {
	if err := rootCmd.Execute(); err != nil {
		os.Exit(1)
	}
}

func runRepl(_ *cobra.Command, _ []string) error {
	printSplash()

	root, err := workspace.FindRoot()
	if err != nil {
		return fmt.Errorf("finding workspace root: %w", err)
	}

	m, err := manifest.Load(root)
	if err != nil {
		return err
	}

	if err := manifest.Validate(m); err != nil {
		return fmt.Errorf("manifest validation: %w", err)
	}

	if m.Init.Sigstore.Enabled {
		dim := color.New(color.Faint)
		dim.Print("[contAIned] verifying provenance … ")
		if _, err := sigstore.VerifyWorkspace(root); err != nil {
			fmt.Printf("\n")
			return fmt.Errorf(
				"Sigstore verification failed — session blocked\n"+
					"  %w\n"+
					"  Run 'contained init' to rebuild and re-sign",
				err,
			)
		}
		dim.Println("✓")
	}

	printRuntimeBanner(root, m)

	runner := docker.New(m.Init.Container, root, m.Runtime, m.Init.Mainlined.URL)
	return runner.RunRepl()
}

func printSplash() {
	green := color.New(color.FgGreen, color.Bold)
	red := color.New(color.FgRed, color.Bold)
	dim := color.New(color.Faint)

	fmt.Printf("\n%s%s%s  %s\n\n",
		green.Sprint("cont["),
		red.Sprint("AI"),
		green.Sprint("]ned"),
		dim.Sprint("take back control of your coding agent!"),
	)
}

func absolutePath(p string) (string, error) {
	abs, err := filepath.Abs(p)
	if err != nil {
		return "", fmt.Errorf("resolving path %s: %w", p, err)
	}
	return abs, nil
}

func printRuntimeBanner(root string, m *manifest.Manifest) {
	dim := color.New(color.Faint)
	dim.Printf("[contAIned] runtime: docker (%s)\n", m.Init.Container.Image)
	dim.Printf("[contAIned] workspace: %s\n\n", root)
}
