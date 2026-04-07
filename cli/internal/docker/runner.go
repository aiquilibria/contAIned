package docker

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"contained.dev/cli/internal/fileutil"
	"contained.dev/cli/internal/manifest"
	"contained.dev/cli/internal/pty"
	"contained.dev/cli/internal/watch"
	"gopkg.in/yaml.v3"
)

var dockerSearchPaths = []string{
	"/usr/local/bin/docker",
	"/usr/bin/docker",
	"/opt/homebrew/bin/docker",
}

// FindDockerBin locates the Docker executable via PATH, then common locations.
func FindDockerBin() (string, error) {
	return findDocker()
}

// findDocker locates the Docker executable via PATH, then common locations.
func findDocker() (string, error) {
	if p, err := exec.LookPath("docker"); err == nil {
		return p, nil
	}
	for _, p := range dockerSearchPaths {
		if info, err := os.Stat(p); err == nil && !info.IsDir() {
			return p, nil
		}
	}
	return "", fmt.Errorf(
		"docker executable not found — ensure Docker is installed and in PATH\n"+
			"  Install Docker Desktop: https://www.docker.com/products/docker-desktop/\n"+
			"  Searched: %v", dockerSearchPaths,
	)
}

// Runner orchestrates `docker run` for a contAIned session.
type Runner struct {
	cfg          manifest.ContainerConfig
	workspace    string
	policy       manifest.RuntimePolicy
	mainlinedURL string // non-empty when mAInlined is configured; drives secrets mount
}

// New creates a Runner for the given manifest and workspace root.
// mainlinedURL is the value of manifest.Init.Mainlined.URL; pass an empty string
// when mAInlined is not configured.
func New(cfg manifest.ContainerConfig, workspace string, policy manifest.RuntimePolicy, mainlinedURL string) *Runner {
	return &Runner{cfg: cfg, workspace: workspace, policy: policy, mainlinedURL: mainlinedURL}
}

// baseArgs returns the docker run arguments common to all invocations,
// excluding -it and the in-container command.
func (r *Runner) baseArgs(dockerBin string) ([]string, error) {
	apiKey := os.Getenv("ANTHROPIC_API_KEY")
	name := fmt.Sprintf("contAIned-%s-%d", filepath.Base(r.workspace), os.Getpid())

	// Ensure ~/.claude and ~/.claude.json exist on the host so that Claude
	// Code credentials persist across container runs.
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, fmt.Errorf("resolving home directory: %w", err)
	}
	claudeDir := filepath.Join(home, ".claude")
	claudeJSON := filepath.Join(home, ".claude.json")
	if err := os.MkdirAll(claudeDir, 0o700); err != nil {
		return nil, fmt.Errorf("creating %s: %w", claudeDir, err)
	}
	if _, err := os.Stat(claudeJSON); os.IsNotExist(err) {
		f, err := os.Create(claudeJSON)
		if err != nil {
			return nil, fmt.Errorf("creating %s: %w", claudeJSON, err)
		}
		f.Close()
	}

	args := []string{
		dockerBin, "run",
		"--rm",
		"--name", name,
		"--volume", r.workspace + ":/workspace",
		"--volume", claudeDir + ":/home/agent/.claude",
		"--volume", claudeJSON + ":/home/agent/.claude.json",
		"--volume", r.cfg.AgentConfigVolume + ":/home/agent/.config/agent",
		"--env", "ANTHROPIC_API_KEY=" + apiKey,
		"--network", r.cfg.Network,
		"--memory", r.cfg.Memory,
		"--cpus", strconv.Itoa(r.cfg.CPUs),
	}

	// Inject workspace .env file entries if present.
	envFile := filepath.Join(r.workspace, ".env")
	if _, err := os.Stat(envFile); err == nil {
		pairs, err := ParseEnvFile(envFile)
		if err != nil {
			return nil, fmt.Errorf("parsing .env: %w", err)
		}
		for k, v := range pairs {
			args = append(args, "--env", k+"="+v)
		}
	}

	for _, m := range r.cfg.ExtraMounts {
		expanded, err := expandHome(m, home)
		if err != nil {
			return nil, fmt.Errorf("expanding extra_mount %q: %w", m, err)
		}
		args = append(args, "--volume", expanded)
	}

	// Bind-mount extra_secrets from the manifest. Each file is mounted read-only
	// at /run/contained/secrets-env/<ENV_VAR_NAME> so the entrypoint can export
	// it without the value appearing in docker run arguments or docker inspect.
	for _, s := range r.cfg.ExtraSecrets {
		expanded, err := expandHome(s.Path, home)
		if err != nil {
			return nil, fmt.Errorf("expanding extra_secret path %q: %w", s.Path, err)
		}
		if _, statErr := os.Stat(expanded); statErr == nil {
			args = append(args, "--volume",
				expanded+":/run/contained/secrets-env/"+s.Env+":ro",
			)
		}
	}

	args = append(args, r.cfg.Image)
	return args, nil
}

