# TGCrossChat Manager

A Telegram bot to manage multiple TGCrossChat instances that bridge Discord and Telegram.

## Quick Setup (Docker — recommended)

1. **Copy the environment file and fill in your values**:
   ```bash
   cp .env.example .env
   # edit .env: set MANAGER_BOT_TOKEN and MANAGER_USERNAME
   ```

2. **Start the manager**:
   ```bash
   docker compose up -d --build
   ```

3. **Use the bot**: Send `/start` in a DM with your manager bot.

> **Note:** The `instances/` directory is created automatically on first run and
> stores all spawned instance configs and MongoDB data. Keep it around — deleting
> it removes all managed instances.

## Manual Setup (without Docker)

1. **Configure the bot**:
   ```python
   # manager/config.py
   telegram_bot_token = "your_bot_token_from_botfather"
   telegram_username = "your_telegram_username"
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   python -m patchright install --with-deps chromium
   ```

3. **Run the manager**:
   ```bash
   cd manager
   python manager.py
   ```

4. **Use the bot**: Send `/start` in a DM with your bot

## Features

- ✅ **Create instances** - Set up new Discord ↔ Telegram bridges
- 📋 **List instances** - View all your bridges with status
- ⚙️ **Manage instances** - Pause/resume/delete individual bridges
- 📊 **View details** - See Docker container info and resource usage
- 🆔 **Get chat IDs** - Use `/id` command anywhere

## How It Works

Each instance:
- Gets its own Docker containers
- Runs independently from others
- Can be paused/resumed without data loss
- Uses unique hash-based naming

When running containerised the manager communicates with the host Docker daemon
via the mounted socket (`/var/run/docker.sock`) and uses `HOST_PWD` to resolve
volume paths so that sibling containers mount the correct host directories.

## Requirements

- Docker & Docker Compose
- Telegram bot token (from @BotFather)
