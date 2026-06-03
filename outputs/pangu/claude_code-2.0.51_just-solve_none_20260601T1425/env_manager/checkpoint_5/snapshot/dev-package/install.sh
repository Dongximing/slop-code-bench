#!/bin/bash
# Generated installer script
# Run with --dry-run to see what would be executed

set -e

DRY_RUN=false

# Parse arguments
for arg in "$@"; do
    if [ "$arg" = "--dry-run" ]; then
        DRY_RUN=true
    fi
done

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Function to execute or dry-run a command
execute() {
    if [ "$DRY_RUN" = true ]; then
        echo "[dry-run] $*"
    else
        eval "$@"
    fi
}

# Install pycharm
if command -v brew &> /dev/null; then
    execute "brew install --cask pycharm"
elif command -v mas &> /dev/null; then
    execute "mas install $(mas search pycharm | head -1 | awk "{print $1}")"
else
    echo "Warning: No supported package manager found for pycharm"
fi

# Run command from 
execute "pip3 install --user neovim pynvim"

execute "brew install fish"
# Install iTerm2
if command -v brew &> /dev/null; then
    execute "brew install --cask iTerm2"
elif command -v mas &> /dev/null; then
    execute "mas install $(mas search iTerm2 | head -1 | awk "{print $1}")"
else
    echo "Warning: No supported package manager found for iTerm2"
fi

# Link shell/fish_config to ~/.config/fish/config.fish
execute "mkdir -p $(dirname ~/.config/fish/config.fish)"
execute "ln -sf "$SCRIPT_DIR/shell/fish_config" ~/.config/fish/config.fish"

# Install git
if command -v brew &> /dev/null; then
    execute "brew install --cask git"
elif command -v mas &> /dev/null; then
    execute "mas install $(mas search git | head -1 | awk "{print $1}")"
else
    echo "Warning: No supported package manager found for git"
fi

# Install vim
if command -v brew &> /dev/null; then
    execute "brew install --cask vim"
elif command -v mas &> /dev/null; then
    execute "mas install $(mas search vim | head -1 | awk "{print $1}")"
else
    echo "Warning: No supported package manager found for vim"
fi

# Install zsh
if command -v brew &> /dev/null; then
    execute "brew install --cask zsh"
elif command -v mas &> /dev/null; then
    execute "mas install $(mas search zsh | head -1 | awk "{print $1}")"
else
    echo "Warning: No supported package manager found for zsh"
fi

# Copy core/vimrc to ~/.vimrc
execute "mkdir -p $(dirname ~/.vimrc)"
execute "cp "$SCRIPT_DIR/core/vimrc" ~/.vimrc"

# Link core/zshrc to ~/.zshrc
execute "mkdir -p $(dirname ~/.zshrc)"
execute "ln -sf "$SCRIPT_DIR/core/zshrc" ~/.zshrc"

# Link core/gitconfig to ~/.gitconfig
execute "mkdir -p $(dirname ~/.gitconfig)"
execute "ln -sf "$SCRIPT_DIR/core/gitconfig" ~/.gitconfig"

# Set preference: 
: # TODO: Implement preference setting


echo "Installation complete (or dry-run completed)"
