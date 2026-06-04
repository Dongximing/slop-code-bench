#!/bin/bash
# Rig installer - generated build artifact
# Runs from package directory

set -euo pipefail

DRY_RUN="${DRY_RUN:-}"

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Change to script directory to ensure relative paths work
cd "$SCRIPT_DIR"

log_action() {
    local prefix="[rig]"
    local action="$1"
    shift
    if [ -n "$DRY_RUN" ]; then
        echo "$prefix $action $*"
    fi
}

execute_install() {
    local manager="$1"
    local target="$2"
    case "$manager" in
        brew)
            brew install "$target"
            ;;
        apt)
            sudo apt-get install -y "$target"
            ;;
        yum)
            sudo yum install -y "$target"
            ;;
        *)
            echo "Unknown manager: $manager"
            exit 1
            ;;
    esac
}

install_package() {
    local manager="$1"
    local package="$2"
    log_action "install_package" "$manager" "$package"
    if [ -z "$DRY_RUN" ]; then
        execute_install "$manager" "$package"
    fi
}

install_application() {
    local manager="$1"
    local application="$2"
    log_action "install_application" "$manager" "$application"
    if [ -z "$DRY_RUN" ]; then
        execute_install "$manager" "$application"
    fi
}

link_action() {
    local source="$1"
    local destination="$2"
    log_action "link" "$source" "$destination"
    if [ -z "$DRY_RUN" ]; then
        mkdir -p "$(dirname "$destination")" && ln -sf "$SCRIPT_DIR/$source" "$destination"
    fi
}

copy_action() {
    local source="$1"
    local destination="$2"
    log_action "copy" "$source" "$destination"
    if [ -z "$DRY_RUN" ]; then
        mkdir -p "$(dirname "$destination")" && cp -f "$SCRIPT_DIR/$source" "$destination"
    fi
}

run_action() {
    local source="$1"
    log_action "run" "$source"
    if [ -z "$DRY_RUN" ]; then
        bash "$SCRIPT_DIR/$source"
    fi
}

set_preference() {
    log_action "set_preference" "$1"
}

configure_dock() {
    log_action "configure_dock" "$@"
}

install_runtime() {
    local manager="$1"
    local language="$2"
    local version="$3"
    log_action "install_runtime" "$manager" "$language" "$version"
}

install_plugin() {
    local manager="$1"
    local plugin="$2"
    log_action "install_plugin" "$manager" "$plugin"
}

create_virtual_env() {
    local manager="$1"
    local plugin="$2"
    local name="$3"
    log_action "create_virtual_env" "$manager" "$plugin" "$name"
}

# Main execution
if [ -n "$DRY_RUN" ]; then
    echo "Dry run mode - no actions will be executed"
fi

# Execute actions from plan

install_package "brew" "coreutils"
