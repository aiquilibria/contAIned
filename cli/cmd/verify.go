package cmd

import (
	"fmt"
	"os"

	"github.com/fatih/color"
	"github.com/spf13/cobra"

	"contained.dev/cli/internal/sigstore"
	"contained.dev/cli/internal/workspace"
)

var verifyCmd = &cobra.Command{
	Use:   "verify [directory]",
	Short: "Verify workspace image provenance",
	Long: `Verify that the local contained:latest image matches the signed digest
recorded at init time, and that the Sigstore signature in the Rekor
transparency log is still valid.

Reports cleanly if Sigstore was disabled at init — not an error.

Examples:
  contained verify
  contained verify ./myrepo`,
	Args:         cobra.MaximumNArgs(1),
	RunE:         runVerify,
	SilenceUsage: true,
}

func init() {
	rootCmd.AddCommand(verifyCmd)
}

func runVerify(_ *cobra.Command, args []string) error {
	root, err := resolveVerifyRoot(args)
	if err != nil {
		return err
	}

	dim := color.New(color.Faint)
	bold := color.New(color.Bold)
	green := color.New(color.FgGreen)
	red := color.New(color.FgRed)

	bold.Printf("\ncontAIned verify")
	dim.Printf(" — %s\n\n", root)

	prov, err := sigstore.VerifyWorkspace(root)
	if err != nil {
		red.Printf("✗ %s\n\n", err)
		os.Exit(1)
	}

	if prov == nil {
		dim.Println("Build provenance was disabled at init — nothing to verify.")
		dim.Println("Re-run 'contained init' and enable Sigstore to record provenance.")
		return nil
	}

	digest := prov.ImageDigest
	if len(digest) > 26 {
		digest = digest[:26] + "…"
	}

	fmt.Printf("  %s Image digest matches  %s\n",
		green.Sprint("✓"), dim.Sprint(digest))
	fmt.Printf("  %s Sigstore signature verified\n\n", green.Sprint("✓"))

	fmt.Printf("  operator : %s\n", prov.OperatorIdentity)
	fmt.Printf("  issuer   : %s\n", prov.OIDCIssuer)
	fmt.Printf("  signed   : %s\n", prov.SignedAt)
	fmt.Printf("  Rekor    : entry %d  %s\n\n",
		prov.RekorLogIndex, dim.Sprint(prov.RekorEntryURL))

	return nil
}

func resolveVerifyRoot(args []string) (string, error) {
	if len(args) > 0 {
		abs, err := absolutePath(args[0])
		if err != nil {
			return "", err
		}
		return abs, nil
	}
	return workspace.FindRoot()
}
