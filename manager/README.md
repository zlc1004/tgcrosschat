# TGCrossChat Manager

A Telegram bot-based management system for creating and managing multiple TGCrossChat instances.

## Setup

1. **Configure the manager bot**:
   - Create a new Telegram bot via @BotFather
   - Copy the bot token to `config.py` (`telegram_bot_token`)
   - Set your Telegram username in `config.py` (`telegram_username`)

2. **Install dependencies**:
   ```bash
   pip install python-telegram-bot==22.0
   ```

3. **Configure `config.py`**:
   ```python
   # Telegram Bot Token for the manager bot (get from @BotFather)
   telegram_bot_token = "1234567890:ABCdefGHIjklMNOpqrSTUvwxyz"

   # Telegram Username (without @) of the authorized user
   telegram_username = "yourusername"  # Your Telegram username
   ```

## Usage

1. **Start the manager bot**:
   ```bash
   cd manager
   python manager.py
   ```

2. **Interact with the bot**:
   - Send `/start` in a DM with the bot to see the management panel
   - Use `/status` for a quick overview of active instances
   - Bot only responds to DMs from the configured username

## Features

### Instance Management
- **Create Instance**: Set up a new TGCrossChat bridge
  - Supports multiple instances per user
  - Asks for Discord user token (selfbot)
  - Asks for Telegram bot token
  - Asks for Telegram topics channel ID
  - Automatically clones repository, configures environment, and starts services

- **List Instances**: View all instances with real-time status indicators
  - 🟢 Running | 🔴 Stopped | 🟡 Unknown

- **Manage Instances**: Individual instance control panel
  - **Pause/Resume**: Stop/start containers without losing data (`compose stop`/`compose up -d`)
  - **Refresh Status**: Update real-time status display
  - **Delete**: Permanently remove instance and all data
  - Real-time status monitoring

### Security
- Only responds to commands from DMs with the configured username
- Ignores all group/channel messages for security
- Each instance is isolated using Docker containers
- Automatic cleanup on failures

### Data Management
- Instances are stored in `data.json` with the structure:
  ```json
  [
    {
      "chatid": "123456789",
      "discord_token": "discord_user_token",
      "telegram_token": "telegram_bot_token",
      "topics_channel_id": "telegram_channel_id",
      "docker_stack_name": "sha256_hash"
    }
  ]
  ```

## File Structure

```
manager/
├── manager.py          # Main manager bot application
├── config.py           # Configuration file
├── data.json           # Instance data storage
├── instances/          # Directory for cloned instances
│   └── {hash}/         # Individual instance directories
└── README.md           # This file
```

## Prerequisites

- Docker and Docker Compose installed
- Git installed
- Python 3.7+ with python-telegram-bot library
- Access to create Telegram bots via @BotFather
- A Telegram account with a username (required for authorization)

## Notes

- Multiple instances can be created per user
- Instance names are generated using SHA256 hash of chat_id + discord_token + telegram_token
- All containers use unique project names to avoid conflicts
- Stopping an instance removes all associated containers and data
