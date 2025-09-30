import logging
import os
import discord
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import asyncio
from pymongo import MongoClient
from datetime import datetime
import threading

# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Get tokens from environment variables
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TOPICS_CHANNEL_ID = int(os.getenv('TOPICS_CHANNEL_ID'))

if not DISCORD_TOKEN or not TELEGRAM_BOT_TOKEN or not TOPICS_CHANNEL_ID:
    raise ValueError("Missing required environment variables. Check your .env file.")

# MongoDB connection (using Docker DNS)
mongo_client = MongoClient('mongodb://mongo:27017/')
db = mongo_client.tgcrosschat

# Collections
mappings_collection = db.mappings  # topic_id <-> discord_user_id mappings
messages_collection = db.messages  # message sync tracking

# Initialize bots
discord_client = discord.Client()
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

class MessageBridge:
    def __init__(self):
        self.telegram_bot = telegram_app.bot
        
    async def get_or_create_topic(self, username: str, user_id: int) -> int:
        """Get existing topic ID for user or create a new one"""
        # Check if mapping exists
        mapping = mappings_collection.find_one({"discord_user_id": user_id})
        if mapping:
            return mapping["telegram_topic_id"]
        
        try:
            # Create a new topic for this user
            topic = await self.telegram_bot.create_forum_topic(
                chat_id=TOPICS_CHANNEL_ID,
                name=f"DM with {username}"
            )
            
            # Store mapping in database
            mapping_doc = {
                "discord_user_id": user_id,
                "discord_username": username,
                "telegram_topic_id": topic.message_thread_id,
                "created_at": datetime.utcnow()
            }
            mappings_collection.insert_one(mapping_doc)
            
            logger.info(f"Created new topic for {username}: {topic.message_thread_id}")
            return topic.message_thread_id
        except Exception as e:
            logger.error(f"Failed to create topic for {username}: {e}")
            raise
            
    async def get_discord_user_from_topic(self, topic_id: int) -> dict:
        """Get Discord user info from Telegram topic ID"""
        mapping = mappings_collection.find_one({"telegram_topic_id": topic_id})
        return mapping
        
    async def forward_discord_to_telegram(self, message: discord.Message):
        """Forward Discord DM to Telegram topic"""
        username = message.author.name
        user_display_name = message.author.display_name
        user_id = message.author.id
        
        try:
            # Get or create topic for this user
            topic_id = await self.get_or_create_topic(username, user_id)
            
            # Prepare the message content
            content = f"**{user_display_name}** (@{username}):\n{message.content}"
            
            # Send message to Telegram topic
            telegram_msg = await self.telegram_bot.send_message(
                chat_id=TOPICS_CHANNEL_ID,
                message_thread_id=topic_id,
                text=content,
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Store message mapping
            message_doc = {
                "message_content": message.content,
                "discord_channel_id": message.channel.id,
                "discord_message_id": message.id,
                "telegram_channel_id": TOPICS_CHANNEL_ID,
                "telegram_topic_id": topic_id,
                "telegram_message_id": telegram_msg.message_id,
                "direction": "discord_to_telegram",
                "timestamp": datetime.utcnow()
            }
            messages_collection.insert_one(message_doc)
            
            # Handle attachments
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith("image/"):
                    await self.telegram_bot.send_photo(
                        chat_id=TOPICS_CHANNEL_ID,
                        message_thread_id=topic_id,
                        photo=attachment.url,
                        caption=f"Image from {user_display_name}"
                    )
                else:
                    await self.telegram_bot.send_document(
                        chat_id=TOPICS_CHANNEL_ID,
                        message_thread_id=topic_id,
                        document=attachment.url,
                        caption=f"File from {user_display_name}: {attachment.filename}"
                    )
                    
            logger.info(f"Forwarded DM from {username} to topic {topic_id}")
            
        except Exception as e:
            logger.error(f"Failed to forward Discord message from {username}: {e}")
            
    async def forward_telegram_to_discord(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Forward Telegram topic message to Discord DM"""
        if not update.message or not update.message.message_thread_id:
            return
            
        # Only process messages in the topics channel
        if update.message.chat_id != TOPICS_CHANNEL_ID:
            return
            
        # Don't forward bot's own messages
        if update.message.from_user.is_bot:
            return
            
        topic_id = update.message.message_thread_id
        
        try:
            # Get Discord user from topic mapping
            mapping = await self.get_discord_user_from_topic(topic_id)
            if not mapping:
                logger.warning(f"No Discord user mapping found for topic {topic_id}")
                return
                
            discord_user_id = mapping["discord_user_id"]
            
            # Get Discord user
            discord_user = await discord_client.fetch_user(discord_user_id)
            if not discord_user:
                logger.error(f"Could not find Discord user {discord_user_id}")
                return
                
            # Send message to Discord DM
            content = update.message.text or "[Media/File]"
            await discord_user.send(content)
            
            # Store message mapping
            message_doc = {
                "message_content": content,
                "discord_channel_id": discord_user.dm_channel.id if discord_user.dm_channel else None,
                "discord_message_id": None,  # We don't get the sent message object back
                "telegram_channel_id": TOPICS_CHANNEL_ID,
                "telegram_topic_id": topic_id,
                "telegram_message_id": update.message.message_id,
                "direction": "telegram_to_discord",
                "timestamp": datetime.utcnow()
            }
            messages_collection.insert_one(message_doc)
            
            logger.info(f"Forwarded Telegram message from topic {topic_id} to Discord user {discord_user_id}")
            
        except Exception as e:
            logger.error(f"Failed to forward Telegram message from topic {topic_id}: {e}")

# Initialize bridge
bridge = MessageBridge()

# Discord events
@discord_client.event
async def on_ready():
    print(f"Discord selfbot logged in as {discord_client.user} (ID: {discord_client.user.id})")
    print("------")

@discord_client.event
async def on_message(message: discord.Message):
    # Ignore messages from the bot itself
    if message.author == discord_client.user:
        return
    
    # Ignore messages sent in Discord servers (only process DMs)
    if message.guild is not None:
        return
    
    # Only process DMs (direct messages)
    if isinstance(message.channel, discord.DMChannel):
        await bridge.forward_discord_to_telegram(message)

# Telegram handlers
async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming Telegram messages"""
    await bridge.forward_telegram_to_discord(update, context)

def run_telegram_bot():
    """Run Telegram bot in a separate thread"""
    # Add message handler for topic messages
    message_handler = MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_telegram_message
    )
    telegram_app.add_handler(message_handler)
    
    # Start Telegram bot
    print("Starting Telegram bot...")
    telegram_app.run_polling(drop_pending_updates=True)

def main():
    """Start both bots"""
    try:
        # Start Telegram bot in a separate thread
        telegram_thread = threading.Thread(target=run_telegram_bot, daemon=True)
        telegram_thread.start()
        
        print("Starting Discord selfbot...")
        # Start Discord selfbot
        discord_client.run(DISCORD_TOKEN, bot=False)  # bot=False for selfbot
        
    except Exception as e:
        logger.error(f"Failed to start bots: {e}")
        raise

if __name__ == "__main__":
    main()