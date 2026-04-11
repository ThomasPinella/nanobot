#!/usr/bin/env bash
# Hazel installer — one command to install everything.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/ThomasPinella/hazel/main/scripts/install.sh | bash
#
# What this does:
#   1. Installs uv (Python package manager) if not present
#   2. Finds the latest Hazel release on GitHub
#   3. Installs Hazel via uv tool install
#   4. Verifies the `hazel` command is available
#
# Environment variables (optional):
#   HAZEL_VERSION    — install a specific version tag (e.g. "v0.1.4"), default: latest

set -euo pipefail

GITHUB_REPO="ThomasPinella/hazel"
VERSION="${HAZEL_VERSION:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}==>${NC} $*"; }
error() { echo -e "${RED}ERROR:${NC} $*" >&2; }

# ---------------------------------------------------------------------------
# Step 1: Ensure uv is installed
# ---------------------------------------------------------------------------
install_uv() {
    if command -v uv &>/dev/null; then
        UV_VERSION=$(uv --version 2>/dev/null | head -1)
        info "uv already installed ($UV_VERSION)"
        return 0
    fi

    info "Installing uv (Python package manager)..."

    if [[ "$(uname)" == "Darwin" ]] || [[ "$(uname)" == "Linux" ]]; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    else
        error "Unsupported platform: $(uname). Install uv manually: https://docs.astral.sh/uv/"
        exit 1
    fi

    # Source the env file uv creates so it's on PATH for this script
    if [[ -f "$HOME/.local/bin/env" ]]; then
        # shellcheck disable=SC1091
        . "$HOME/.local/bin/env"
    elif [[ -f "$HOME/.cargo/env" ]]; then
        # shellcheck disable=SC1091
        . "$HOME/.cargo/env"
    fi

    export PATH="$HOME/.local/bin:$PATH"

    if ! command -v uv &>/dev/null; then
        error "uv installed but not found on PATH. Restart your shell and try again."
        exit 1
    fi

    info "uv installed successfully"
}

# ---------------------------------------------------------------------------
# Step 2: Find the wheel URL from GitHub Releases
# ---------------------------------------------------------------------------
find_wheel_url() {
    if [[ -n "$VERSION" ]]; then
        RELEASE_TAG="$VERSION"
        # Prepend 'v' if not present
        [[ "$RELEASE_TAG" == v* ]] || RELEASE_TAG="v$RELEASE_TAG"
        API_URL="https://api.github.com/repos/$GITHUB_REPO/releases/tags/$RELEASE_TAG"
        info "Looking for Hazel $RELEASE_TAG..."
    else
        API_URL="https://api.github.com/repos/$GITHUB_REPO/releases/latest"
        info "Looking for latest Hazel release..."
    fi

    RELEASE_JSON=$(curl -sSf "$API_URL" 2>/dev/null) || {
        error "Could not fetch release from GitHub."
        error "URL: $API_URL"
        if [[ -n "$VERSION" ]]; then
            error "Check that version '$VERSION' exists at:"
            error "  https://github.com/$GITHUB_REPO/releases"
        fi
        exit 1
    }

    # Extract the .whl asset URL (prefer browser_download_url)
    WHEEL_URL=$(echo "$RELEASE_JSON" | grep -o '"browser_download_url": *"[^"]*\.whl"' | head -1 | cut -d'"' -f4)

    if [[ -z "$WHEEL_URL" ]]; then
        error "No .whl file found in the release assets."
        error "Check: https://github.com/$GITHUB_REPO/releases"
        exit 1
    fi

    RELEASE_NAME=$(echo "$RELEASE_JSON" | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
    info "Found $RELEASE_NAME: $(basename "$WHEEL_URL")"
}

# ---------------------------------------------------------------------------
# Step 3: Install Hazel
# ---------------------------------------------------------------------------
install_hazel() {
    # Check if already installed
    if uv tool list 2>/dev/null | grep -q "hazel-ai"; then
        warn "hazel-ai is already installed. Upgrading..."
        uv tool uninstall hazel-ai 2>/dev/null || true
    fi

    info "Installing Hazel..."
    uv tool install "hazel-ai @ $WHEEL_URL"
}

# ---------------------------------------------------------------------------
# Step 4: Verify installation
# ---------------------------------------------------------------------------
verify() {
    UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
    export PATH="$UV_BIN_DIR:$PATH"

    if ! command -v hazel &>/dev/null; then
        error "'hazel' binary not found even after updating PATH. Something went wrong."
        return 1
    fi

    # Ensure the bin dir is in the user's shell profile so `hazel` works in new shells
    ensure_on_path "$UV_BIN_DIR"

    HAZEL_VERSION_STR=$(hazel --version 2>/dev/null || echo "unknown")
    info "Hazel installed successfully! ($HAZEL_VERSION_STR)"
}

# ---------------------------------------------------------------------------
# Ensure a directory is on PATH permanently via shell profile
# ---------------------------------------------------------------------------
ensure_on_path() {
    local bin_dir="$1"
    local path_line="export PATH=\"$bin_dir:\$PATH\""

    # Already on PATH in a fresh login shell — nothing to do
    if bash -lc 'echo ":$PATH:"' 2>/dev/null | grep -q ":$bin_dir:"; then
        return 0
    fi

    # Find the right shell profile
    local profile=""
    local current_shell
    current_shell="$(basename "${SHELL:-/bin/bash}")"
    case "$current_shell" in
        zsh)  profile="$HOME/.zshrc" ;;
        bash)
            if [[ -f "$HOME/.bashrc" ]]; then
                profile="$HOME/.bashrc"
            else
                profile="$HOME/.profile"
            fi
            ;;
        *)    profile="$HOME/.profile" ;;
    esac

    # Don't add it twice
    if [[ -f "$profile" ]] && grep -qF "$bin_dir" "$profile" 2>/dev/null; then
        return 0
    fi

    info "Adding $bin_dir to PATH in $profile"
    echo "" >> "$profile"
    echo "# Added by Hazel installer" >> "$profile"
    echo "$path_line" >> "$profile"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    echo ""
    echo -e "${CYAN}  _   _               _  ${NC}"
    echo -e "${CYAN} | | | | __ _ _______| | ${NC}"
    echo -e "${CYAN} | |_| |/ _\` |_  / _ \\ | ${NC}"
    echo -e "${CYAN} |  _  | (_| |/ /  __/ | ${NC}"
    echo -e "${CYAN} |_| |_|\\__,_/___\\___|_| ${NC}"
    echo ""
    echo "  Hazel Installer"
    echo ""

    install_uv
    echo ""
    find_wheel_url
    echo ""
    install_hazel
    echo ""
    verify

    echo ""
    echo "---------------------------------------"
    info "Run this next to set up Hazel:"
    echo ""
    echo "  hazel onboard --wizard"
    echo ""
    echo "The wizard walks you through configuring your"
    echo "API key, model, and chat channels. Then:"
    echo ""
    echo "  hazel agent       # interactive chat"
    echo "  hazel gateway     # start the always-on daemon"
    echo ""
}

main "$@"
