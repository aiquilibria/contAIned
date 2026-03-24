package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"text/tabwriter"

	"github.com/fatih/color"
	"github.com/spf13/cobra"

	"contained.dev/cli/internal/docker"
	"contained.dev/cli/internal/manifest"
	"contained.dev/cli/internal/scaffold"
	"contained.dev/cli/internal/sigstore"
)

var initCmd = &cobra.Command{
	Use:   "init [directory]",
	Short: "Initialise a contAIned workspace",
	Long: `Initialise a contAIned workspace in DIRECTORY (default: current directory).

Scaffolds .contAIned/, .claude/, and CLAUDE.md. Builds the contained:latest
Docker image with the manifest baked in so policy is enforced at the
highest-precedence settings level.

A manifest must be provided via --manifest or --mainlined. Run without either
flag to see a starter manifest you can save and customise.

Examples:
  contained init --manifest policy.yaml
  contained init --mainlined https://mainlined.example.com
  contained init --manifest policy.yaml --rebuild
  contained init ./myrepo --manifest policy.yaml`,
	Args:         cobra.MaximumNArgs(1),
	RunE:         runInit,
	SilenceUsage: true,
}

var (
	initManifestPath string
	initMainlinedURL string
	initForce        bool
	initRebuild      bool
)

func init() {
	initCmd.Flags().StringVar(&initManifestPath, "manifest", "", "Path to manifest.yaml to bake into the image")
	initCmd.Flags().StringVar(&initMainlinedURL, "mainlined", "", "Mainlined URL to fetch manifest from")
	initCmd.Flags().BoolVarP(&initForce, "force", "f", false, "Re-initialise even if workspace already exists")
	initCmd.Flags().BoolVarP(&initRebuild, "rebuild", "r", false, "Force Docker image rebuild")
	rootCmd.AddCommand(initCmd)
}

func runInit(_ *cobra.Command, args []string) error {
	// Resolve target directory.
	target, err := resolveInitTarget(args)
	if err != nil {
		return err
	}

	dim := color.New(color.Faint)
	bold := color.New(color.Bold)
	bold.Printf("\ncontAIned init")
	dim.Printf(" — %s\n\n", target)

	// Require --manifest or --mainlined. Neither → print starter and exit.
	if initManifestPath == "" && initMainlinedURL == "" {
		return printStarterAndExit()
	}

	// Load the manifest.
	var manifestContent string
	var m *manifest.Manifest

	switch {
	case initMainlinedURL != "":
		token := os.Getenv("MAINLINED_TOKEN")
		dim.Printf("  Fetching manifest from %s …\n", initMainlinedURL)
		m, err = manifest.FetchFromURL(initMainlinedURL, token)
		if err != nil {
			return fmt.Errorf("fetching manifest: %w", err)
		}
		// Re-serialise for baking into the image.
		manifestContent, err = manifest.Serialise(m)
		if err != nil {
			return err
		}

	case initManifestPath != "":
		raw, err := os.ReadFile(initManifestPath)
		if err != nil {
			return fmt.Errorf("reading manifest %s: %w", initManifestPath, err)
		}
		m, err = manifest.Parse(raw)
		if err != nil {
			return fmt.Errorf("parsing manifest: %w", err)
		}
		if err := manifest.Validate(m); err != nil {
			return fmt.Errorf("manifest validation: %w", err)
		}
		manifestContent = string(raw)
		dim.Printf("  Using manifest: %s\n\n", initManifestPath)
	}

	// Pull policy_ref/version from mainlined if configured.
	manifestContent = docker.PolicyPull(manifestContent)

	// Build managed-settings.json from the manifest.
	managedSettings, err := docker.BuildManagedSettings(m)
	if err != nil {
		return fmt.Errorf("building managed-settings: %w", err)
	}

	// Locate contAIned Python source for local builds.
	source := docker.FindSource()
	if source != "" {
		dim.Printf("  Local source detected: %s\n", source)
	} else {
		dim.Printf("  No local source found — will install contained from PyPI.\n")
	}
	fmt.Println()

	// Docker: build image + ensure volume + network.
	printf := func(f string, a ...any) { fmt.Printf(f, a...) }
	imageRebuilt, err := docker.DockerSetup(
		m.Runtime.Docker,
		target,
		source,
		initRebuild,
		manifestContent,
		managedSettings,
		printf,
	)
	if err != nil {
		return fmt.Errorf("docker setup failed: %w", err)
	}
	fmt.Println()

	// Sigstore image signing — when enabled and image was (re)built.
	if m.Policy.Sigstore.Enabled && imageRebuilt {
		bundleDest := filepath.Join(target, ".contAIned", "provenance.bundle")
		fmt.Print("  Signing image with Sigstore …")
		prov, err := sigstore.SignImage(
			m.Runtime.Docker.Image,
			m.Policy.Sigstore.RekorURL,
			m.Policy.Sigstore.FulcioURL,
			bundleDest,
		)
		if err != nil {
			fmt.Printf(" warning\n  Warning: image signing failed — workspace will function but lacks Sigstore provenance.\n  %v\n", err)
		} else {
			fmt.Printf(" done\n")
			if err := sigstore.WriteProvenance(target, prov); err != nil {
				fmt.Printf("  Warning: writing provenance.yaml failed: %v\n", err)
			}
			// Smoke-test: verify the provenance we just wrote.
			fmt.Print("  Verifying provenance …")
			if _, verr := sigstore.VerifyWorkspace(target); verr != nil {
				fmt.Printf(" warning\n  Warning: post-sign verification failed: %v\n", verr)
			} else {
				fmt.Printf(" ok\n")
			}
		}
	}

	var results []result

	// Git repo.
	gitStatus, err := ensureGitRepo(target)
	if err != nil {
		gitStatus = "failed"
	}
	results = append(results, result{".git/", gitStatus})

	gitRoot := findGitRoot(target)
	alreadyInit := isAlreadyInit(target)

	// Managed files (hooks, CLAUDE.md) — always overwritten on re-run.
	for _, mf := range scaffold.ManagedFiles() {
		content, err := scaffold.TemplateContent(mf.Template)
		if err != nil {
			return err
		}
		absPath := filepath.Join(target, mf.RelPath)
		status, err := scaffold.WriteFile(absPath, content, mf.Executable, alreadyInit || initForce)
		if err != nil {
			return fmt.Errorf("writing %s: %w", mf.RelPath, err)
		}
		results = append(results, result{mf.RelPath, status})
	}

	// Manifest — write only on first init or --force; never overwrite on refresh.
	manifestDest := filepath.Join(target, ".contAIned", "manifest.yaml")
	manifestStatus, err := scaffold.WriteFile(manifestDest, manifestContent, false, initForce)
	if err != nil {
		return fmt.Errorf("writing manifest: %w", err)
	}
	results = append(results, result{".contAIned/manifest.yaml", manifestStatus})

	// Directory markers.
	markerStatus, err := scaffold.Touch(filepath.Join(target, ".contAIned", "audit", ".gitkeep"))
	if err != nil {
		return err
	}
	results = append(results, result{".contAIned/audit/.gitkeep", markerStatus})

	// .gitignore — update in git root if available, otherwise workspace root.
	giRoot := target
	if gitRoot != "" {
		giRoot = gitRoot
	}
	giStatus, err := scaffold.UpdateGitignore(giRoot)
	if err != nil {
		return fmt.Errorf("updating .gitignore: %w", err)
	}
	relGI, _ := filepath.Rel(target, filepath.Join(giRoot, ".gitignore"))
	results = append(results, result{relGI, giStatus})

	// settings.json migration.
	settingsStatus, err := scaffold.MigrateSettingsJSON(target)
	if err != nil {
		return fmt.Errorf("migrating settings.json: %w", err)
	}
	if settingsStatus == "migrated" {
		results = append(results, result{".claude/settings.json", settingsStatus})
	}

	fmt.Println()
	printResultTable(results)
	fmt.Println()

	_ = gitRoot // used via findGitRoot above
	return nil
}

