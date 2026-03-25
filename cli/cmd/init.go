package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"text/tabwriter"

	"github.com/fatih/color"
	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"contained.dev/cli/internal/docker"
	"contained.dev/cli/internal/manifest"
	"contained.dev/cli/internal/scaffold"
	"contained.dev/cli/internal/sigstore"
)

var initCmd = &cobra.Command{
	Use:   "init [directory]",
	Short: "Initialise a contAIned workspace",
	Long: `Initialise a contAIned workspace in DIRECTORY (default: current directory).

Scaffolds .contAIned/ and .claude/. Builds a Docker image with
the manifest baked in so policy is enforced at the highest-precedence settings
level. The image is tagged contained:<workspace-name> by default, so each
project gets its own image without manual configuration.

A manifest must be provided via --manifest or --mainlined. Run without either
flag to see a starter manifest you can save and customise.

Examples:
  contained init --manifest policy.yaml
  contained init --mainlined https://mAInlined.example.com
  contained init --manifest policy.yaml --rebuild
  contained init ./myrepo --manifest policy.yaml
  contained init --ecosystem go
  contained init --ecosystem go,python`,
	Args:         cobra.MaximumNArgs(1),
	RunE:         runInit,
	SilenceUsage: true,
}

var (
	initManifestPath string
	initmAInlinedURL string
	initEcosystem    []string
	initForce        bool
	initRebuild      bool
)