// expandHome replaces a leading ~ in s with the provided home directory.
func expandHome(s, home string) (string, error) {
	if len(s) == 0 || s[0] != '~' {
		return s, nil
	}
	if len(s) > 1 && s[1] != '/' {
		return "", fmt.Errorf("~ expansion only supported for current user (got %q)", s)
	}
	return filepath.Join(home, s[2:]), nil
}

// RunRepl starts an interactive contAIned session inside the container and
// blocks until it exits. The process exits with the container's exit code.
func (r *Runner) RunRepl() error {
	dockerBin, err := findDocker()
	if err != nil {
		return err
	}

	args, err := r.baseArgs(dockerBin)
	if err != nil {
		return err
	}

	// The image name is the last element. Insert -it and any provenance
	// volume mounts before it so Docker doesn't interpret them as the
	// in-container command.
	image := args[len(args)-1]
	args = args[:len(args)-1]

	// Snapshot provenance files to a temp dir and mount read-only.
	tmpDir, provArgs, err := provenance(r.workspace, image, r.mainlinedURL)
	if err != nil {
		return err
	}
	if tmpDir != "" {
		defer os.RemoveAll(tmpDir)
	}

	// Stage a session-specific copy of the mAInlined API key so that parallel
	// sessions — even within the same workspace — never share or clobber each
	// other's credential. The copy lives in .contAIned/sessions/<pid>/ which is
	// already covered by the workspace .gitignore and is cleaned up on exit.
	sessionKeyDir, keyArgs := r.stageSessionKey()
	if sessionKeyDir != "" {
		defer os.RemoveAll(sessionKeyDir)
	}

	args = append(args, "-it")
	args = append(args, provArgs...)
	args = append(args, keyArgs...)
	args = append(args, image)

	// Prepare the .images/ directory inside the workspace. This directory is
	// accessible inside the container at /workspace/.images/ via the existing
	// workspace bind-mount and serves as the bridge for shared images.
	imagesDir := filepath.Join(r.workspace, ".images")
	if err := os.MkdirAll(imagesDir, 0o755); err != nil {
		return fmt.Errorf("creating .images dir: %w", err)
	}
	if err := ensureGitignore(r.workspace, ".images/"); err != nil {
		return fmt.Errorf("updating .gitignore: %w", err)
	}

	if watcher, err := watch.Start(r.workspace); err != nil {
		fmt.Fprintf(os.Stderr, "[contAIned] clipboard watch unavailable: %v\n", err)
	} else {
		fmt.Fprintf(os.Stderr, "[contAIned] clipboard watcher active\n")
		defer watcher.Stop()
	}

	cmd := exec.Command(args[0], args[1:]...)

	w, err := pty.Start(cmd, r.workspace)
	if err != nil {
		return fmt.Errorf("starting PTY: %w", err)
	}

	if err := w.Wait(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			os.Exit(exitErr.ExitCode())
		}
		return err
	}
	return nil
}

// stageSessionKey copies the workspace mAInlined API key to a session-specific
// directory inside .contAIned/sessions/<pid>/ and returns the directory path
// and the docker volume mount args needed to expose it at the well-known
// container path /run/contained/secrets/mainlined_api_key.
//
// If no key has been written yet (e.g. mAInlined was not configured), both
// return values are empty and the container starts without the key — hooks
// will surface a clear "mainlined_api_key not found" error at proof submission.
func (r *Runner) stageSessionKey() (sessionDir string, volumeArgs []string) {
	if r.mainlinedURL == "" {
		return "", nil
	}
	srcPath := filepath.Join(r.workspace, ".contAIned", "mainlined_api_key")
	keyData, err := os.ReadFile(srcPath)
	if err != nil {
		return "", nil
	}
	sessionDir = filepath.Join(r.workspace, ".contAIned", "sessions", fmt.Sprintf("%d", os.Getpid()))
	if err := os.MkdirAll(sessionDir, 0o700); err != nil {
		return "", nil
	}
	keyFile := filepath.Join(sessionDir, "mainlined_api_key")
	if err := os.WriteFile(keyFile, keyData, 0o600); err != nil {
		os.RemoveAll(sessionDir)
		return "", nil
	}
	return sessionDir, []string{"--volume", keyFile + ":/run/contained/secrets/mainlined_api_key:ro"}
}

