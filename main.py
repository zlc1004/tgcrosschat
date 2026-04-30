import io
import logging
import os
import discord
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.constants import ParseMode
import asyncio
from pymongo import MongoClient
from datetime import datetime
import threading
import json

def create_header(bot, auth_token):
    headers = {
            'Accept-Language': 'en-US',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Origin': 'https://discord.com',
            'Pragma': 'no-cache',
            'Referer': 'https://discord.com/channels/@me',
            'Sec-CH-UA': '"Google Chrome";v="{0}", "Chromium";v="{0}", ";Not A Brand";v="99"'.format(
                str(bot.http.browser_version).split('.')[0]
            ),
            'Sec-CH-UA-Mobile': '?0',
            'Sec-CH-UA-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': bot.http.user_agent,
            'X-Discord-Locale': 'en-US',
            'X-Debug-Options': 'bugReporterEnabled',
            'X-Super-Properties': bot.http.headers.encoded_super_properties,
            'Authorization': auth_token
    }
    return headers

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

MAX_ATTACHMENT_SIZE = 9 * 1024 * 1024  # 9 MB

def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram Markdown v1, avoiding URLs."""
    if not text:
        return text
    # Don't escape URLs - look for http:// or https://
    # Split on URLs and only escape the non-URL parts
    import re
    url_pattern = r'https?://\S+'
    
    def escape_non_url(match_obj):
        # If it's a URL (matched pattern), return as-is; otherwise escape
        return match_obj.group(0)
    
    parts = re.split(f'({url_pattern})', text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:  # Non-URL parts
            # Escape backslash first, then other Markdown special characters
            # But NOT parentheses, since they appear in normal text and escaping breaks readability
            escaped = part.replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]").replace("`", "\\`").replace("~", "\\~")
            result.append(escaped)
        else:  # URL parts
            result.append(part)
    return ''.join(result)

# MongoDB connection (using Docker DNS)
mongo_client = MongoClient('mongodb://mongo:27017/')
db = mongo_client.tgcrosschat

# Collections
mappings_collection = db.mappings  # topic_id <-> discord_user_id mappings
channel_mappings_collection = db.channel_mappings  # topic_id <-> discord_channel_id mappings
messages_collection = db.messages  # message sync tracking

def initialize_database():
    """Initialize database and collections"""
    try:
        # Test connection
        mongo_client.admin.command('ping')
        logger.info("Successfully connected to MongoDB")

        # Ensure database exists by creating collections if they don't exist
        if 'mappings' not in db.list_collection_names():
            db.create_collection('mappings')
            logger.info("Created 'mappings' collection")

        if 'channel_mappings' not in db.list_collection_names():
            db.create_collection('channel_mappings')
            logger.info("Created 'channel_mappings' collection")

        if 'messages' not in db.list_collection_names():
            db.create_collection('messages')
            logger.info("Created 'messages' collection")

        # Create indexes for better performance
        mappings_collection.create_index("discord_user_id", unique=True)
        mappings_collection.create_index("telegram_topic_id", unique=True)
        channel_mappings_collection.create_index("discord_channel_id", unique=True)
        channel_mappings_collection.create_index("telegram_topic_id", unique=True)
        messages_collection.create_index("discord_message_id")
        messages_collection.create_index("telegram_message_id")

        # Migration: backfill custom_status field on existing mappings documents
        result = mappings_collection.update_many(
            {"custom_status": {"$exists": False}},
            {"$set": {"custom_status": None}}
        )
        if result.modified_count > 0:
            logger.info(f"Migration: added custom_status field to {result.modified_count} existing mapping(s)")

        # Migration: backfill status_message_id field on existing mappings documents
        result = mappings_collection.update_many(
            {"status_message_id": {"$exists": False}},
            {"$set": {"status_message_id": None}}
        )
        if result.modified_count > 0:
            logger.info(f"Migration: added status_message_id field to {result.modified_count} existing mapping(s)")

        logger.info("Database initialization completed successfully")
        logger.info(f"Available collections: {db.list_collection_names()}")

    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

# Initialize bots
discord_client = discord.Client()

async def post_init(application):
    """Register bot commands in the Telegram command palette after startup"""
    from telegram import BotCommand
    await application.bot.set_my_commands([
        BotCommand("ping",         "Check if the bot is alive"),
        BotCommand("data",         "Show current channel and topic IDs"),
        BotCommand("connect",      "Link a Discord channel to this topic"),
        BotCommand("unlink",       "Unlink the Discord channel from this topic"),
        BotCommand("resetstatus",  "Clear the stored Discord status for this topic"),
        BotCommand("header",       "Show raw Discord request headers (debug)"),
    ])
    logger.info("Bot commands registered")

telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

# Store Discord event loop for cross-thread calls
discord_loop = None

class MessageBridge:
    def __init__(self):
        self.telegram_bot = telegram_app.bot
        # Maps topic_id -> (user_id, last_message_datetime) for header suppression
        self._last_sender: dict = {}
        # Maps discord_user_id -> (status_text, timestamp) for status deduplication
        self._last_status_sent: dict = {}

    def _should_show_header(self, topic_id: int, user_id: int) -> bool:
        """Return False (suppress header) if the same user sent the last message in this topic within 30 seconds."""
        entry = self._last_sender.get(topic_id)
        if entry is None:
            return True
        last_user_id, last_time = entry
        if last_user_id != user_id:
            return True
        elapsed = (datetime.utcnow() - last_time).total_seconds()
        return elapsed > 30

    def _update_last_sender(self, topic_id: int, user_id: int):
        """Record the most recent sender for a topic."""
        self._last_sender[topic_id] = (user_id, datetime.utcnow())

    def _should_send_status_update(self, user_id: int, status_text: str | None) -> bool:
        """Return True only if this status update should be sent (deduplicates rapid Discord presence events).
        
        Returns False if the same status was sent to this user within the last 3 seconds.
        """
        entry = self._last_status_sent.get(user_id)
        if entry is None:
            return True
        last_status, last_time = entry
        if last_status != status_text:
            return True
        elapsed = (datetime.utcnow() - last_time).total_seconds()
        return elapsed > 3

    def _record_status_sent(self, user_id: int, status_text: str | None):
        """Record that a status update was sent for this user."""
        self._last_status_sent[user_id] = (status_text, datetime.utcnow())

    async def get_or_create_topic(self, username: str, user_id: int, display_name: str = None) -> int:
        """Get existing topic ID for user or create a new one"""
        # Check if mapping exists
        mapping = mappings_collection.find_one({"discord_user_id": user_id})
        if mapping:
            return mapping["telegram_topic_id"]

        try:
            # Create a new topic for this user with display name and username
            topic_name = f"DM with {display_name or username}({username})"
            topic = await self.telegram_bot.create_forum_topic(
                chat_id=TOPICS_CHANNEL_ID,
                name=topic_name
            )

            # Store mapping in database
            mapping_doc = {
                "discord_user_id": user_id,
                "discord_username": username,
                "telegram_topic_id": topic.message_thread_id,
                "custom_status": None,
                "status_message_id": None,
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

    async def forward_channel_to_telegram(self, message: discord.Message):
        """Forward Discord channel message to connected Telegram topic"""
        channel_id = message.channel.id

        # Check if this channel is connected to a Telegram topic
        mapping = channel_mappings_collection.find_one({"discord_channel_id": channel_id})
        if not mapping:
            return  # Channel not connected, ignore

        topic_id = mapping["telegram_topic_id"]
        username = message.author.name
        # Use global_name if it exists and is different from username, otherwise use display_name
        try:
            global_name = message.author.global_name
        except AttributeError:
            global_name = message.author.display_name  # Fallback for older discord.py versions
        user_display_name = global_name if (global_name and global_name != username) else message.author.display_name
        channel_name = message.channel.name

        try:
            # Check if this is a reply to another message
            reply_to_message_id = None
            if message.reference and message.reference.message_id:
                # Find the corresponding Telegram message (check both directions)
                reply_mapping = messages_collection.find_one({
                    "discord_message_id": message.reference.message_id,
                    "direction": {"$in": ["discord_to_telegram", "telegram_to_discord"]}
                })
                if reply_mapping:
                    reply_to_message_id = reply_mapping["telegram_message_id"]

            # Prepare the message content
            if self._should_show_header(topic_id, message.author.id):
                content = f"**{escape_markdown(user_display_name)}** (@{escape_markdown(username)}):\n{escape_markdown(message.content)}"
            else:
                content = escape_markdown(message.content)

            # Determine if we should send text message separately or with attachment
            has_image = any(a.content_type and a.content_type.startswith("image/") for a in message.attachments if a.size <= MAX_ATTACHMENT_SIZE)
            text_message_sent = False

            # If there's an image and text content, send as image with caption instead of separate message
            if has_image and content.strip():
                for attachment in message.attachments:
                    if attachment.content_type and attachment.content_type.startswith("image/") and attachment.size <= MAX_ATTACHMENT_SIZE:
                        try:
                            file_response = requests.get(attachment.url)
                            if file_response.status_code != 200:
                                logger.error(f"Failed to download image {attachment.filename}: HTTP {file_response.status_code}")
                                continue
                            file_bio = io.BytesIO(file_response.content)
                            file_bio.name = attachment.filename
                            telegram_msg = await self.telegram_bot.send_photo(
                                chat_id=TOPICS_CHANNEL_ID,
                                message_thread_id=topic_id,
                                photo=file_bio,
                                caption=content,
                                parse_mode=ParseMode.MARKDOWN,
                                reply_to_message_id=reply_to_message_id
                            )
                            text_message_sent = True

                            # Store message mapping
                            message_doc = {
                                "message_content": message.content,
                                "discord_channel_id": channel_id,
                                "discord_message_id": message.id,
                                "telegram_channel_id": TOPICS_CHANNEL_ID,
                                "telegram_topic_id": topic_id,
                                "telegram_message_id": telegram_msg.message_id,
                                "direction": "discord_to_telegram",
                                "timestamp": datetime.utcnow(),
                                "is_reply": reply_to_message_id is not None,
                                "reply_to_telegram_id": reply_to_message_id,
                                "is_channel_message": True,
                                "channel_name": channel_name,
                                "has_attachment": True,
                                "attachment_filename": attachment.filename
                            }
                            messages_collection.insert_one(message_doc)
                            self._update_last_sender(topic_id, message.author.id)
                            break  # Send only first image with caption
                        except Exception as e:
                            logger.error(f"Failed to send channel image with caption: {e}")

            # If we haven't sent the message yet (no image with caption), send as text (if not empty)
            if not text_message_sent and content.strip():
                telegram_msg = await self.telegram_bot.send_message(
                    chat_id=TOPICS_CHANNEL_ID,
                    message_thread_id=topic_id,
                    text=content,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_to_message_id=reply_to_message_id
                )

                self._update_last_sender(topic_id, message.author.id)

                # Store message mapping
                message_doc = {
                    "message_content": message.content,
                    "discord_channel_id": channel_id,
                    "discord_message_id": message.id,
                    "telegram_channel_id": TOPICS_CHANNEL_ID,
                    "telegram_topic_id": topic_id,
                    "telegram_message_id": telegram_msg.message_id,
                    "direction": "discord_to_telegram",
                    "timestamp": datetime.utcnow(),
                    "is_reply": reply_to_message_id is not None,
                    "reply_to_telegram_id": reply_to_message_id,
                    "is_channel_message": True,
                    "channel_name": channel_name
                }
                messages_collection.insert_one(message_doc)

            # Handle remaining attachments (skip first image if it was already sent with caption)
            first_image_skipped = text_message_sent
            for attachment in message.attachments:
                try:
                    # Skip first image if it was already sent with caption
                    if first_image_skipped and attachment.content_type and attachment.content_type.startswith("image/") and attachment.size <= MAX_ATTACHMENT_SIZE:
                        first_image_skipped = False
                        continue

                    if attachment.size > MAX_ATTACHMENT_SIZE:
                        # File too large to re-upload; send as a link
                        telegram_attachment = await self.telegram_bot.send_message(
                            chat_id=TOPICS_CHANNEL_ID,
                            message_thread_id=topic_id,
                            text=f"📎 {escape_markdown(attachment.filename)} (from {escape_markdown(user_display_name)}): {attachment.url}"
                        )
                    else:
                        # Download to BytesIO and re-upload
                        file_response = requests.get(attachment.url)
                        if file_response.status_code != 200:
                            logger.error(f"Failed to download attachment {attachment.filename}: HTTP {file_response.status_code}")
                            continue  # Skip this attachment instead of raising
                        file_bio = io.BytesIO(file_response.content)
                        file_bio.name = attachment.filename
                        if attachment.content_type and attachment.content_type.startswith("image/"):
                            telegram_attachment = await self.telegram_bot.send_photo(
                                chat_id=TOPICS_CHANNEL_ID,
                                message_thread_id=topic_id,
                                photo=file_bio,
                                caption=f"Image from {escape_markdown(user_display_name)}"
                            )
                        else:
                            telegram_attachment = await self.telegram_bot.send_document(
                                chat_id=TOPICS_CHANNEL_ID,
                                message_thread_id=topic_id,
                                document=file_bio,
                                caption=f"File from {escape_markdown(user_display_name)}: {escape_markdown(attachment.filename)}"
                            )

                    # Store attachment mapping
                    attachment_doc = {
                        "message_content": f"[Attachment: {attachment.filename}]",
                        "discord_channel_id": channel_id,
                        "discord_message_id": message.id,
                        "telegram_channel_id": TOPICS_CHANNEL_ID,
                        "telegram_topic_id": topic_id,
                        "telegram_message_id": telegram_attachment.message_id,
                        "direction": "discord_to_telegram",
                        "timestamp": datetime.utcnow(),
                        "is_reply": False,
                        "has_attachment": True,
                        "attachment_filename": attachment.filename,
                        "attachment_url": attachment.url,
                        "is_channel_message": True,
                        "channel_name": channel_name
                    }
                    messages_collection.insert_one(attachment_doc)

                except Exception as e:
                    logger.error(f"Failed to send attachment {attachment.filename}: {e}")

            logger.info(f"Forwarded channel message from {channel_name} to topic {topic_id}")

        except Exception as e:
            logger.error(f"Failed to forward channel message from {channel_name}: {e}")

    async def forward_discord_to_telegram(self, message: discord.Message):
        """Forward Discord DM to Telegram topic"""
        username = message.author.name
        # Use global_name if it exists and is different from username, otherwise use display_name
        try:
            global_name = message.author.global_name
        except AttributeError:
            global_name = message.author.display_name  # Fallback for older discord.py versions
        user_display_name = global_name if (global_name and global_name != username) else message.author.display_name
        user_id = message.author.id

        try:
            # Get or create topic for this user
            topic_id = await self.get_or_create_topic(username, user_id, user_display_name)

            # Check if this is a reply to another message
            reply_to_message_id = None
            if message.reference and message.reference.message_id:
                # Find the corresponding Telegram message (check both directions)
                reply_mapping = messages_collection.find_one({
                    "discord_message_id": message.reference.message_id,
                    "direction": {"$in": ["discord_to_telegram", "telegram_to_discord"]}
                })
                if reply_mapping:
                    reply_to_message_id = reply_mapping["telegram_message_id"]

            # Prepare the message content
            if self._should_show_header(topic_id, user_id):
                content = f"**{escape_markdown(user_display_name)}** (@{escape_markdown(username)}):\n{escape_markdown(message.content)}"
            else:
                content = escape_markdown(message.content)

            # Determine if we should send text message separately or with attachment
            has_image = any(a.content_type and a.content_type.startswith("image/") for a in message.attachments if a.size <= MAX_ATTACHMENT_SIZE)
            text_message_sent = False

            # If there's an image and text content, send as image with caption instead of separate message
            if has_image and content.strip():
                for attachment in message.attachments:
                    if attachment.content_type and attachment.content_type.startswith("image/") and attachment.size <= MAX_ATTACHMENT_SIZE:
                        try:
                            file_response = requests.get(attachment.url)
                            if file_response.status_code != 200:
                                logger.error(f"Failed to download image {attachment.filename}: HTTP {file_response.status_code}")
                                continue
                            file_bio = io.BytesIO(file_response.content)
                            file_bio.name = attachment.filename
                            telegram_msg = await self.telegram_bot.send_photo(
                                chat_id=TOPICS_CHANNEL_ID,
                                message_thread_id=topic_id,
                                photo=file_bio,
                                caption=content,
                                parse_mode=ParseMode.MARKDOWN,
                                reply_to_message_id=reply_to_message_id
                            )
                            text_message_sent = True

                            # Store message mapping
                            message_doc = {
                                "message_content": message.content,
                                "discord_channel_id": message.author.id,
                                "discord_message_id": message.id,
                                "telegram_channel_id": TOPICS_CHANNEL_ID,
                                "telegram_topic_id": topic_id,
                                "telegram_message_id": telegram_msg.message_id,
                                "direction": "discord_to_telegram",
                                "timestamp": datetime.utcnow(),
                                "is_reply": reply_to_message_id is not None,
                                "reply_to_telegram_id": reply_to_message_id,
                                "has_attachment": True,
                                "attachment_filename": attachment.filename
                            }
                            messages_collection.insert_one(message_doc)
                            self._update_last_sender(topic_id, user_id)
                            break  # Send only first image with caption
                        except Exception as e:
                            logger.error(f"Failed to send image with caption: {e}")

            # If we haven't sent the message yet (no image with caption), send as text (if not empty)
            if not text_message_sent and content.strip():
                telegram_msg = await self.telegram_bot.send_message(
                    chat_id=TOPICS_CHANNEL_ID,
                    message_thread_id=topic_id,
                    text=content,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_to_message_id=reply_to_message_id
                )

                self._update_last_sender(topic_id, user_id)

                # Store message mapping
                message_doc = {
                    "message_content": message.content,
                    "discord_channel_id": message.author.id,
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

            # Handle remaining attachments (skip first image if it was already sent with caption)
            first_image_skipped = text_message_sent  # Skip first image if already sent as caption
            for attachment in message.attachments:
                try:
                    # Skip first image if it was already sent with caption
                    if first_image_skipped and attachment.content_type and attachment.content_type.startswith("image/") and attachment.size <= MAX_ATTACHMENT_SIZE:
                        first_image_skipped = False
                        continue

                    if attachment.size > MAX_ATTACHMENT_SIZE:
                        # File too large to re-upload; send as a link
                        telegram_attachment = await self.telegram_bot.send_message(
                            chat_id=TOPICS_CHANNEL_ID,
                            message_thread_id=topic_id,
                            text=f"📎 {escape_markdown(attachment.filename)} (from {escape_markdown(user_display_name)}): {attachment.url}"
                        )
                    else:
                        # Download to BytesIO and re-upload
                        file_response = requests.get(attachment.url)
                        if file_response.status_code != 200:
                            logger.error(f"Failed to download attachment {attachment.filename}: HTTP {file_response.status_code}")
                            continue  # Skip this attachment instead of raising
                        file_bio = io.BytesIO(file_response.content)
                        file_bio.name = attachment.filename
                        if attachment.content_type and attachment.content_type.startswith("image/"):
                            telegram_attachment = await self.telegram_bot.send_photo(
                                chat_id=TOPICS_CHANNEL_ID,
                                message_thread_id=topic_id,
                                photo=file_bio,
                                caption=f"Image from {escape_markdown(user_display_name)}"
                            )
                        else:
                            telegram_attachment = await self.telegram_bot.send_document(
                                chat_id=TOPICS_CHANNEL_ID,
                                message_thread_id=topic_id,
                                document=file_bio,
                                caption=f"File from {escape_markdown(user_display_name)}: {escape_markdown(attachment.filename)}"
                            )

                    # Store attachment mapping
                    attachment_doc = {
                        "message_content": f"[Attachment: {attachment.filename}]",
                        "discord_channel_id": message.author.id,
                        "discord_message_id": message.id,
                        "telegram_channel_id": TOPICS_CHANNEL_ID,
                        "telegram_topic_id": topic_id,
                        "telegram_message_id": telegram_attachment.message_id,
                        "direction": "discord_to_telegram",
                        "timestamp": datetime.utcnow(),
                        "is_reply": False,
                        "has_attachment": True,
                        "attachment_filename": attachment.filename,
                        "attachment_url": attachment.url
                    }
                    messages_collection.insert_one(attachment_doc)

                except Exception as e:
                    logger.error(f"Failed to send attachment {attachment.filename}: {e}")

            logger.info(f"Forwarded DM from {username} to topic {topic_id}")

        except Exception as e:
            logger.error(f"Failed to forward Discord message from {username}: {e}")

    async def edit_channel_message_in_telegram(self, before: discord.Message, after: discord.Message):
        """Edit corresponding Telegram message when Discord channel message is edited"""
        channel_id = after.channel.id

        # Check if this channel is connected
        mapping = channel_mappings_collection.find_one({"discord_channel_id": channel_id})
        if not mapping:
            return  # Channel not connected, ignore

        try:
            # Find the corresponding Telegram message
            message_mapping = messages_collection.find_one({
                "discord_message_id": after.id,
                "direction": "discord_to_telegram"
            })

            if not message_mapping:
                logger.warning(f"No Telegram message found for edited Discord channel message {after.id}")
                return

            # Prepare the updated content
            username = after.author.name
            # Use global_name if it exists and is different from username, otherwise use display_name
            try:
                global_name = after.author.global_name
            except AttributeError:
                global_name = after.author.display_name  # Fallback for older discord.py versions
            user_display_name = global_name if (global_name and global_name != username) else after.author.display_name
            channel_name = after.channel.name
            content = f"**{escape_markdown(user_display_name)}** (@{escape_markdown(username)}) *[edited]*:\n{escape_markdown(after.content)}"

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

            logger.info(f"Edited Telegram message {message_mapping['telegram_message_id']} for Discord channel edit")

        except Exception as e:
            logger.error(f"Failed to edit Telegram message for Discord channel edit: {e}")

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
            # Use global_name if it exists and is different from username, otherwise use display_name
            try:
                global_name = after.author.global_name
            except AttributeError:
                global_name = after.author.display_name  # Fallback for older discord.py versions
            user_display_name = global_name if (global_name and global_name != username) else after.author.display_name
            content = f"**{escape_markdown(user_display_name)}** (@{escape_markdown(username)}) *[edited]*:\n{escape_markdown(after.content)}"

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
        """Forward Telegram topic message to Discord DM or channel"""
        if not update.message or not update.message.message_thread_id:
            return

        # Only process messages in the topics channel
        if update.message.chat_id != TOPICS_CHANNEL_ID:
            return

        # Don't forward bot's own messages; skip anonymous/channel messages with no sender
        if not update.message.from_user or update.message.from_user.is_bot:
            return

        topic_id = update.message.message_thread_id

        try:
            # Check for user mapping first (DM topics)
            user_mapping = await self.get_discord_user_from_topic(topic_id)
            if user_mapping:
                discord_user_id = user_mapping["discord_user_id"]
                await self._send_discord_message(discord_user_id, update, topic_id)
                return

            # Check for channel mapping (connected channels)
            channel_mapping = channel_mappings_collection.find_one({"telegram_topic_id": topic_id})
            if channel_mapping:
                discord_channel_id = channel_mapping["discord_channel_id"]
                await self._send_discord_channel_message(discord_channel_id, update, topic_id)
                return

            logger.warning(f"No mapping found for topic {topic_id}")

        except Exception as e:
            logger.error(f"Failed to forward Telegram message from topic {topic_id}: {e}")

    async def _send_discord_message(self, discord_user_id: int, update: Update, topic_id: int):
        """Helper method to send Discord message using direct HTTP requests"""
        try:
            # First, create or get DM channel using HTTP API
            dm_channel_id = await self._get_or_create_dm_channel(discord_user_id)
            if not dm_channel_id:
                logger.error(f"Could not create DM channel with Discord user {discord_user_id}")
                return

            # Check if this is a reply to another message
            message_reference = None
            if update.message.reply_to_message:
                # First, look for Discord messages that were forwarded TO Telegram
                reply_mapping = messages_collection.find_one({
                    "telegram_message_id": update.message.reply_to_message.message_id,
                    "direction": "discord_to_telegram"
                })

                # If not found, look for Telegram messages that were forwarded TO Discord
                if not reply_mapping:
                    reply_mapping = messages_collection.find_one({
                        "telegram_message_id": update.message.reply_to_message.message_id,
                        "direction": "telegram_to_discord"
                    })

                if reply_mapping and reply_mapping.get("discord_message_id"):
                    message_reference = {
                        "message_id": str(reply_mapping["discord_message_id"])
                    }

            # Handle different message types
            content = update.message.text or ""

            # Handle images and documents
            file_data = None
            filename = None

            if update.message.photo:
                photo = update.message.photo[-1]
                file_size = photo.file_size
                tg_file = await photo.get_file()
                if file_size is None:
                    # Try to download and forward as file (don't just send link)
                    try:
                        file_data = bytes(await tg_file.download_as_bytearray())
                        filename = f"photo_{photo.file_unique_id}.jpg"
                    except Exception as e:
                        logger.error(f"Failed to download Telegram photo: {e}")
                        content = (content or "") + "\n" + tg_file.file_path
                elif file_size > MAX_ATTACHMENT_SIZE:
                    content = (content or "") + "\n" + tg_file.file_path
                else:
                    file_data = bytes(await tg_file.download_as_bytearray())
                    filename = f"photo_{photo.file_unique_id}.jpg"

            elif update.message.document:
                doc = update.message.document
                file_size = doc.file_size
                if file_size is None or file_size > MAX_ATTACHMENT_SIZE:
                    tg_file = await doc.get_file()
                    content = (content or "") + "\n" + tg_file.file_path
                else:
                    tg_file = await doc.get_file()
                    file_data = bytes(await tg_file.download_as_bytearray())
                    filename = doc.file_name or f"document_{doc.file_unique_id}"

            elif update.message.video:
                video = update.message.video
                file_size = video.file_size
                if file_size is None or file_size > MAX_ATTACHMENT_SIZE:
                    tg_file = await video.get_file()
                    content = (content or "") + "\n" + tg_file.file_path
                else:
                    tg_file = await video.get_file()
                    file_data = bytes(await tg_file.download_as_bytearray())
                    filename = video.file_name or f"video_{video.file_unique_id}.mp4"

            if not content and not file_data:
                content = "[Empty message]"

            logger.info(f"Attempting to send message to Discord user {discord_user_id}: '{content}'")

            if file_data:
                # Upload file to Discord
                discord_msg_id = await self._send_discord_file(
                    discord_user_id, dm_channel_id, content, None, filename,
                    message_reference, file_data
                )
                if discord_msg_id:
                    message_doc = {
                        "message_content": content,
                        "discord_channel_id": discord_user_id,
                        "discord_message_id": discord_msg_id,
                        "telegram_channel_id": TOPICS_CHANNEL_ID,
                        "telegram_topic_id": topic_id,
                        "telegram_message_id": update.message.message_id,
                        "direction": "telegram_to_discord",
                        "timestamp": datetime.utcnow(),
                        "is_reply": message_reference is not None,
                        "reply_to_discord_id": message_reference["message_id"] if message_reference else None
                    }
                    messages_collection.insert_one(message_doc)
                return

            # Prepare the request payload
            payload = {
                "content": content
            }

            if message_reference:
                payload["message_reference"] = message_reference

            # Send the message
            headers = create_header(discord_client, DISCORD_TOKEN)

            # Trigger typing indicator in Discord DM before sending the message
            try:
                typing_response = requests.post(
                    f"https://discord.com/api/v9/channels/{dm_channel_id}/typing",
                    headers=headers
                )
                if typing_response.status_code not in (200, 204):
                    logger.debug(f"Discord typing indicator returned status {typing_response.status_code}")
            except Exception as e:
                logger.debug(f"Failed to trigger Discord typing indicator: {e}")

            response = requests.post(
                f"https://discord.com/api/v9/channels/{dm_channel_id}/messages",
                json=payload,
                headers=headers
            )

            if response.status_code == 200:
                discord_msg_data = response.json()
                discord_msg_id = int(discord_msg_data["id"])

                # Store message mapping
                message_doc = {
                    "message_content": content,
                    "discord_channel_id": discord_user_id,  # For DMs, channel ID = user ID
                    "discord_message_id": discord_msg_id,
                    "telegram_channel_id": TOPICS_CHANNEL_ID,
                    "telegram_topic_id": topic_id,
                    "telegram_message_id": update.message.message_id,
                    "direction": "telegram_to_discord",
                    "timestamp": datetime.utcnow(),
                    "is_reply": message_reference is not None,
                    "reply_to_discord_id": message_reference["message_id"] if message_reference else None
                }
                messages_collection.insert_one(message_doc)

                logger.info(f"Successfully sent message to Discord user {discord_user_id}")
            else:
                logger.error(f"Failed to send Discord message. Status: {response.status_code}, Response: {response.text}")

        except Exception as e:
            logger.error(f"Failed to send Discord message using HTTP API: {e}")
            logger.debug(f"Discord user ID: {discord_user_id}")
            logger.debug(f"Telegram topic ID: {topic_id}")
            logger.debug(f"Message content: '{update.message.text or '[Media/File]'}'")

            import traceback
            logger.error(f"Full traceback:\n{traceback.format_exc()}")

    async def _send_discord_file(self, discord_user_id: int, dm_channel_id: str, content: str, file_url: str, filename: str, message_reference: dict = None, file_data: bytes = None):
        """Send file to Discord using multipart form data"""
        try:
            if file_data is None:
                # Download the file
                file_response = requests.get(file_url)
                if file_response.status_code != 200:
                    logger.error(f"Failed to download file from Telegram: {file_response.status_code}")
                    return
                file_data = file_response.content

            # Prepare multipart form data
            files = {
                'files[0]': (filename, file_data)
            }

            data = {
                'content': content,
                'payload_json': {
                    'content': content
                }
            }

            if message_reference:
                data['payload_json']['message_reference'] = message_reference

            # Convert payload_json to string
            data['payload_json'] = json.dumps(data['payload_json'])

            headers = create_header(discord_client, DISCORD_TOKEN)

            response = requests.post(
                f"https://discord.com/api/v9/channels/{dm_channel_id}/messages",
                files=files,
                data=data,
                headers=headers
            )

            if response.status_code == 200:
                discord_msg_data = response.json()
                discord_msg_id = discord_msg_data["id"]

                # Message tracking will be handled by the caller

                logger.info(f"Successfully sent file {filename} to Discord user {discord_user_id}")
                return discord_msg_id
            else:
                logger.error(f"Failed to send Discord file. Status: {response.status_code}, Response: {response.text}")

        except Exception as e:
            logger.error(f"Failed to send file to Discord: {e}")

    async def _send_discord_channel_file(self, discord_channel_id: int, content: str, file_url: str, filename: str, message_reference: dict = None, file_data: bytes = None):
        """Send file to Discord channel using multipart form data"""
        try:
            if file_data is None:
                # Download the file
                file_response = requests.get(file_url)
                if file_response.status_code != 200:
                    logger.error(f"Failed to download file from Telegram: {file_response.status_code}")
                    return
                file_data = file_response.content

            # Prepare multipart form data
            files = {
                'files[0]': (filename, file_data)
            }

            data = {
                'content': content,
                'payload_json': {
                    'content': content
                }
            }

            if message_reference:
                data['payload_json']['message_reference'] = message_reference

            # Convert payload_json to string
            data['payload_json'] = json.dumps(data['payload_json'])

            headers = create_header(discord_client, DISCORD_TOKEN)

            response = requests.post(
                f"https://discord.com/api/v9/channels/{discord_channel_id}/messages",
                files=files,
                data=data,
                headers=headers
            )

            if response.status_code == 200:
                discord_msg_data = response.json()
                discord_msg_id = discord_msg_data["id"]

                logger.info(f"Successfully sent file {filename} to Discord channel {discord_channel_id}")
                return discord_msg_id
            else:
                logger.error(f"Failed to send Discord channel file. Status: {response.status_code}, Response: {response.text}")

        except Exception as e:
            logger.error(f"Failed to send file to Discord channel: {e}")

    async def _send_discord_channel_message(self, discord_channel_id: int, update: Update, topic_id: int):
        """Send message to Discord channel using HTTP API"""
        try:
            # Check if this is a reply to another message
            message_reference = None
            if update.message.reply_to_message:
                # First, look for Discord messages that were forwarded TO Telegram
                reply_mapping = messages_collection.find_one({
                    "telegram_message_id": update.message.reply_to_message.message_id,
                    "direction": "discord_to_telegram"
                })

                # If not found, look for Telegram messages that were forwarded TO Discord
                if not reply_mapping:
                    reply_mapping = messages_collection.find_one({
                        "telegram_message_id": update.message.reply_to_message.message_id,
                        "direction": "telegram_to_discord"
                    })

                if reply_mapping and reply_mapping.get("discord_message_id"):
                    message_reference = {
                        "message_id": str(reply_mapping["discord_message_id"])
                    }

            # Handle different message types
            content = update.message.text or ""

            # Handle media for channels
            file_data = None
            filename = None
            full_content = content


            if update.message.photo:
                photo = update.message.photo[-1]
                file_size = photo.file_size
                tg_file = await photo.get_file()
                if file_size is None:
                    # Try to download and forward as file (don't just send link)
                    try:
                        file_data = bytes(await tg_file.download_as_bytearray())
                        filename = f"photo_{photo.file_unique_id}.jpg"
                    except Exception as e:
                        logger.error(f"Failed to download Telegram photo: {e}")
                        full_content = (full_content or "") + "\n" + tg_file.file_path
                elif file_size > MAX_ATTACHMENT_SIZE:
                    full_content = (full_content or "") + "\n" + tg_file.file_path
                else:
                    file_data = bytes(await tg_file.download_as_bytearray())
                    filename = f"photo_{photo.file_unique_id}.jpg"

            elif update.message.document:
                doc = update.message.document
                file_size = doc.file_size
                if file_size is None or file_size > MAX_ATTACHMENT_SIZE:
                    tg_file = await doc.get_file()
                    full_content = (full_content or "") + "\n" + tg_file.file_path
                else:
                    tg_file = await doc.get_file()
                    file_data = bytes(await tg_file.download_as_bytearray())
                    filename = doc.file_name or f"document_{doc.file_unique_id}"

            elif update.message.video:
                video = update.message.video
                file_size = video.file_size
                if file_size is None or file_size > MAX_ATTACHMENT_SIZE:
                    tg_file = await video.get_file()
                    full_content = (full_content or "") + "\n" + tg_file.file_path
                else:
                    tg_file = await video.get_file()
                    file_data = bytes(await tg_file.download_as_bytearray())
                    filename = video.file_name or f"video_{video.file_unique_id}.mp4"

            if not full_content.strip() and not file_data:
                full_content = "[Empty message]"

            logger.info(f"Attempting to send message to Discord channel {discord_channel_id}: '{full_content}'")

            if file_data:
                # Upload file to Discord channel
                discord_msg_id = await self._send_discord_channel_file(
                    discord_channel_id, full_content, None, filename,
                    message_reference, file_data
                )
                if discord_msg_id:
                    message_doc = {
                        "message_content": content,
                        "discord_channel_id": discord_channel_id,
                        "discord_message_id": discord_msg_id,
                        "telegram_channel_id": TOPICS_CHANNEL_ID,
                        "telegram_topic_id": topic_id,
                        "telegram_message_id": update.message.message_id,
                        "direction": "telegram_to_discord",
                        "timestamp": datetime.utcnow(),
                        "is_reply": message_reference is not None,
                        "reply_to_discord_id": message_reference["message_id"] if message_reference else None,
                        "is_channel_message": True
                    }
                    messages_collection.insert_one(message_doc)
                return

            # Prepare the request payload
            payload = {
                "content": full_content
            }

            if message_reference:
                payload["message_reference"] = message_reference

            # Send the message
            headers = create_header(discord_client, DISCORD_TOKEN)

            # Trigger typing indicator in Discord channel before sending the message
            try:
                typing_response = requests.post(
                    f"https://discord.com/api/v9/channels/{discord_channel_id}/typing",
                    headers=headers
                )
                if typing_response.status_code not in (200, 204):
                    logger.debug(f"Discord typing indicator returned status {typing_response.status_code}")
            except Exception as e:
                logger.debug(f"Failed to trigger Discord typing indicator: {e}")

            response = requests.post(
                f"https://discord.com/api/v9/channels/{discord_channel_id}/messages",
                json=payload,
                headers=headers
            )

            if response.status_code == 200:
                discord_msg_data = response.json()
                discord_msg_id = int(discord_msg_data["id"])

                # Store message mapping
                message_doc = {
                    "message_content": content,
                    "discord_channel_id": discord_channel_id,
                    "discord_message_id": discord_msg_id,
                    "telegram_channel_id": TOPICS_CHANNEL_ID,
                    "telegram_topic_id": topic_id,
                    "telegram_message_id": update.message.message_id,
                    "direction": "telegram_to_discord",
                    "timestamp": datetime.utcnow(),
                    "is_reply": message_reference is not None,
                    "reply_to_discord_id": message_reference["message_id"] if message_reference else None,
                    "is_channel_message": True
                }
                messages_collection.insert_one(message_doc)

                logger.info(f"Successfully sent message to Discord channel {discord_channel_id}")
            else:
                logger.error(f"Failed to send Discord channel message. Status: {response.status_code}, Response: {response.text}")

        except Exception as e:
            logger.error(f"Failed to send Discord channel message: {e}")

    async def _get_discord_channel_info(self, channel_id: int) -> dict:
        """Get Discord channel and server information using HTTP API"""
        try:
            headers = create_header(discord_client, DISCORD_TOKEN)

            # Get channel information
            response = requests.get(
                f"https://discord.com/api/v9/channels/{channel_id}",
                headers=headers
            )

            if response.status_code == 200:
                channel_data = response.json()
                channel_name = channel_data.get("name", "unknown-channel")
                guild_id = channel_data.get("guild_id")

                if guild_id:
                    # Get guild (server) information
                    guild_response = requests.get(
                        f"https://discord.com/api/v9/guilds/{guild_id}",
                        headers=headers
                    )

                    if guild_response.status_code == 200:
                        guild_data = guild_response.json()
                        server_name = guild_data.get("name", "unknown-server")

                        return {
                            "name": channel_name,
                            "guild_name": server_name,
                            "guild_id": guild_id
                        }

                return {"name": channel_name, "guild_name": "unknown-server"}
            else:
                logger.error(f"Failed to get channel info. Status: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"Failed to get Discord channel info for {channel_id}: {e}")
            return None

    async def _get_or_create_dm_channel(self, discord_user_id: int) -> str:
        """Create or get DM channel with a Discord user using HTTP API"""
        try:
            headers = create_header(discord_client, DISCORD_TOKEN)

            payload = {
                "recipient_id": str(discord_user_id)
            }

            response = requests.post(
                "https://discord.com/api/v9/users/@me/channels",
                json=payload,
                headers=headers
            )

            if response.status_code == 200:
                channel_data = response.json()
                channel_id = channel_data["id"]
                logger.info(f"Created/retrieved DM channel {channel_id} with user {discord_user_id}")
                return channel_id
            else:
                logger.error(f"Failed to create DM channel. Status: {response.status_code}, Response: {response.text}")
                return None

        except Exception as e:
            logger.error(f"Failed to create DM channel with user {discord_user_id}: {e}")
            return None

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

            # Check if this is a channel message or DM
            if message_mapping.get("is_channel_message"):
                # Edit channel message using HTTP API
                await self._edit_discord_channel_message(update, message_mapping)
            else:
                # Edit DM using Discord client (requires event loop)
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

    async def _edit_discord_channel_message(self, update: Update, message_mapping: dict):
        """Edit Discord channel message using HTTP API"""
        try:
            new_content = f"{update.edited_message.text or '[Media/File]'} *[edited]*"

            headers = create_header(discord_client, DISCORD_TOKEN)

            payload = {
                "content": new_content
            }

            response = requests.patch(
                f"https://discord.com/api/v9/channels/{message_mapping['discord_channel_id']}/messages/{message_mapping['discord_message_id']}",
                json=payload,
                headers=headers
            )

            if response.status_code == 200:
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

                logger.info(f"Edited Discord channel message {message_mapping['discord_message_id']} for Telegram edit")
            else:
                logger.error(f"Failed to edit Discord channel message. Status: {response.status_code}, Response: {response.text}")

        except Exception as e:
            logger.error(f"Failed to edit Discord channel message: {e}")

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

    # Handle DMs (direct messages)
    if isinstance(message.channel, discord.DMChannel):
        await bridge.forward_discord_to_telegram(message)
        return

    # Handle server channel messages if they're connected
    if message.guild is not None:
        await bridge.forward_channel_to_telegram(message)

@discord_client.event
async def on_typing(channel, user, when):  # `when` is provided by discord.py's event interface
    # Ignore typing from the bot itself
    if user == discord_client.user:
        return

    topic_id = None

    # Handle DM typing
    if isinstance(channel, discord.DMChannel):
        mapping = mappings_collection.find_one({"discord_user_id": user.id})
        if mapping:
            topic_id = mapping["telegram_topic_id"]

    # Handle server channel typing
    elif hasattr(channel, 'guild') and channel.guild is not None:
        mapping = channel_mappings_collection.find_one({"discord_channel_id": channel.id})
        if mapping:
            topic_id = mapping["telegram_topic_id"]

    if topic_id is not None:
        try:
            await bridge.telegram_bot.send_chat_action(
                chat_id=TOPICS_CHANNEL_ID,
                message_thread_id=topic_id,
                action="typing"
            )
            logger.debug(f"Forwarded typing indicator from Discord user {user.id} to Telegram topic {topic_id}")
        except Exception as e:
            logger.error(f"Failed to send typing action to Telegram: {e}")

@discord_client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    # Ignore messages from the bot itself
    if after.author == discord_client.user:
        return

    # Handle DM edits
    if isinstance(after.channel, discord.DMChannel):
        await bridge.edit_discord_message_in_telegram(before, after)
        return

    # Handle server channel message edits if they're connected
    if after.guild is not None:
        await bridge.edit_channel_message_in_telegram(before, after)

def _get_custom_activity(member) -> discord.CustomActivity | None:
    """Return the CustomActivity for a member, or None if not set."""
    for activity in getattr(member, 'activities', []):
        if isinstance(activity, discord.CustomActivity):
            return activity
    return None

@discord_client.event
async def on_presence_update(before, after):
    # Ignore self
    if after.id == discord_client.user.id:
        return

    after_custom = _get_custom_activity(after)
    after_text = after_custom.name if after_custom else None

    # When a user goes offline Discord clears all activities; don't notify for that
    if after_text is None and after.status == discord.Status.offline:
        return

    # Only notify if a DM topic already exists for this user
    mapping = mappings_collection.find_one({"discord_user_id": after.id})
    if not mapping:
        return

    topic_id = mapping["telegram_topic_id"]
    username = mapping["discord_username"]

    # Check in-memory cache first to prevent rapid-fire duplicates from Discord's multiple presence_update events
    if not bridge._should_send_status_update(after.id, after_text):
        logger.debug(f"Skipping duplicate status update for {username}: same status sent recently")
        return

    # Also compare against the last status we notified (DB-stored) for cross-restart deduplication
    stored_status = mapping.get("custom_status")
    if after_text == stored_status:
        bridge._record_status_sent(after.id, after_text)  # Still record it in memory even though it matches DB
        return

    # Record status immediately to prevent race conditions from rapid Discord events
    bridge._record_status_sent(after.id, after_text)

    if after_text:
        emoji_part = ""
        if after_custom and after_custom.emoji:
            # Only use the emoji if it's a Unicode emoji; custom guild emojis render as <:name:id> in Telegram
            emoji = after_custom.emoji
            if emoji.is_unicode_emoji():
                emoji_part = f"{emoji.name} "
        # Escape Markdown special characters in user-provided status text and username
        status_msg = f"💬 **@{escape_markdown(username)}** set their status to: {emoji_part}{escape_markdown(after_text)}"
    else:
        status_msg = f"💬 **@{escape_markdown(username)}** cleared their custom status"

    status_message_id = mapping.get("status_message_id")

    need_new_message = status_message_id is None

    if status_message_id is not None:
        # Edit the existing pinned status message
        try:
            await bridge.telegram_bot.edit_message_text(
                chat_id=TOPICS_CHANNEL_ID,
                message_id=status_message_id,
                text=status_msg,
                parse_mode=ParseMode.MARKDOWN
            )
            logger.debug(f"Edited pinned status message {status_message_id} for {username}")
        except Exception as e:
            logger.error(f"Failed to edit status message for {username}: {e}. Will send a new message.")
            # Message may have been deleted; fall through to create a fresh one
            need_new_message = True
            status_message_id = None

    if need_new_message:
        # Unpin old topic messages and send a fresh status message
        try:
            await bridge.telegram_bot.unpin_all_forum_topic_messages(
                chat_id=TOPICS_CHANNEL_ID,
                message_thread_id=topic_id
            )
        except Exception as e:
            logger.debug(f"Could not unpin existing messages for {username} (may be none): {e}")

        try:
            sent = await bridge.telegram_bot.send_message(
                chat_id=TOPICS_CHANNEL_ID,
                message_thread_id=topic_id,
                text=status_msg,
                parse_mode=ParseMode.MARKDOWN
            )
            status_message_id = sent.message_id
            await bridge.telegram_bot.pin_chat_message(
                chat_id=TOPICS_CHANNEL_ID,
                message_id=status_message_id,
                disable_notification=True
            )
            logger.debug(f"Sent and pinned new status message {status_message_id} for {username}")
        except Exception as e:
            logger.error(f"Failed to send/pin status message for {username}: {e}")
            return

    # Persist the updated status and message ID
    try:
        mappings_collection.update_one(
            {"discord_user_id": after.id},
            {"$set": {"custom_status": after_text, "status_message_id": status_message_id}}
        )
    except Exception as e:
        logger.error(f"Failed to persist status info for {username}: {e}")

# Telegram handlers
async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming Telegram messages"""
    await bridge.forward_telegram_to_discord(update, context)

