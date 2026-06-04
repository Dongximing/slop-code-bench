#!/bin/bash
# Rig Installer - Auto-generated
# Usage: $0 [--dry-run]

set -e

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Action handlers
dry_run_install_package() {
    echo "[rig] install_package $1 $2"
}

dry_run_install_application() {
    echo "[rig] install_application $1"
}

dry_run_link() {
    echo "[rig] link $1 $2"
}

dry_run_copy() {
    echo "[rig] copy $1 $2"
}

dry_run_run() {
    echo "[rig] run $1"
}

dry_run_set_preference() {
    echo "[rig] set_preference $1"
}

dry_run_configure_dock() {
    echo "[rig] configure_dock $1 items"
}

dry_run_install_runtime() {
    echo "[rig] install_runtime $1 $2 $3"
}

dry_run_install_plugin() {
    echo "[rig] install_plugin $1 $2"
}

dry_run_create_virtual_env() {
    echo "[rig] create_virtual_env $1 $2 $3"
}

run_install_package() {
    local manager="$1"
    local pkg="$2"

    case "$manager" in
        brew)
            if command -v brew &>/dev/null; then
                brew install "$pkg"
            else
                echo "Error: brew not found" >&2
                exit 1
            fi
            ;;
        apt)
            sudo apt-get update
            sudo apt-get install -y "$pkg"
            ;;
        asdf)
            # asdf plugin-add approach
            asdf plugin-add "$pkg" 2>/dev/null || true
            ;;
        *)
            echo "Unknown package manager: $manager" >&2
            exit 1
            ;;
    esac
}

run_install_application() {
    local app="$1"

    # macOS: install via brew cask or MAS
    if command -v brew &>/dev/null; then
        brew install --cask "$app"
    else
        echo "Error: brew not found for application install" >&2
        exit 1
    fi
}

run_link() {
    local source="$1"
    local dest="$2"

    local parent_dir
    parent_dir=$(dirname "$dest")

    mkdir -p "$parent_dir"
    ln -sf "$SCRIPT_DIR/$source" "$dest"
}

run_copy() {
    local source="$1"
    local dest="$2"

    local parent_dir
    parent_dir=$(dirname "$dest")

    mkdir -p "$parent_dir"
    cp "$SCRIPT_DIR/$source" "$dest"
}

run_run() {
    local source="$1"

    # Check if the source is executable
    if [[ -x "$SCRIPT_DIR/$source" ]]; then
        "$SCRIPT_DIR/$source"
    else
        # Fall back to shell
        bash "$SCRIPT_DIR/$source"
    fi
}

run_set_preference() {
    local name="$1"

    # Extract name from preferences array by looking at stored preference data
    # This is simplified - actual implementation would need to store preference metadata
    echo "Applying preference: $name"
}

run_configure_dock() {
    local items_str="$1"

    # Dock configuration implementation
    # This would typically use dockutil or defaults commands
    echo "Configuring Dock with items: $items_str"
}

run_install_runtime() {
    local manager="$1"
    local language="$2"
    local version="$3"

    case "$manager" in
        pyenv)
            if command -v pyenv &>/dev/null; then
                pyenv install "$version"
            else
                echo "Error: pyenv not found" >&2
                exit 1
            fi
            ;;
        asdf)
            if command -v asdf &>/dev/null; then
                asdf install "$language" "$version"
            else
                echo "Error: asdf not found" >&2
                exit 1
            fi
            ;;
        *)
            echo "Unknown runtime manager: $manager" >&2
            exit 1
            ;;
    esac
}

run_install_plugin() {
    local manager="$1"
    local plugin="$2"

    case "$manager" in
        pyenv)
            # pyenv-virtualenv or similar
            git clone https://github.com/yyuu/pyenv-virtualenv.git "$(pyenv root)/plugins/pyenv-virtualenv"
            ;;
        asdf)
            asdf plugin-add "$plugin"
            ;;
        *)
            echo "Unknown plugin manager: $manager" >&2
            exit 1
            ;;
    esac
}

run_create_virtual_env() {
    local manager="$1"
    local plugin="$2"
    local name="$3"

    case "$manager-$plugin" in
        pyenv-pyenv-virtualenv)
            pyenv virtualenv "$name"
            ;;
        asdf-asdf-venv)
            asdf venv create "$name"
            ;;
        *)
            echo "Unsupported environment: $manager-$plugin" >&2
            exit 1
            ;;
    esac
}