// provenanceDoc is the ordered schema for /run/contained/provenance.yaml.
// Field order matches the conceptual hierarchy: operator → workspace →
// policy → image → Sigstore transparency log.
type provenanceDoc struct {
	SchemaVersion    int    `yaml:"schema_version"`
	OperatorIdentity string `yaml:"operator_identity,omitempty"`
	OIDCIssuer       string `yaml:"oidc_issuer,omitempty"`
	HostWorkspace    string `yaml:"host_workspace,omitempty"`
	MainlinedURL     string `yaml:"mainlined_url,omitempty"`
	PolicyRef        string `yaml:"policy_ref,omitempty"`
	PolicyVersion    string `yaml:"policy_version,omitempty"`
	ImageName        string `yaml:"image_name,omitempty"`
	ImageDigest      string `yaml:"image_digest,omitempty"`
	RekorLogIndex    int64  `yaml:"rekor_log_index,omitempty"`
	RekorEntryURL    string `yaml:"rekor_entry_url,omitempty"`
	SignedAt         string `yaml:"signed_at,omitempty"`
	SignedPayload    string `yaml:"signed_payload,omitempty"`
}

// provenance reads provenance.yaml from the workspace (if present), augments
// it with runtime context (image name and host workspace path), writes the
// result to a temp directory, and returns docker volume mount args for a
// read-only bind-mount into the container. The optional provenance.bundle is
// copied unchanged alongside it when present.
func provenance(workspace, image, mainlinedURL string) (tmpDir string, args []string, err error) {
	tmp, err := os.MkdirTemp("", "contained-prov-")
	if err != nil {
		return "", nil, fmt.Errorf("creating provenance temp dir: %w", err)
	}

	var doc provenanceDoc
	provYAML := filepath.Join(workspace, ".contAIned", "provenance.yaml")
	if data, readErr := os.ReadFile(provYAML); readErr == nil {
		_ = yaml.Unmarshal(data, &doc)
	} else {
		doc.SchemaVersion = 1
	}
	doc.HostWorkspace = workspace
	doc.MainlinedURL = mainlinedURL
	doc.ImageName = image

	out, err := yaml.Marshal(&doc)
	if err != nil {
		os.RemoveAll(tmp)
		return "", nil, fmt.Errorf("marshaling provenance: %w", err)
	}
	if err := os.WriteFile(filepath.Join(tmp, "provenance.yaml"), out, 0o600); err != nil {
		os.RemoveAll(tmp)
		return "", nil, err
	}
	args = append(args,
		"--volume", tmp+"/provenance.yaml:/run/contained/provenance.yaml:ro",
	)

	provBundle := filepath.Join(workspace, ".contAIned", "provenance.bundle")
	if _, err := os.Stat(provBundle); err == nil {
		if err := fileutil.CopyFile(provBundle, filepath.Join(tmp, "provenance.bundle")); err != nil {
			os.RemoveAll(tmp)
			return "", nil, err
		}
		args = append(args,
			"--volume", tmp+"/provenance.bundle:/run/contained/provenance.bundle:ro",
		)
	}

	return tmp, args, nil
}

// ensureGitignore appends entry to <workspace>/.gitignore if not already
// present, creating the file if needed.
func ensureGitignore(workspace, entry string) error {
	gitignorePath := filepath.Join(workspace, ".gitignore")
	data, err := os.ReadFile(gitignorePath)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.TrimSpace(line) == entry {
			return nil
		}
	}
	f, err := os.OpenFile(gitignorePath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = fmt.Fprintf(f, "\n# contAIned — shared images (operator drop zone)\n%s\n", entry)
	return err
}
