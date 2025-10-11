# TGCrossChat

A Discord ↔ Telegram bridge that forwards messages between the two platforms in real-time.

Made for users that use Telegram as their main messaging app but have friends on Discord.

## 🚀 **Recommended: Use the Manager Bot**

For easy setup and management of multiple instances, use our **Telegram bot manager**:

👉 **[Go to Manager Setup →](manager/README.md)**

The manager allows you to create, control, and monitor multiple bridges through a simple Telegram interface.

---

## What TGCrossChat Does

- **🔗 Bridges Discord DMs** to Telegram forum topics automatically
- **📢 Links Discord channels** to Telegram topics for group conversations  
- **📁 Forwards files and media** between platforms
- **✏️ Syncs message edits** and replies across platforms
- **⚡ Real-time messaging** with instant synchronization

## Manual Setup (Alternative)

If you prefer manual setup instead of using the manager:

### Prerequisites
- Docker & Docker Compose
- Discord user token (selfbot)
- Telegram bot token (from @BotFather)
- Telegram topics channel ID

### Environment Setup
1. **Copy environment template**:
   ```bash
   cp .env.example .env
   ```

2. **Configure your tokens** in `.env`:
   ```bash
   # Discord user token (not bot token!)
   DISCORD_TOKEN=your_discord_user_token_here

   # Telegram bot token from @BotFather
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

   # Telegram channel ID where topics will be created
   TOPICS_CHANNEL_ID=your_telegram_topics_channel_id_here
   ```

### Running the Bridge
```bash
# Start the bridge
docker compose up --build

# Run in background
docker compose up -d --build

# Stop the bridge
docker compose down
```

The bridge will be available and start forwarding messages automatically.

## How It Works

1. **Discord DMs** → Automatically creates Telegram topics for each Discord user
2. **Discord Channels** → Use `/connect` command to link specific channels to topics
3. **Message Sync** → All messages, files, edits, and replies are forwarded both ways
4. **User Mapping** → Discord users are mapped to Telegram topics for organization

## Getting Tokens

### Discord Token
⚠️ **This uses a Discord selfbot (user token), not a bot token**
1. Open Discord in browser
2. Press F12 → Network tab
3. Send any message
4. Find request with Authorization header
5. Copy the token (starts with your user ID)

### Telegram Bot Token
1. Message @BotFather on Telegram
2. Send `/newbot` and follow instructions
3. Copy the provided token
4. Make sure to add the bot to your channel with admin rights
5. Make sure to disable "Group Privacy" in BotFather settings

### Telegram Channel ID
1. Create a Telegram channel/group
2. Add @@JsonDumpBot to the channel
3. Use `/start` command to get the channel ID

## Docker Services

- **tgcrosschat** - Main bridge application
- **mongo** - Database for storing mappings
- **mongo-express** - Web UI for database (optional)

## Commands

- `/ping` - Test bot connectivity
- `/data` - Show current channel/topic info
- `/connect` - Link Discord channel to Telegram topic
- `/unlink` - Remove Discord channel link

## Notes

- Each Discord user gets their own Telegram topic
- Channel connections persist until manually unlinked
- All data is stored in MongoDB for reliability
- Bridge works bidirectionally (Discord ↔ Telegram)

## Troubleshooting

- **"Missing environment variables"** → Check your `.env` file
- **"MongoDB connection failed"** → Ensure Docker is running
- **"Discord login failed"** → Verify your user token is correct
- **"Bot not responding"** → Check bot token and permissions

---

## 🎛️ **Still Prefer the Manager?**

The [Telegram Manager Bot](manager/README.md) handles all of this automatically and lets you manage multiple bridges easily!
