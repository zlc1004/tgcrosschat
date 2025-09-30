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
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.DEBUG
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("pymongo").setLevel(logging.WARNING)

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

# Store Discord event loop for cross-thread calls
discord_loop = None

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
        logger.debug(f"Looking up mapping for topic {topic_id}: {mapping}")
        return mapping
        
    async def forward_discord_to_telegram(self, message: discord.Message):
        """Forward Discord DM to Telegram topic"""
        username = message.author.name
        user_display_name = message.author.display_name
        user_id = message.author.id
        
        try:
            # Get or create topic for this user
            topic_id = await self.get_or_create_topic(username, user_id)
            
            # Check if this is a reply to another message
            reply_to_message_id = None
            if message.reference and message.reference.message_id:
                # Find the corresponding Telegram message
                reply_mapping = messages_collection.find_one({
                    "discord_message_id": message.reference.message_id,
                    "direction": "discord_to_telegram"
                })
                if reply_mapping:
                    reply_to_message_id = reply_mapping["telegram_message_id"]
            
            # Prepare the message content
            content = f"**{user_display_name}** (@{username}):\n{message.content}"
            
            # Send message to Telegram topic
            telegram_msg = await self.telegram_bot.send_message(
                chat_id=TOPICS_CHANNEL_ID,
                message_thread_id=topic_id,
                text=content,
                parse_mode=ParseMode.MARKDOWN,
                reply_to_message_id=reply_to_message_id
            )
            
            # Store message mapping
            message_doc = {
                "message_content": message.content,
                "discord_channel_id": message.author.id,  # For DMs, channel ID = user ID
                "discord_message_id": message.id,
                "telegram_channel_id": TOPICS_CHANNEL_ID,
                "telegram_topic_id": topic_id,
                "telegram_message_id": telegram_msg.message_id,
                "direction": "discord_to_telegram",
                "timestamp": datetime.utcnow(),
                "is_reply": reply_to_message_id is not None,
                "reply_to_telegram_id": reply_to_message_id
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
            
    async def edit_discord_message_in_telegram(self, before: discord.Message, after: discord.Message):
        """Edit corresponding Telegram message when Discord message is edited"""
        try:
            # Find the corresponding Telegram message
            message_mapping = messages_collection.find_one({
                "discord_message_id": after.id,
                "direction": "discord_to_telegram"
            })
            
            if not message_mapping:
                logger.warning(f"No Telegram message found for edited Discord message {after.id}")
                return
                
            # Prepare the updated content
            username = after.author.name
            user_display_name = after.author.display_name
            content = f"**{user_display_name}** (@{username}) *[edited]*:\n{after.content}"
            
            # Edit the Telegram message
            await self.telegram_bot.edit_message_text(
                chat_id=TOPICS_CHANNEL_ID,
                message_id=message_mapping["telegram_message_id"],
                text=content,
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Update the database record
            messages_collection.update_one(
                {"_id": message_mapping["_id"]},
                {
                    "$set": {
                        "message_content": after.content,
                        "last_edited": datetime.utcnow()
                    }
                }
            )
            
            logger.info(f"Edited Telegram message {message_mapping['telegram_message_id']} for Discord edit")
            
        except Exception as e:
            logger.error(f"Failed to edit Telegram message for Discord edit: {e}")
            
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
            logger.debug(f"Retrieved mapping for topic {topic_id}: {mapping}")
            if not mapping:
                logger.warning(f"No Discord user mapping found for topic {topic_id}")
                return
                
            discord_user_id = mapping["discord_user_id"]
            
            # Use run_coroutine_threadsafe to call Discord functions
            if discord_loop is None:
                logger.error("Discord event loop not available")
                return
                
            future = asyncio.run_coroutine_threadsafe(
                self._send_discord_message(discord_user_id, update, topic_id),
                discord_loop
            )
            
            # Wait for the result with timeout
            try:
                future.result(timeout=10)  # 10 second timeout
            except Exception as e:
                logger.error(f"Failed to send Discord message: {e}")
            
        except Exception as e:
            logger.error(f"Failed to forward Telegram message from topic {topic_id}: {e}")
            
    async def _send_discord_message(self, discord_user_id: int, update: Update, topic_id: int):
        """Helper method to send Discord message in Discord's event loop"""
        discord_user = None
        try:
            # Get Discord user
            discord_user = await discord_client.fetch_user(discord_user_id)
            if not discord_user:
                logger.error(f"Could not find Discord user {discord_user_id}")
                return
            
            logger.info(f"Found Discord user: {discord_user.name}#{discord_user.discriminator} (ID: {discord_user.id})")
            
            # Create DM channel
            try:
                dm_channel = discord_user.dm_channel
                if dm_channel is None:
                    dm_channel = await discord_user.create_dm()
                logger.info(f"DM channel created/found: {dm_channel.id}")
            except Exception as e:
                logger.error(f"Failed to create DM channel with {discord_user.name}: {e}")
                return
            
            # Check if this is a reply to another message
            reference = None
            if update.message.reply_to_message:
                # Find the corresponding Discord message
                reply_mapping = messages_collection.find_one({
                    "telegram_message_id": update.message.reply_to_message.message_id,
                    "direction": "telegram_to_discord"
                })
                if reply_mapping and reply_mapping.get("discord_message_id"):
                    # Create a message reference for Discord
                    try:
                        original_message = await dm_channel.fetch_message(reply_mapping["discord_message_id"])
                        reference = original_message
                    except:
                        pass  # If we can't fetch the original message, just send without reference
                
            # Send message to Discord DM
            content = update.message.text or "[Media/File]"
            
            logger.info(f"Attempting to send message to {discord_user.name}: '{content}'")
            
            if reference:
                discord_msg = await reference.reply(content)
            else:
                discord_msg = await dm_channel.send(content)
            
            # Store message mapping
            message_doc = {
                "message_content": content,
                "discord_channel_id": discord_user_id,  # For DMs, channel ID = user ID
                "discord_message_id": discord_msg.id,
                "telegram_channel_id": TOPICS_CHANNEL_ID,
                "telegram_topic_id": topic_id,
                "telegram_message_id": update.message.message_id,
                "direction": "telegram_to_discord",
                "timestamp": datetime.utcnow(),
                "is_reply": reference is not None,
                "reply_to_discord_id": reference.id if reference else None
            }
            messages_collection.insert_one(message_doc)
            
            logger.info(f"Forwarded Telegram message from topic {topic_id} to Discord user {discord_user_id}")
            
        except Exception as e:
            import discord
            
            # Handle specific Discord errors more gracefully
            if isinstance(e, discord.Forbidden):
                logger.warning(f"Cannot access Discord user {discord_user_id} - User may have DMs disabled, blocked the selfbot, or privacy settings prevent access")
                logger.info(f"Skipping message from Telegram topic {topic_id} due to Discord access restrictions")
                return
            elif isinstance(e, discord.NotFound):
                logger.warning(f"Discord user {discord_user_id} not found - User may no longer exist")
                logger.info(f"You may want to delete the mapping for topic {topic_id} as the Discord user no longer exists")
                return
            
            # For other errors, log detailed debug info
            logger.error(f"Failed to send Discord message: {e}")
            logger.error(f"Exception type: {type(e).__name__}")
            
            # Log comprehensive debug info
            logger.debug(f"Discord user ID: {discord_user_id}")
            discord_user = discord_client.get_user(discord_user_id)
            if discord_user:
                logger.debug(f"Discord user: {discord_user.name}#{discord_user.discriminator} (ID: {discord_user.id})")
            else:
                logger.debug(f"Discord user: NOT FOUND")
            logger.debug(f"Telegram topic ID: {topic_id}")
            logger.debug(f"Telegram message ID: {update.message.message_id}")
            logger.debug(f"Message content: '{update.message.text or '[Media/File]'}'")
            logger.debug(f"From Telegram user: {update.message.from_user.username} (ID: {update.message.from_user.id})")
            
            # Log more details about the Discord error for non-403/404 errors
            if hasattr(e, 'status'):
                logger.error(f"HTTP Status: {e.status}")
            if hasattr(e, 'code'):
                logger.error(f"Error Code: {e.code}")
            if hasattr(e, 'text'):
                logger.error(f"Error Text: {e.text}")
            if hasattr(e, 'response'):
                logger.error(f"Response: {e.response}")
                
            if isinstance(e, discord.HTTPException):
                logger.error(f"Discord HTTP Exception - Status: {e.status}, Code: {e.code}")
            elif isinstance(e, discord.DiscordServerError):
                logger.error(f"Discord Server Error - Internal Discord issue")
                
            # Print traceback for unexpected errors only
            if not isinstance(e, (discord.Forbidden, discord.NotFound)):
                import traceback
                logger.error(f"Full traceback:\n{traceback.format_exc()}")
            
    async def edit_telegram_message_in_discord(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Edit corresponding Discord message when Telegram message is edited"""
        if not update.edited_message or not update.edited_message.message_thread_id:
            return
            
        # Only process messages in the topics channel
        if update.edited_message.chat_id != TOPICS_CHANNEL_ID:
            return
            
        try:
            # Find the corresponding Discord message
            message_mapping = messages_collection.find_one({
                "telegram_message_id": update.edited_message.message_id,
                "direction": "telegram_to_discord"
            })
            
            if not message_mapping:
                logger.warning(f"No Discord message found for edited Telegram message {update.edited_message.message_id}")
                return
                
            # Use run_coroutine_threadsafe to call Discord functions
            if discord_loop is None:
                logger.error("Discord event loop not available")
                return
                
            future = asyncio.run_coroutine_threadsafe(
                self._edit_discord_message(update, message_mapping),
                discord_loop
            )
            
            # Wait for the result with timeout
            try:
                future.result(timeout=10)  # 10 second timeout
            except Exception as e:
                logger.error(f"Failed to edit Discord message: {e}")
                
        except Exception as e:
            logger.error(f"Failed to edit Discord message for Telegram edit: {e}")
            
    async def _edit_discord_message(self, update: Update, message_mapping: dict):
        """Helper method to edit Discord message in Discord's event loop"""
        try:
            # Get the Discord message and edit it
            topic_id = update.edited_message.message_thread_id
            mapping = await self.get_discord_user_from_topic(topic_id)
            if not mapping:
                return
                
            discord_user_id = mapping["discord_user_id"]
            discord_user = await discord_client.fetch_user(discord_user_id)
            
            if discord_user:
                try:
                    # Create/get DM channel
                    dm_channel = discord_user.dm_channel
                    if dm_channel is None:
                        dm_channel = await discord_user.create_dm()
                    discord_msg = await dm_channel.fetch_message(message_mapping["discord_message_id"])
                    new_content = f"{update.edited_message.text or '[Media/File]'} *[edited]*"
                    await discord_msg.edit(content=new_content)
                    
                    # Update the database record
                    messages_collection.update_one(
                        {"_id": message_mapping["_id"]},
                        {
                            "$set": {
                                "message_content": update.edited_message.text or '[Media/File]',
                                "last_edited": datetime.utcnow()
                            }
                        }
                    )
                    
                    logger.info(f"Edited Discord message {message_mapping['discord_message_id']} for Telegram edit")
                    
                except Exception as e:
                    logger.error(f"Failed to edit Discord message: {e}")
                    
        except Exception as e:
            logger.error(f"Failed to edit Discord message: {e}")

# Initialize bridge
bridge = MessageBridge()

# Discord events
@discord_client.event
async def on_ready():
    global discord_loop
    discord_loop = asyncio.get_event_loop()
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

@discord_client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    # Ignore messages from the bot itself
    if after.author == discord_client.user:
        return
    
    # Ignore messages sent in Discord servers (only process DMs)
    if after.guild is not None:
        return
    
    # Only process DM edits
    if isinstance(after.channel, discord.DMChannel):
        await bridge.edit_discord_message_in_telegram(before, after)

# Telegram handlers
async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming Telegram messages"""
    await bridge.forward_telegram_to_discord(update, context)

async def handle_telegram_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle edited Telegram messages"""
    await bridge.edit_telegram_message_in_discord(update, context)

def run_discord_bot():
    """Run Discord bot in a separate thread"""
    print("Starting Discord selfbot...")
    try:
        discord_client.run(DISCORD_TOKEN)
    except Exception as e:
        logger.error(f"Failed to start Discord bot: {e}")

def run_telegram_bot():
    """Run Telegram bot in main thread"""
    # Add message handler for topic messages
    message_handler = MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_telegram_message
    )
    telegram_app.add_handler(message_handler)
    
    # Add edit handler for edited messages
    edit_handler = MessageHandler(
        filters.UpdateType.EDITED_MESSAGE,
        handle_telegram_edit
    )
    telegram_app.add_handler(edit_handler)
    
    # Start Telegram bot
    print("Starting Telegram bot...")
    telegram_app.run_polling(drop_pending_updates=True)

def main():
    """Start both bots"""
    try:
        # Start Discord bot in a separate thread
        discord_thread = threading.Thread(target=run_discord_bot, daemon=True)
        discord_thread.start()
        
        # Run Telegram bot in main thread
        run_telegram_bot()
        
    except Exception as e:
        logger.error(f"Failed to start bots: {e}")
        raise

if __name__ == "__main__":
    main()