#!/usr/bin/bash

# Kyber-Chat TUI Installer
# Interactive setup with gum for pretty UI

set -e

# Colors
GUM_INPUT_BORDER="#ff00ff"
GUM_INPUT_PROMPT="#00ff00"
GUM_CONFIRM_PROMPT="#ff00ff"
GUM_CONFIRM_ABORTED="#ff0000"
GUM_CONFIRM_SELECTED="#00ff00"

BOLD="\033[1m"
DIM="\033[2m"
NC="\033[0m"

KYBER_CHAT_DIR="$HOME/.kyber-chat"
SERVICE_FILE="$HOME/.config/systemd/user/kyber-chat.service"

# Logo art
print_logo() {
    echo ""
    gum style --foreground "#ff00ff" --bold "   ██████╗ ██████╗ ███████╗ ██████╗ ██████╗ ██████╗ ███████╗"
    gum style --foreground "#00ffff" --bold "  ██╔════╝██╔═══██╗██╔════╝██╔════╝██╔═══██╗██╔══██╗██╔════╝"
    gum style --foreground "#00ff00" --bold "  ██║     ██║   ██║███████╗██║     ██║   ██║██║  ██║█████╗  "
    gum style --foreground "#ffff00" --bold "  ██║     ██║   ██║╚════██║██║     ██║   ██║██║  ██║██╔══╝  "
    gum style --foreground "#ff9900" --bold "  ╚██████╗╚██████╔╝███████║╚██████╗╚██████╔╝██████╔╝███████╗"
    gum style --foreground "#ff0000" --bold "   ╚═════╝ ╚═════╝ ╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝"
    gum style --foreground "#ff00ff" --bold "                              C H A T"
    echo ""
}

# Check dependencies
check_deps() {
    if ! command -v gum &> /dev/null; then
        echo "gum not found - installing..."
        if command -v brew &> /dev/null; then
            brew install gum
        elif command -v apt &> /dev/null; then
            sudo apt update && sudo apt install -y gum
        else
            echo "please install gum first: https://github.com/charmbracelet/gum"
            exit 1
        fi
    fi

    if ! command -v uv &> /dev/null; then
        echo "uv not found - installing..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
}

# Resolve source version from local checkout.
get_version() {
    if [ -f "pyproject.toml" ]; then
        awk -F'"' '/^version = "/ { print $2; exit }' pyproject.toml
        return
    fi
    if [ -f "kyber/__init__.py" ]; then
        awk -F'"' '/^__version__ = "/ { print $2; exit }' kyber/__init__.py
        return
    fi
    echo "dev"
}

# Resolve currently installed version from package metadata.
get_installed_version() {
    local py_bin=""
    if command -v python3 >/dev/null 2>&1; then
        py_bin="python3"
    elif command -v python >/dev/null 2>&1; then
        py_bin="python"
    else
        echo "not-installed"
        return
    fi

    local installed
    installed=$("$py_bin" - <<'PY'
import importlib.metadata as md

for name in ("kyber-chat", "kyber"):
    try:
        print(md.version(name))
        break
    except md.PackageNotFoundError:
        continue
else:
    print("not-installed")
PY
)
    echo "${installed:-not-installed}"
}

# Welcome screen
welcome() {
    local src_version
    src_version="$(get_version)"

    print_logo
    gum style --foreground "#00ff00" --bold "  welcome to the kyber-chat interactive installer 💎"
    gum style --dim "  installer package version: $src_version"
    echo ""
    gum style --dim "  this will set up kyber-chat with your custom configuration"
    echo ""
}

# Step 1: Install location
get_install_location() {
    INSTALL_DIR=$(gum input \
        --placeholder "$HOME/.kyber-chat" \
        --prompt "install location: " \
        --prompt.foreground "$GUM_INPUT_PROMPT" \
        --border.foreground "$GUM_INPUT_BORDER") || INSTALL_DIR="$HOME/.kyber-chat"

    echo "$INSTALL_DIR"
}

# Step 2: Discord config
get_discord_config() {
    echo ""
    gum style --foreground "#00ffff" --bold "discord configuration"
    echo ""

    USE_DISCORD=$(gum confirm --prompt.foreground "$GUM_CONFIRM_PROMPT" --selected.background "$GUM_CONFIRM_SELECTED" --aborted.background "$GUM_CONFIRM_ABORTED" "enable discord?" && echo "true" || echo "false")

    if [ "$USE_DISCORD" = "true" ]; then
        DISCORD_TOKEN=$(gum input \
            --placeholder "your discord bot token" \
            --prompt "discord token: " \
            --prompt.foreground "$GUM_INPUT_PROMPT" \
            --border.foreground "$GUM_INPUT_BORDER" --password)
    else
        DISCORD_TOKEN=""
    fi

    echo "$USE_DISCORD|$DISCORD_TOKEN"
}

