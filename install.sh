#!/bin/bash

set -e

REPO="aiquilibria/contAIned"
BINARY_NAME="contained"
INSTALL_DIR="/usr/local/bin"

# Check for required dependencies
DOWNLOADER=""
if command -v curl >/dev/null 2>&1; then
    DOWNLOADER="curl"
elif command -v wget >/dev/null 2>&1; then
    DOWNLOADER="wget"
else
    echo "Either curl or wget is required but neither is installed" >&2
    exit 1
fi

download() {
    local url="$1"
    local out="$2"
    if [ "$DOWNLOADER" = "curl" ]; then
        [ -n "$out" ] && curl -fsSL -o "$out" "$url" || curl -fsSL "$url"
    else
        [ -n "$out" ] && wget -q -O "$out" "$url" || wget -q -O - "$url"
    fi
}

# Detect OS
case "$(uname -s)" in
    Darwin) OS="darwin" ;;
    Linux)  OS="linux"  ;;
    MINGW*|MSYS*|CYGWIN*)
        echo "Windows is not supported by this script." >&2
        echo "Download the binary directly from https://github.com/${REPO}/releases" >&2
        exit 1 ;;
    *)
        echo "Unsupported operating system: $(uname -s)" >&2
        exit 1 ;;
esac

# Detect architecture
case "$(uname -m)" in
    x86_64|amd64)  ARCH="amd64" ;;
    arm64|aarch64) ARCH="arm64" ;;
    *)
        echo "Unsupported architecture: $(uname -m)" >&2
        exit 1 ;;
esac

# Detect Rosetta 2 on macOS: if running as x86_64 under Rosetta on an ARM Mac,
# download the native arm64 binary instead
if [ "$OS" = "darwin" ] && [ "$ARCH" = "amd64" ]; then
    if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = "1" ]; then
        ARCH="arm64"
    fi
fi

# Resolve version — accept an optional first argument (e.g. "v0.2.0" or "0.2.0"),
# otherwise fetch the latest release tag from the GitHub API
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    VERSION=$(download "https://api.github.com/repos/${REPO}/releases/latest" \
        | grep '"tag_name"' \
        | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')
    if [ -z "$VERSION" ]; then
        echo "Failed to determine latest release version" >&2
        exit 1
    fi
fi

# Normalise: VERSION_TAG is the bare semver (no leading v), VERSION_REF has the v prefix
VERSION_TAG="${VERSION#v}"
VERSION_REF="v${VERSION_TAG}"

FILENAME="${BINARY_NAME}_${VERSION_TAG}_${OS}_${ARCH}"
BASE_URL="https://github.com/${REPO}/releases/download/${VERSION_REF}"

TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_WORK"' EXIT

echo "Downloading contAIned ${VERSION_REF} (${OS}/${ARCH})..."

if ! download "${BASE_URL}/${FILENAME}" "${TMPDIR_WORK}/${FILENAME}"; then
    echo "Download failed. Check that ${VERSION_REF} exists at https://github.com/${REPO}/releases" >&2
    exit 1
fi

download "${BASE_URL}/checksums.txt" "${TMPDIR_WORK}/checksums.txt"

# Verify SHA-256
EXPECTED=$(grep "[[:space:]]${FILENAME}$" "${TMPDIR_WORK}/checksums.txt" | awk '{print $1}')
if [ -z "$EXPECTED" ]; then
    echo "Checksum entry not found for ${FILENAME}" >&2
    exit 1
fi

if [ "$OS" = "darwin" ]; then
    ACTUAL=$(shasum -a 256 "${TMPDIR_WORK}/${FILENAME}" | cut -d' ' -f1)
else
    ACTUAL=$(sha256sum "${TMPDIR_WORK}/${FILENAME}" | cut -d' ' -f1)
fi

if [ "$ACTUAL" != "$EXPECTED" ]; then
    echo "Checksum verification failed" >&2
    exit 1
fi

chmod +x "${TMPDIR_WORK}/${FILENAME}"

# Install — prefer /usr/local/bin, fall back to ~/.local/bin without requiring sudo
if [ -w "$INSTALL_DIR" ]; then
    mv "${TMPDIR_WORK}/${FILENAME}" "${INSTALL_DIR}/${BINARY_NAME}"
elif command -v sudo >/dev/null 2>&1; then
    sudo mv "${TMPDIR_WORK}/${FILENAME}" "${INSTALL_DIR}/${BINARY_NAME}"
else
    INSTALL_DIR="$HOME/.local/bin"
    mkdir -p "$INSTALL_DIR"
    mv "${TMPDIR_WORK}/${FILENAME}" "${INSTALL_DIR}/${BINARY_NAME}"
    echo "Note: installed to ${INSTALL_DIR} — ensure it is on your PATH"
fi

echo ""
echo "✅ contAIned ${VERSION_REF} installed to ${INSTALL_DIR}/${BINARY_NAME}"
echo ""
echo "Next steps:"
echo "  contained init --ecosystem go        # print a starter manifest for your stack"
echo "  contained init --manifest policy.yaml  # scaffold workspace and build image"
echo "  contained                              # start a session"
echo ""