// ── helpers ───────────────────────────────────────────────────────────────────

type result struct {
	file   string
	status string
}

func resolveInitTarget(args []string) (string, error) {
	dir := "."
	if len(args) > 0 {
		dir = args[0]
	}
	abs, err := filepath.Abs(dir)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(abs, 0o755); err != nil {
		return "", fmt.Errorf("creating directory %s: %w", abs, err)
	}
	return abs, nil
}

func printStarterAndExit() error {
	starter, err := scaffold.TemplateContent("templates/manifest_starter.yaml")
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, `Error: --manifest or --mainlined is required.

Save the following starter manifest to a file (e.g. policy.yaml), customise
it for your project, then re-run:

  contained init --manifest policy.yaml

──────────────────────────────────────────────────────────────────────────────
%s
──────────────────────────────────────────────────────────────────────────────

See docs/policy-reference.md for full schema documentation.
`, starter)
	os.Exit(1)
	return nil
}

func isAlreadyInit(target string) bool {
	for _, p := range []string{
		filepath.Join(target, ".contAIned", "manifest.yaml"),
		filepath.Join(target, ".contAIned", "policy", "manifest.yaml"),
	} {
		if _, err := os.Stat(p); err == nil {
			return true
		}
	}
	return false
}

func ensureGitRepo(path string) (string, error) {
	if _, err := os.Stat(filepath.Join(path, ".git")); err == nil {
		return "exists", nil
	}
	out, err := exec.Command("git", "init").Output()
	if err != nil {
		return "", fmt.Errorf("git init: %s", strings.TrimSpace(string(out)))
	}
	return "created", nil
}

func findGitRoot(path string) string {
	current := filepath.Clean(path)
	for {
		if _, err := os.Stat(filepath.Join(current, ".git")); err == nil {
			return current
		}
		parent := filepath.Dir(current)
		if parent == current {
			return ""
		}
		current = parent
	}
}

func printResultTable(results []result) {
	green := color.New(color.FgGreen)
	yellow := color.New(color.FgYellow)
	dim := color.New(color.Faint)
	red := color.New(color.FgRed)

	w := tabwriter.NewWriter(os.Stdout, 0, 0, 3, ' ', 0)
	fmt.Fprintf(w, "  %s\t%s\n", dim.Sprint("File"), dim.Sprint("Status"))
	for _, r := range results {
		var statusStr string
		switch r.status {
		case "created", "migrated":
			statusStr = green.Sprint(r.status)
		case "updated":
			statusStr = yellow.Sprint(r.status)
		case "exists", "already configured":
			statusStr = dim.Sprint(r.status + " — skipped")
		case "failed":
			statusStr = red.Sprint(r.status)
		default:
			statusStr = r.status
		}
		fmt.Fprintf(w, "  %s\t%s\n", dim.Sprint(r.file), statusStr)
	}
	w.Flush()
}