# Step 3: Telegram config
get_telegram_config() {
    echo ""
    gum style --foreground "#00ffff" --bold "telegram configuration"
    echo ""

    USE_TELEGRAM=$(gum confirm --prompt.foreground "$GUM_CONFIRM_PROMPT" --selected.background "$GUM_CONFIRM_SELECTED" --aborted.background "$GUM_CONFIRM_ABORTED" "enable telegram?" && echo "true" || echo "false")

    if [ "$USE_TELEGRAM" = "true" ]; then
        TELEGRAM_TOKEN=$(gum input \
            --placeholder "your telegram bot token" \
            --prompt "telegram token: " \
            --prompt.foreground "$GUM_INPUT_PROMPT" \
            --border.foreground "$GUM_INPUT_BORDER" --password)
    else
        TELEGRAM_TOKEN=""
    fi

    echo "$USE_TELEGRAM|$TELEGRAM_TOKEN"
}

# Step 4: LLM config
get_llm_config() {
    echo ""
    gum style --foreground "#00ffff" --bold "llm configuration"
    echo ""

    PROVIDER=$(gum choose \
        --cursor "▶" \
        --header="select your llm provider:" \
        --header.foreground "$GUM_INPUT_PROMPT" \
        "groq" "openai" "anthropic" "ollama" "none") || PROVIDER="groq"

    API_KEY=""
    if [ "$PROVIDER" != "ollama" ] && [ "$PROVIDER" != "none" ]; then
        API_KEY=$(gum input \
            --placeholder "your api key" \
            --prompt "$PROVIDER api key: " \
            --prompt.foreground "$GUM_INPUT_PROMPT" \
            --border.foreground "$GUM_INPUT_BORDER" --password)
    fi

    MODEL=""
    if [ "$PROVIDER" != "none" ]; then
        MODEL=$(gum input \
            --placeholder "default model (press enter for default)" \
            --prompt "model: " \
            --prompt.foreground "$GUM_INPUT_PROMPT" \
            --border.foreground "$GUM_INPUT_BORDER") || MODEL=""
    fi

    BASE_URL=""
    if [ "$PROVIDER" = "ollama" ]; then
        BASE_URL=$(gum input \
            --placeholder "http://localhost:11434/v1" \
            --prompt "ollama url: " \
            --prompt.foreground "$GUM_INPUT_PROMPT" \
            --border.foreground "$GUM_INPUT_BORDER") || BASE_URL="http://localhost:11434/v1"
    fi

    echo "$PROVIDER|$API_KEY|$MODEL|$BASE_URL"
}

# Step 5: Service setup
get_service_config() {
    echo ""
    gum style --foreground "#00ffff" --bold "service configuration"
    echo ""

    SETUP_SERVICE=$(gum confirm --prompt.foreground "$GUM_CONFIRM_PROMPT" --selected.background "$GUM_CONFIRM_SELECTED" --aborted.background "$GUM_CONFIRM_ABORTED" "install as systemd service?" && echo "true" || echo "false")
    echo "$SETUP_SERVICE"
}

# Summary
show_summary() {
    local install_dir=$1
    local discord_use=$2
    local telegram_use=$3
    local provider=$4

    echo ""
    gum style --foreground "#ff00ff" --bold "installation summary"
    echo ""

    gum format "install location: \`$install_dir\`"
    [ "$discord_use" = "true" ] && gum format "discord: **enabled**" || gum format "discord: disabled"
    [ "$telegram_use" = "true" ] && gum format "telegram: **enabled**" || gum format "telegram: disabled"
    gum format "llm provider: \`$provider\`"
    echo ""
}