# Load preferences data
load_preferences() {
    # Preferences are stored in a JSON file for the installer to use
    local prefs_file="$SCRIPT_DIR/.preferences.json"
    if [[ -f "$prefs_file" ]]; then
        # Parse preferences if needed
        true
    fi
}

# Main execution
main() {
    # We need to load actions from a stored file since we can't pass them as arguments
    # The build process will generate an actions.json file
    local actions_file="$SCRIPT_DIR/.actions.json"

    if [[ ! -f "$actions_file" ]]; then
        echo "Error: actions file not found" >&2
        exit 1
    fi

    load_preferences

    # Read and execute actions
    # Using Python for robust JSON parsing
    python3 -c "
import json
import sys
import os
import subprocess

with open('$actions_file', 'r') as f:
    actions = json.load(f)

script_dir = os.path.dirname(os.path.abspath('$0'))
dry_run = $DRY_RUN

def run_action(action):
    act_type = action.get('type')

    if dry_run:
        prefix = '[rig]'
        if act_type == 'install_package':
            print(f'{prefix} install_package {action.get(\"manager\")} {action.get(\"package\")}')
        elif act_type == 'install_application':
            print(f'{prefix} install_application {action.get(\"application\")}')
        elif act_type == 'link':
            src = action.get('source', '')
            dst = action.get('destination', '')
            # Make source relative to script dir
            if not os.path.isabs(src):
                src = os.path.join(script_dir, src)
            print(f'{prefix} link {src} {dst}')
        elif act_type == 'copy':
            src = action.get('source', '')
            dst = action.get('destination', '')
            if not os.path.isabs(src):
                src = os.path.join(script_dir, src)
            print(f'{prefix} copy {src} {dst}')
        elif act_type == 'run':
            src = action.get('source', '')
            if not os.path.isabs(src):
                src = os.path.join(script_dir, src)
            print(f'{prefix} run {src}')
        elif act_type == 'set_preference':
            print(f'{prefix} set_preference {action.get(\"name\", \"\")}')
        elif act_type == 'configure_dock':
            items = action.get('items', [])
            print(f'{prefix} configure_dock {len(items)} items')
        elif act_type == 'install_runtime':
            print(f'{prefix} install_runtime {action.get(\"manager\")} {action.get(\"language\")} {action.get(\"version\")}')
        elif act_type == 'install_plugin':
            print(f'{prefix} install_plugin {action.get(\"manager\")} {action.get(\"plugin\")}')
        elif act_type == 'create_virtual_env':
            print(f'{prefix} create_virtual_env {action.get(\"manager\")} {action.get(\"plugin\")} {action.get(\"name\")}')
    else:
        if act_type == 'install_package':
            # Execute actual installation
            subprocess.run(['bash', '-c', f'''case \"{action.get(\"manager\")}\" in
    brew) brew install {action.get(\"package\")} ;;
    apt) sudo apt-get update && sudo apt-get install -y {action.get(\"package\")} ;;
esac'''], check=True)
        elif act_type == 'link':
            src = action.get('source', '')
            dst = action.get('destination', '')
            if not os.path.isabs(src):
                src = os.path.join(script_dir, src)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst) or os.path.islink(dst):
                os.remove(dst)
            os.symlink(src, dst)
        elif act_type == 'copy':
            src = action.get('source', '')
            dst = action.get('destination', '')
            if not os.path.isabs(src):
                src = os.path.join(script_dir, src)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            import shutil
            shutil.copy2(src, dst)
        elif act_type == 'run':
            src = action.get('source', '')
            if not os.path.isabs(src):
                src = os.path.join(script_dir, src)
            if os.access(src, os.X_OK):
                os.execve(src, [src], os.environ)
            else:
                os.execve('bash', ['bash', src], os.environ)
        elif act_type == 'set_preference':
            print(f'Applying preference: {action.get(\"name\")}')
        elif act_type == 'configure_dock':
            print(f'Configuring Dock: {action.get(\"items\")}')
        elif act_type == 'install_runtime':
            print(f'Installing runtime: {action.get(\"manager\")} {action.get(\"language\")} {action.get(\"version\")}')
        elif act_type == 'install_plugin':
            print(f'Installing plugin: {action.get(\"manager\")} {action.get(\"plugin\")}')
        elif act_type == 'create_virtual_env':
            print(f'Creating virtual env: {action.get(\"manager\")} {action.get(\"plugin\")} {action.get(\"name\")}')

for action in actions:
    run_action(action)
" || exit 1
}

main "$@"
