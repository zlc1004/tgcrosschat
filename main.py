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
                bot.http.browser_version.split('.')[0]
            ),
            'Sec-CH-UA-Mobile': '?0',
            'Sec-CH-UA-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': bot.http.user_agent,
            'X-Discord-Locale': 'en-US',
            'X-Debug-Options': 'bugReporterEnabled',
            'X-Super-Properties': bot.http.encoded_super_properties,
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

        logger.info("Database initialization completed successfully")
        logger.info(f"Available collections: {db.list_collection_names()}")

    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

# Initialize bots
discord_client = discord.Client()
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# Store Discord event loop for cross-thread calls
discord_loop = None

class MessageBridge:
    def __init__(self):
        self.telegram_bot = telegram_app.bot

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

            # Handle attachments
            for attachment in message.attachments:
                try:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        telegram_attachment = await self.telegram_bot.send_photo(
                            chat_id=TOPICS_CHANNEL_ID,
                            message_thread_id=topic_id,
                            photo=attachment.url,
                            caption=f"Image from {user_display_name}"
                        )
                    else:
                        telegram_attachment = await self.telegram_bot.send_document(
                            chat_id=TOPICS_CHANNEL_ID,
                            message_thread_id=topic_id,
                            document=attachment.url,
                            caption=f"File from {user_display_name}: {attachment.filename}"
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
                try:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        telegram_attachment = await self.telegram_bot.send_photo(
                            chat_id=TOPICS_CHANNEL_ID,
                            message_thread_id=topic_id,
                            photo=attachment.url,
                            caption=f"Image from {user_display_name}"
                        )
                    else:
                        telegram_attachment = await self.telegram_bot.send_document(
                            chat_id=TOPICS_CHANNEL_ID,
                            message_thread_id=topic_id,
                            document=attachment.url,
                            caption=f"File from {user_display_name}: {attachment.filename}"
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
        """Forward Telegram topic message to Discord DM or channel"""
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

            # Handle images and documents - append URL to content like old.py
            if len(update.message.photo) > 0:
                # Get the largest photo using same method as old.py
                file_url = (await update.message.photo[-1].get_file()).file_path
                content = (content or "") + "\n" + file_url

            elif update.message.document:
                doc = update.message.document
                file_url = (await doc.get_file()).file_path
                content = (content or "") + "\n" + file_url

            elif update.message.video:
                video = update.message.video
                file_url = (await video.get_file()).file_path
                content = (content or "") + "\n" + file_url

            # Handle text messages
            if not content:
                content = "[Empty message]"

            logger.info(f"Attempting to send text message to Discord user {discord_user_id}: '{content}'")

            # Prepare the request payload
            payload = {
                "content": content
            }

            if message_reference:
                payload["message_reference"] = message_reference

            # Send the message
            headers = create_header(discord_client, DISCORD_TOKEN)

            response = requests.post(
                f"https://discord.com/api/v9/channels/{dm_channel_id}/messages",
                json=payload,
                headers=headers
            )

            if response.status_code == 200:
                discord_msg_data = response.json()
                discord_msg_id = discord_msg_data["id"]

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

    async def _send_discord_file(self, discord_user_id: int, dm_channel_id: str, content: str, file_url: str, filename: str, message_reference: dict = None):
        """Send file to Discord using multipart form data"""
        try:
            # Download the file
            file_response = requests.get(file_url)
            if file_response.status_code != 200:
                logger.error(f"Failed to download file from Telegram: {file_response.status_code}")
                return

            # Prepare multipart form data
            files = {
                'files[0]': (filename, file_response.content)
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

    async def _send_discord_channel_file(self, discord_channel_id: int, content: str, file_url: str, filename: str, message_reference: dict = None):
        """Send file to Discord channel using multipart form data"""
        try:
            # Download the file
            file_response = requests.get(file_url)
            if file_response.status_code != 200:
                logger.error(f"Failed to download file from Telegram: {file_response.status_code}")
                return

            # Prepare multipart form data
            files = {
                'files[0]': (filename, file_response.content)
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

            # Send just the content without Telegram header
            full_content = content

            # Handle media for channels - append URLs to content like old.py
            if update.message.photo:
                # Get the largest photo using same method as old.py
                file_url = (await update.message.photo[-1].get_file()).file_path
                full_content = (full_content or "") + "\n" + file_url

            elif update.message.document:
                # Get document URL
                file_url = (await update.message.document.get_file()).file_path
                full_content = (full_content or "") + "\n" + file_url

            elif update.message.video:
                # Get video URL
                file_url = (await update.message.video.get_file()).file_path
                full_content = (full_content or "") + "\n" + file_url

            if not full_content.strip():
                full_content = "[Empty message]"

            logger.info(f"Attempting to send message to Discord channel {discord_channel_id}: '{full_content}'")

            # Prepare the request payload
            payload = {
                "content": full_content
            }

            if message_reference:
                payload["message_reference"] = message_reference

            # Send the message
            headers = create_header(discord_client, DISCORD_TOKEN)

            response = requests.post(
                f"https://discord.com/api/v9/channels/{discord_channel_id}/messages",
                json=payload,
                headers=headers
            )

            if response.status_code == 200:
                discord_msg_data = response.json()
                discord_msg_id = discord_msg_data["id"]

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
    telegram_app.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND & ~filters.PHOTO, handle_telegram_message))
    telegram_app.add_handler(MessageHandler(filters.REPLY & filters.PHOTO & ~filters.COMMAND, handle_telegram_message))
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
