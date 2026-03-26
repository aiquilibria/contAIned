package docker

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"

	"contained.dev/cli/internal/manifest"
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
	cfg       manifest.DockerConfig
	workspace string
	policy    manifest.PolicyConfig
}

// New creates a Runner for the given manifest and workspace root.
func New(cfg manifest.DockerConfig, workspace string, policy manifest.PolicyConfig) *Runner {
	return &Runner{cfg: cfg, workspace: workspace, policy: policy}
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
	tmpDir, provArgs, err := provenance(r.workspace)
	if err != nil {
		return err
	}
	if tmpDir != "" {
		defer os.RemoveAll(tmpDir)
	}

	args = append(args, "-it")
	args = append(args, provArgs...)
	args = append(args, image)

	cmd := exec.Command(args[0], args[1:]...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			os.Exit(exitErr.ExitCode())
		}
		return err
	}
	return nil
}

// provenance copies provenance.yaml (and optionally provenance.bundle) from
// the workspace to a temp directory and returns docker volume mount args for
// them as read-only mounts. Returns ("", nil, nil) when no provenance files
// are present.
func provenance(workspace string) (tmpDir string, args []string, err error) {
	provYAML := filepath.Join(workspace, ".contAIned", "provenance.yaml")
	if _, err := os.Stat(provYAML); os.IsNotExist(err) {
		return "", nil, nil
	}

	tmp, err := os.MkdirTemp("", "contained-prov-")
	if err != nil {
		return "", nil, fmt.Errorf("creating provenance temp dir: %w", err)
	}

	if err := copyFile(provYAML, filepath.Join(tmp, "provenance.yaml")); err != nil {
		os.RemoveAll(tmp)
		return "", nil, err
	}
	args = append(args,
		"--volume", tmp+"/provenance.yaml:/run/contained/provenance.yaml:ro",
	)

	provBundle := filepath.Join(workspace, ".contAIned", "provenance.bundle")
	if _, err := os.Stat(provBundle); err == nil {
		if err := copyFile(provBundle, filepath.Join(tmp, "provenance.bundle")); err != nil {
			os.RemoveAll(tmp)
			return "", nil, err
		}
		args = append(args,
			"--volume", tmp+"/provenance.bundle:/run/contained/provenance.bundle:ro",
		)
	}

	return tmp, args, nil
}

func copyFile(src, dst string) error {
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
