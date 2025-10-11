# TGCrossChat Manager

A Telegram bot to manage multiple TGCrossChat instances that bridge Discord and Telegram.

## Quick Setup

1. **Configure the bot**:
   ```python
   # manager/config.py
   telegram_bot_token = "your_bot_token_from_botfather"
   telegram_username = "your_telegram_username"
   ```

2. **Install dependencies**:
   ```bash
   pip install python-telegram-bot==22.0
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

## Requirements

- Docker & Docker Compose
- Git
- Python 3.7+
- Telegram bot token (from @BotFather)
