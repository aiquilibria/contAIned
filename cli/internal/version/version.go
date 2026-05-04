// Package version holds the contAIned CLI version string.
package version

// Version is the current CLI version, used to label Docker images so
// contained init can detect stale images and trigger a rebuild.
var Version = "dev"