func init() {
	initCmd.Flags().StringVar(&initManifestPath, "manifest", "", "Path to manifest.yaml to bake into the image")
	initCmd.Flags().StringVar(&initmAInlinedURL, "mAInlined", "", "mAInlined URL to fetch manifest from")
	initCmd.Flags().StringSliceVar(&initEcosystem, "ecosystem", nil, "Print a repo manifest starter for these ecosystem(s) and exit (go, node, python, typescript; comma-separated or repeated)")
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

	// --ecosystem → print repo manifest starter for those ecosystems and exit.
	if len(initEcosystem) > 0 {
		return printEcosystemStarterAndExit(initEcosystem)
	}

	// Require --manifest or --mainlined. Neither → print generic starter and exit.
	if initManifestPath == "" && initmAInlinedURL == "" {
		return printStarterAndExit()
	}

	// Load the manifest.
	var manifestContent string
	var m *manifest.Manifest

	switch {
	case initmAInlinedURL != "":
		token := os.Getenv("mAInlined_TOKEN")
		dim.Printf("  Fetching manifest from %s …\n", initmAInlinedURL)
		m, err = manifest.FetchFromURL(initmAInlinedURL, token)
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

	// Merge repo-level manifest (ecosystems + QA checks) if present.
	repoManifest, err := manifest.LoadRepoManifest(target)
	if err != nil {
		return fmt.Errorf("repo manifest: %w", err)
	}
	if repoManifest != nil {
		if err := manifest.ValidateRepoManifest(repoManifest); err != nil {
			return fmt.Errorf("repo manifest validation: %w", err)
		}
		m, err = manifest.MergeRepoManifest(m, repoManifest)
		if err != nil {
			return fmt.Errorf("merging repo manifest: %w", err)
		}
		// Re-serialise so the merged state is what gets baked into the image.
		manifestContent, err = manifest.Serialise(m)
		if err != nil {
			return err
		}
		dim.Printf("  Repo manifest merged: %d ecosystem(s), %d QA check(s).\n",
			len(repoManifest.Ecosystems),
			len(repoManifest.Policy.QA.Checks),
		)
	}

	// Derive image tag from workspace name when the manifest uses the generic default.
	// This ensures each project gets its own image tag automatically, so multiple
	// projects can coexist without rebuilding overwriting a shared contained:latest.
	if m.Runtime.Docker.Image == "contained:latest" {
		m.Runtime.Docker.Image = "contained:" + sanitizeImageTag(filepath.Base(target))
		// Re-serialise so the baked manifest reflects the real tag.
		manifestContent, err = manifest.Serialise(m)
		if err != nil {
			return err
		}
		dim.Printf("  Image tag: %s (derived from workspace name)\n", m.Runtime.Docker.Image)
	}

	// Pull policy_ref/version from mAInlined if configured.
	manifestContent = docker.PolicyPull(manifestContent)

	// Build managed-settings.json from the manifest.
	managedSettings, err := docker.BuildManagedSettings(m)
	if err != nil {
		return fmt.Errorf("building managed-settings: %w", err)
	}

	// Load the CLAUDE.md template to bake into /etc/claude-code/CLAUDE.md.
	claudeMd, err := scaffold.TemplateContent("templates/CLAUDE.md")
	if err != nil {
		return fmt.Errorf("loading CLAUDE.md template: %w", err)
	}

	fmt.Println()

	// Docker: build image + ensure volume + network.
	printf := func(f string, a ...any) { fmt.Printf(f, a...) }
	imageRebuilt, err := docker.DockerSetup(
		m.Runtime.Docker,
		target,
		initRebuild,
		manifestContent,
		managedSettings,
		claudeMd,
		printf,
	)
	if err != nil {
		return fmt.Errorf("docker setup failed: %w", err)
	}
	fmt.Println()

	// Write manifest to disk before signing so VerifyWorkspace reads the correct
	// image tag. When imageRebuilt is true the in-memory manifest may have a
	// different image tag than the on-disk copy (e.g. contained:latest →
	// contained:<workspace-name>); VerifyWorkspace re-reads manifest.yaml to
	// locate the image, so it must be current before the smoke-test runs.
	// On a pure hook refresh (imageRebuilt=false) we leave the
	// existing manifest untouched unless --force was given.
	manifestDest := filepath.Join(target, ".contAIned", "manifest.yaml")
	manifestStatus, err := scaffold.WriteFile(manifestDest, manifestContent, false, imageRebuilt || initForce)
	if err != nil {
		return fmt.Errorf("writing manifest: %w", err)
	}

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

	// Managed files (hooks) — always overwritten on re-run.
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

	// Report merged ecosystems and QA checks in the results table.
	if repoManifest != nil {
		for name, version := range repoManifest.Ecosystems {
			results = append(results, result{"ecosystem: " + name, version})
		}
		for _, check := range repoManifest.Policy.QA.Checks {
			results = append(results, result{"qa check: " + check.Name, "merged"})
		}
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
	starter, err := scaffold.TemplateContent("templates/manifests/manifest_generic.yaml")
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, `Error: --manifest or --mainlined is required.

Save the following starter manifest to a file (e.g. policy.yaml), customise
it for your project, then re-run:

  contained init --manifest policy.yaml

For a repo-level manifest (ecosystems + QA only), use --ecosystem:

  contained init --ecosystem go             # or: node, python, typescript
  contained init --ecosystem go,python     # multiple ecosystems

──────────────────────────────────────────────────────────────────────────────
%s
──────────────────────────────────────────────────────────────────────────────

See docs/policy-reference.md for full schema documentation.
`, starter)
	os.Exit(1)
	return nil
}

var ecosystemTemplates = map[string]string{
	"go":         "templates/manifests/manifest_go.yaml",
	"node":       "templates/manifests/manifest_node.yaml",
	"python":     "templates/manifests/manifest_python.yaml",
	"typescript": "templates/manifests/manifest_typescript.yaml",
}

func printEcosystemStarterAndExit(ecosystems []string) error {
	supported := make([]string, 0, len(ecosystemTemplates))
	for k := range ecosystemTemplates {
		supported = append(supported, k)
	}
	sort.Strings(supported)

	for _, name := range ecosystems {
		if _, ok := ecosystemTemplates[name]; !ok {
			fmt.Fprintf(os.Stderr, "Error: unknown ecosystem %q. Supported: %s\n", name, strings.Join(supported, ", "))
			os.Exit(1)
		}
	}

	var starter string
	if len(ecosystems) == 1 {
		// Single ecosystem: print template verbatim to preserve comments.
		content, err := scaffold.TemplateContent(ecosystemTemplates[ecosystems[0]])
		if err != nil {
			return err
		}
		starter = content
	} else {
		// Multiple ecosystems: merge ecosystem declarations (union) and QA checks (concatenated).
		merged := &manifest.RepoManifest{}
		merged.Ecosystems = make(map[string]string)
		for _, name := range ecosystems {
			raw, err := scaffold.TemplateContent(ecosystemTemplates[name])
			if err != nil {
				return err
			}
			var r manifest.RepoManifest
			if err := yaml.Unmarshal([]byte(raw), &r); err != nil {
				return fmt.Errorf("parsing %s template: %w", name, err)
			}
			for k, v := range r.Ecosystems {
				merged.Ecosystems[k] = v
			}
			merged.Policy.QA.Checks = append(merged.Policy.QA.Checks, r.Policy.QA.Checks...)
		}
		out, err := yaml.Marshal(merged)
		if err != nil {
			return fmt.Errorf("serialising merged manifest: %w", err)
		}
		starter = "# merged from: " + strings.Join(ecosystems, ", ") + "\n" + string(out)
	}

	label := strings.Join(ecosystems, ", ")
	fmt.Fprintf(os.Stderr, `Repo manifest starter for ecosystem: %s

Save this to .contAIned_manifest.yaml in your repository root and commit it.
It will be merged into the mAInlined manifest on the next `+"`contained init`"+`.

──────────────────────────────────────────────────────────────────────────────
%s
──────────────────────────────────────────────────────────────────────────────

See docs/policy-reference.md for full schema documentation.
`, label, starter)
	os.Exit(0)
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

// sanitizeImageTag converts a directory name into a valid Docker tag component.
// Docker tags allow [a-zA-Z0-9_.-] but may not start with '.' or '-'.
// Anything outside that set is replaced with '-'; leading/trailing separators
// are trimmed. Falls back to "workspace" if the result is empty.
func sanitizeImageTag(name string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(name) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' || r == '.' || r == '-' {
			b.WriteRune(r)
		} else {
			b.WriteRune('-')
		}
	}
	tag := strings.Trim(b.String(), "-.")
	if tag == "" {
		return "workspace"
	}
	if len(tag) > 100 {
		tag = tag[:100]
	}
	return tag
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
		case "updated", "merged":
			statusStr = yellow.Sprint(r.status)
		case "exists", "already configured":
			statusStr = dim.Sprint(r.status + " — skipped")
		case "failed":
			statusStr = red.Sprint(r.status)
		default:
			statusStr = dim.Sprint(r.status)
		}
		fmt.Fprintf(w, "  %s\t%s\n", dim.Sprint(r.file), statusStr)
	}
	w.Flush()
}