async def handle_telegram_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle edited Telegram messages"""
    await bridge.edit_telegram_message_in_discord(update, context)

async def handle_telegram_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages from Telegram"""
    await bridge.forward_telegram_to_discord(update, context)

async def handle_telegram_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reply messages from Telegram"""
    await bridge.forward_telegram_to_discord(update, context)

async def handle_telegram_reply_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reply photo messages from Telegram"""
    await bridge.forward_telegram_to_discord(update, context)

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ping command"""
    await update.message.reply_text("pong")

async def data_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /data command - shows channel and topic info"""
    channel_id = update.message.chat_id
    topic_id = update.message.message_thread_id

    response = f"Channel ID: `{channel_id}`"

    if topic_id:
        response += f"\nTopic ID: `{topic_id}`"
    else:
        response += "\nNot in a topic"

    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /connect command - links Discord channel to new Telegram topic"""
    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "Usage: `/connect <discord_channel_id>`\n\n"
            "Example: `/connect 123456789012345678`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        discord_channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid channel ID. Please provide a valid Discord channel ID (numbers only).",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        # Check if channel is already connected
        existing_mapping = channel_mappings_collection.find_one({"discord_channel_id": discord_channel_id})
        if existing_mapping:
            topic_id = existing_mapping["telegram_topic_id"]
            await update.message.reply_text(
                f"❌ Discord channel `{discord_channel_id}` is already connected to topic `{topic_id}`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # Get channel info from Discord using HTTP API
        channel_info = await bridge._get_discord_channel_info(discord_channel_id)
        if channel_info:
            channel_name = channel_info.get("name", "unknown-channel")
            server_name = channel_info.get("guild_name", "unknown-server")
            topic_name = f"{channel_name}({server_name})"
        else:
            topic_name = f"Channel-{discord_channel_id}(unknown-server)"
            channel_name = f"Channel-{discord_channel_id}"

        # Create a new topic for this channel
        topic = await bridge.telegram_bot.create_forum_topic(
            chat_id=TOPICS_CHANNEL_ID,
            name=topic_name
        )

        # Store mapping in database
        mapping_doc = {
            "discord_channel_id": discord_channel_id,
            "discord_channel_name": channel_name,
            "telegram_topic_id": topic.message_thread_id,
            "created_at": datetime.utcnow(),
            "created_by_user": update.message.from_user.username or update.message.from_user.first_name
        }
        channel_mappings_collection.insert_one(mapping_doc)

        await update.message.reply_text(
            f"✅ **Connected Successfully!**\n\n"
            f"Discord Channel: `{discord_channel_id}`\n"
            f"Telegram Topic: `{topic.message_thread_id}`\n\n"
            f"Messages from the Discord channel will now be forwarded to this topic.",
            parse_mode=ParseMode.MARKDOWN
        )

        logger.info(f"Created channel mapping: Discord {discord_channel_id} -> Telegram topic {topic.message_thread_id}")

    except Exception as e:
        logger.error(f"Failed to create channel connection: {e}")
        await update.message.reply_text(
            f"❌ **Failed to create connection**\n\n"
            f"Error: {str(e)}\n\n"
            f"Please check that the channel ID is valid and the bot has necessary permissions.",
            parse_mode=ParseMode.MARKDOWN
        )

async def unlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unlink command - removes Discord channel link when run in a linked topic"""
    if not update.message.message_thread_id:
        await update.message.reply_text(
            "❌ This command must be run in a topic thread.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    topic_id = update.message.message_thread_id

    try:
        # Find the channel mapping for this topic
        mapping = channel_mappings_collection.find_one({"telegram_topic_id": topic_id})

        if not mapping:
            await update.message.reply_text(
                "❌ This topic is not linked to any Discord channel.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        discord_channel_id = mapping["discord_channel_id"]
        channel_name = mapping.get("discord_channel_name", f"Channel-{discord_channel_id}")

        # Remove the mapping from database
        result = channel_mappings_collection.delete_one({"telegram_topic_id": topic_id})

        if result.deleted_count > 0:
            await update.message.reply_text(
                f"✅ **Unlinked Successfully!**\n\n"
                f"Discord Channel: `{channel_name}` (`{discord_channel_id}`)\n"
                f"Telegram Topic: `{topic_id}`\n\n"
                f"Messages will no longer be forwarded between this topic and the Discord channel.",
                parse_mode=ParseMode.MARKDOWN
            )

            logger.info(f"Removed channel mapping: Discord {discord_channel_id} -> Telegram topic {topic_id}")
        else:
            await update.message.reply_text(
                "❌ Failed to remove the link. Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )

    except Exception as e:
        logger.error(f"Failed to unlink channel: {e}")
        await update.message.reply_text(
            f"❌ **Failed to unlink**\n\n"
            f"Error: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )

def run_discord_bot():
    """Run Discord bot in a separate thread"""
    print("Starting Discord selfbot...")
    try:
        discord_client.run(DISCORD_TOKEN)
    except Exception as e:
        logger.error(f"Failed to start Discord bot: {e}")

async def header_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
                f"Header test:\n`{create_header(discord_client, 'DISCORD_TOKEN')}`",
                parse_mode=ParseMode.MARKDOWN
    )

async def resetstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resetstatus command - clears custom_status and status_message_id for this topic's mapping"""
    topic_id = update.message.message_thread_id
    if not topic_id:
        await update.message.reply_text(
            "❌ This command must be run in a topic thread.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Check DM mapping first
    mapping = mappings_collection.find_one({"telegram_topic_id": topic_id})
    collection = mappings_collection
    if not mapping:
        mapping = channel_mappings_collection.find_one({"telegram_topic_id": topic_id})
        collection = channel_mappings_collection

    if not mapping:
        await update.message.reply_text(
            "❌ No mapping found for this topic.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    collection.update_one(
        {"_id": mapping["_id"]},
        {"$set": {"custom_status": None, "status_message_id": None}}
    )

    # Also clear the in-memory status cache for this user
    discord_user_id = mapping.get("discord_user_id")
    if discord_user_id and discord_user_id in bridge._last_status_sent:
        del bridge._last_status_sent[discord_user_id]

    await update.message.reply_text(
        "✅ Status reset. The next Discord status change will create a fresh pinned message.",
        parse_mode=ParseMode.MARKDOWN
    )
    logger.info(f"resetstatus executed for topic {topic_id}")

def run_telegram_bot():
    """Run Telegram bot in main thread"""
    # Add ping command handler
    ping_handler = CommandHandler("ping", ping_command)
    telegram_app.add_handler(ping_handler)

    # Add data command handler
    data_handler = CommandHandler("data", data_command)
    telegram_app.add_handler(data_handler)

    # Add connect command handler
    connect_handler = CommandHandler("connect", connect_command)
    telegram_app.add_handler(connect_handler)

    # Add unlink command handler
    unlink_handler = CommandHandler("unlink", unlink_command)
    telegram_app.add_handler(unlink_handler)

    # Add resetstatus command handler
    resetstatus_handler = CommandHandler("resetstatus", resetstatus_command)
    telegram_app.add_handler(resetstatus_handler)

    # Add header command handler for testing
    header_handler = CommandHandler("header", header_command)
    telegram_app.add_handler(header_handler)

    # Add message handlers for topic messages (matching old.py structure)
    # Text messages (excluding photos and replies)

    # Add edit handler for edited messages
    edit_handler = MessageHandler(
        filters.UpdateType.EDITED_MESSAGE,
        handle_telegram_edit
    )
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.PHOTO & ~filters.REPLY, handle_telegram_message))
    telegram_app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND & ~filters.REPLY, handle_telegram_message))
    telegram_app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND & ~filters.REPLY, handle_telegram_message))
    telegram_app.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND & ~filters.REPLY, handle_telegram_message))
    telegram_app.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND & ~filters.PHOTO, handle_telegram_message))
    telegram_app.add_handler(MessageHandler(filters.REPLY & filters.PHOTO & ~filters.COMMAND, handle_telegram_message))
    telegram_app.add_handler(MessageHandler(filters.REPLY & filters.Document.ALL & ~filters.COMMAND, handle_telegram_message))
    telegram_app.add_handler(MessageHandler(filters.REPLY & filters.VIDEO & ~filters.COMMAND, handle_telegram_message))
    telegram_app.add_handler(edit_handler)

    # Start Telegram bot
    print("Starting Telegram bot...")
    telegram_app.run_polling(drop_pending_updates=True)

def main():
    """Start both bots"""
    try:
        # Initialize database first
        initialize_database()

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
