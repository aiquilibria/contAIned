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
	"contained.dev/cli/internal/mainlined"
	"contained.dev/cli/internal/manifest"
	"contained.dev/cli/internal/oidc"
	"contained.dev/cli/internal/scaffold"
	"contained.dev/cli/internal/sigstore"
)

var initCmd = &cobra.Command{
	Use:   "init [directory]",
	Short: "Initialise a contAIned workspace",
	Long: `Initialise a contAIned workspace in DIRECTORY (default: current directory).

Scaffolds .contAIned/ and builds a Docker image with the operator policy
baked in at the highest-precedence settings level. Each project gets its own
image tag derived from the workspace name so multiple projects can coexist
without manual configuration.

There are two modes:

  Operator init — provide a full policy manifest to bake into the image.
  Use --manifest (local file) or --mainlined (remote policy server URL).
  Run without either flag to print a generic manifest starter you can save
  and customise.

    contained init --manifest policy.yaml
    contained init --mainlined https://mainlined.example.com
    contained init --manifest policy.yaml --rebuild
    contained init ./myrepo --manifest policy.yaml

  Repo manifest starter — print a .contAIned_manifest.yaml template and exit.
  Use --ecosystem to select one or more language runtimes. Commit the output
  to the repository root; it is merged into the operator manifest on the next
  contained init.

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
	initCmd.Flags().StringVar(&initmAInlinedURL, "mainlined", "", "mAInlined scope URL to register with (e.g. https://mainlined.example.com/org/scope)")
	// Accept the stylised form --mAInlined as an alias so both spellings work.
	initCmd.Flags().StringVar(&initmAInlinedURL, "mAInlined", "", "alias for --mainlined")
	_ = initCmd.Flags().MarkHidden("mAInlined")
	initCmd.Flags().BoolVarP(&initRebuild, "rebuild", "r", false, "Force Docker image rebuild")
	initCmd.Flags().BoolVarP(&initForce, "force", "f", false, "Re-initialise even if workspace already exists")
	initCmd.Flags().StringSliceVar(&initEcosystem, "ecosystem", nil, "Print a repo manifest starter for these ecosystem(s) and exit (go, node, python, typescript; comma-separated or repeated)")
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

	// idToken holds the OIDC ID token obtained during mAInlined registration.
	// It may be reused for Sigstore signing to avoid a second browser prompt.
	var idToken string

	// mAInlinedParsed is non-nil when --mainlined was supplied; it drives the
	// deferred registration for the --manifest + --mainlined combined flow.
	var mAInlinedParsed *mainlined.ParsedURL

	// mAInlinedUsed and mAInlinedAPIKey are set whenever mAInlined registration
	// completes (either flow). Used to intimate provenance after signing.
	var mAInlinedUsed *mainlined.ParsedURL
	var mAInlinedAPIKey string

	switch {
	case initmAInlinedURL != "":
		// Parse the mAInlined URL early so any format error surfaces immediately.
		p, err := mainlined.ParseURL(initmAInlinedURL)
		if err != nil {
			return fmt.Errorf("invalid --mainlined URL: %w", err)
		}

		// Obtain the OIDC ID token (browser flow or SIGSTORE_ID_TOKEN env var).
		dim.Printf("  Obtaining OIDC token for mAInlined …\n")
		idToken, err = oidc.GetIDToken(oidc.SigstoreIssuer, oidc.SigstoreClientID, []string{"openid", "email"})
		if err != nil {
			return fmt.Errorf("OIDC authentication: %w", err)
		}

		if initManifestPath != "" {
			// ── Flow B: --manifest + --mainlined ──────────────────────────────
			// Load the local manifest first. Registration (which computes the
			// manifest hash) is deferred until after the repo manifest merge and
			// image-tag derivation so the hash covers the fully-merged manifest.
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
			mAInlinedParsed = &p // registration runs after merge/image-tag steps

		} else {
			// ── Flow A: --mainlined alone (bootstrap) ──────────────────────────
			// No local manifest — register immediately with an empty-manifest hash.
			// The server's policy_yaml response becomes the base manifest.
			dim.Printf("  Registering with mAInlined (%s/%s) …\n", p.Org, p.Scope)
			reg, err := mainlined.Register(p, idToken, mainlined.SystemURI(p.Org, p.Scope), mainlined.ManifestHashEmpty)
			if err != nil {
				return fmt.Errorf("mAInlined registration: %w", err)
			}
			keyPath, err := mainlined.StoreAPIKey(p.Org, p.Scope, reg.APIKey)
			if err != nil {
				return fmt.Errorf("storing mAInlined API key: %w", err)
			}
			dim.Printf("  API key written to %s\n", keyPath)
			mAInlinedAPIKey = reg.APIKey
			mAInlinedUsed = &p

			m, err = manifest.Parse([]byte(reg.PolicyYAML))
			if err != nil {
				return fmt.Errorf("parsing mAInlined policy_yaml: %w", err)
			}
			if err := manifest.Validate(m); err != nil {
				return fmt.Errorf("mAInlined policy_yaml validation: %w", err)
			}
			// The registration endpoint may omit policy_version even though the
			// policy_yaml it returns contains it at policy.mAInlined.policy_version.
			// PolicyConfig.mAInlined is unexported so yaml.v3 can't unmarshal into
			// it; extract the version from the raw YAML string instead.
			policyVersion := reg.PolicyVersion
			if policyVersion == "" {
				policyVersion = manifest.ExtractPolicyVersion(reg.PolicyYAML)
			}
			m.Mainlined = manifest.MainlinedSection{
				URL:           initmAInlinedURL,
				PolicyRef:     reg.PolicyRef,
				PolicyVersion: policyVersion,
				PolicyYAML:    reg.PolicyYAML,
			}
			manifestContent, err = manifest.Serialise(m)
			if err != nil {
				return err
			}
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
	// MergeRepoManifest is always called (even with nil repo) because it
	// normalises toolchain constraint strings like ">= 1.24" to concrete
	// install versions like "1.24" before they reach GenerateToolchainsScript.
	repoManifest, err := manifest.LoadRepoManifest(target)
	if err != nil {
		return fmt.Errorf("repo manifest: %w", err)
	}
	if repoManifest != nil {
		if err := manifest.ValidateRepoManifest(repoManifest); err != nil {
			return fmt.Errorf("repo manifest validation: %w", err)
		}
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
	if repoManifest != nil {
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

	// ── Flow B: deferred mAInlined registration ─────────────────────────────
	// When --manifest + --mainlined are both specified, registration happens here
	// (after the repo manifest merge and image-tag derivation) so the manifest
	// hash sent to the server covers the fully-merged, finalised manifest.
	if mAInlinedParsed != nil {
		hash := mainlined.HashManifest(manifestContent)
		dim.Printf("  Registering with mAInlined (%s/%s) …\n", mAInlinedParsed.Org, mAInlinedParsed.Scope)
		reg, err := mainlined.Register(*mAInlinedParsed, idToken,
			mainlined.SystemURI(mAInlinedParsed.Org, mAInlinedParsed.Scope), hash)
		if err != nil {
			return fmt.Errorf("mAInlined registration: %w", err)
		}
		keyPath, err := mainlined.StoreAPIKey(mAInlinedParsed.Org, mAInlinedParsed.Scope, reg.APIKey)
		if err != nil {
			return fmt.Errorf("storing mAInlined API key: %w", err)
		}
		dim.Printf("  API key written to %s\n", keyPath)
		mAInlinedAPIKey = reg.APIKey
		mAInlinedUsed = mAInlinedParsed

		// The registration endpoint may omit policy_version; extract it from
		// the returned policy_yaml (at policy.mAInlined.policy_version) if so.
		policyVersionB := reg.PolicyVersion
		if policyVersionB == "" {
			policyVersionB = manifest.ExtractPolicyVersion(reg.PolicyYAML)
		}
		m.Mainlined = manifest.MainlinedSection{
			URL:           initmAInlinedURL,
			PolicyRef:     reg.PolicyRef,
			PolicyVersion: policyVersionB,
			PolicyYAML:    reg.PolicyYAML,
		}
		manifestContent, err = manifest.Serialise(m)
		if err != nil {
			return err
		}
	}

	// Pull policy_ref/version from the legacy policy.mAInlined section if configured.
	// This is a no-op for manifests that use the new top-level mainlined: section.
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
			idToken,                   // reuse mAInlined OIDC token (aud=sigstore); "" = cosign drives its own flow
			m.Mainlined.PolicyRef,     // included in the signed payload → bound in Rekor entry
			m.Mainlined.PolicyVersion, // included in the signed payload → bound in Rekor entry
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
			// Intimate provenance to mAInlined (fire-and-forget).
			// Only when mAInlined was used for this init; skip when sigstore.enabled: false
			// (already inside that guard) or when mAInlined was not configured.
			if mAInlinedUsed != nil {
				dim.Printf("  Intimating provenance to mAInlined …\n")
				mainlined.IntimateProvenance(
					*mAInlinedUsed,
					mAInlinedAPIKey,
					prov.ImageDigest,
					prov.RekorLogIndex,
					m.Policy.Sigstore.RekorURL,
					prov.OperatorIdentity,
					m.Mainlined.PolicyRef,
					m.Mainlined.PolicyVersion,
				)
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