# Install
do_install() {
    local install_dir=$1
    local discord_config=$2
    local telegram_config=$3
    local llm_config=$4
    local service_config=$5

    local discord_use="${discord_config%%|*}"
    local discord_token="${discord_config#*|}"
    local telegram_use="${telegram_config%%|*}"
    local telegram_token="${telegram_config#*|}"
    local provider="${llm_config%%|*}"
    local llm_rest="${llm_config#*|}"
    local api_key="${llm_rest%%|*}"
    local llm_rest2="${llm_rest#*|}"
    local model="${llm_rest2%%|*}"
    local base_url="${llm_rest2#*|}"
    local target_version
    local before_version
    local after_version

    target_version="$(get_version)"
    before_version="$(get_installed_version)"

    # Create install dir
    echo ""
    gum spin --spinner dot --title "creating install directory..." -- bash -c "mkdir -p '$install_dir'"

    # Install Python package
    gum spin --spinner dot --title "installing kyber-chat v${target_version}..." -- bash -c "uv pip install . --upgrade"
    after_version="$(get_installed_version)"

    # Create config
    gum spin --spinner dot --title "creating configuration..." -- bash -c "cat > '$install_dir/config.yaml' << 'EOF'
# Kyber-Chat Configuration
llm:
  provider: $provider
  model: ${model:-auto}
  api_key: ${api_key:-}
  base_url: ${base_url:-}

platforms:
  discord:
    enabled: $discord_use
    token: ${discord_token:-}
  telegram:
    enabled: $telegram_use
    token: ${telegram_token:-}

logging:
  level: INFO
  file: $install_dir/kyber-chat.log
EOF
"

    # Create env file
    gum spin --spinner dot --title "creating environment file..." -- bash -c "cat > '$install_dir/.env' << 'EOF'
KYBER_CHAT_CONFIG=$install_dir/config.yaml
${discord_token:+DISCORD_TOKEN=$discord_token}
${telegram_token:+TELEGRAM_TOKEN=$telegram_token}
${api_key:+LLM_API_KEY=$api_key}
EOF
"

    # Setup service
    if [ "$service_config" = "true" ]; then
        gum spin --spinner dot --title "setting up systemd service..." -- bash -c "
        mkdir -p '$HOME/.config/systemd/user'
        cat > '$SERVICE_FILE' << 'EOF'
[Unit]
Description=Kyber-Chat Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=$install_dir
Environment=KYBER_CHAT_CONFIG=$install_dir/config.yaml
${discord_token:+Environment=DISCORD_TOKEN=$discord_token}
${telegram_token:+Environment=TELEGRAM_TOKEN=$telegram_token}
${api_key:+Environment=LLM_API_KEY=$api_key}
ExecStart=$(which python) -m kyber_chat
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

        systemctl --user daemon-reload
        systemctl --user enable kyber-chat 2>/dev/null || true
        "
    fi

    echo ""
    gum style --foreground "#00ff00" --bold "  ✓ installation complete!"
    if [ "$before_version" = "not-installed" ]; then
        gum style --dim "  installed version: $after_version"
    else
        gum style --dim "  upgraded version: $before_version -> $after_version"
    fi
    echo ""

    if [ "$service_config" = "true" ]; then
        gum style --foreground "#00ffff" "  start the service:"
        echo "  systemctl --user start kyber-chat"
        echo ""
        gum style --foreground "#00ffff" "  check status:"
        echo "  systemctl --user status kyber-chat"
        echo ""
    else
        gum style --foreground "#00ffff" "  run manually:"
        echo "  cd '$install_dir'"
        echo "  export KYBER_CHAT_CONFIG='$install_dir/config.yaml'"
        echo "  python -m kyber_chat"
        echo ""
    fi
}

# Main
main() {
    check_deps
    welcome

    INSTALL_DIR=$(get_install_location)
    DISCORD_CONFIG=$(get_discord_config)
    TELEGRAM_CONFIG=$(get_telegram_config)
    LLM_CONFIG=$(get_llm_config)
    SERVICE_CONFIG=$(get_service_config)

    show_summary "$INSTALL_DIR" "${DISCORD_CONFIG%%|*}" "${TELEGRAM_CONFIG%%|*}" "${LLM_CONFIG%%|*}"

    echo ""
    gum confirm --prompt.foreground "$GUM_CONFIRM_PROMPT" --selected.background "$GUM_CONFIRM_SELECTED" --aborted.background "$GUM_CONFIRM_ABORTED" "proceed with installation?" || {
        echo ""
        gum style --foreground "#ff0000" "installation cancelled"
        exit 0
    }

    do_install "$INSTALL_DIR" "$DISCORD_CONFIG" "$TELEGRAM_CONFIG" "$LLM_CONFIG" "$SERVICE_CONFIG"
}

main
