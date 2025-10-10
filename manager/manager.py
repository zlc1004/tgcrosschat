#!/usr/bin/env python3
"""
TGCrossChat Manager Bot
Manages multiple TGCrossChat instances through Telegram bot interface
"""

import os
import json
import hashlib
import subprocess
import shutil
import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ConversationHandler, ContextTypes, filters
from telegram.constants import ParseMode

import config

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
WAITING_DISCORD_TOKEN, WAITING_TELEGRAM_TOKEN, WAITING_TOPICS_CHANNEL = range(3)

# Data file path
DATA_FILE = Path("data.json")
INSTANCES_DIR = Path("instances")

class InstanceManager:
    def __init__(self):
        self.data_file = DATA_FILE
        self.instances_dir = INSTANCES_DIR

        # Ensure instances directory exists
        self.instances_dir.mkdir(exist_ok=True)

        # Load existing data
        self.instances = self.load_data()

    def load_data(self) -> List[Dict]:
        """Load instances data from JSON file"""
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading data file: {e}")
                return []
        return []

    def save_data(self):
        """Save instances data to JSON file"""
        try:
            with open(self.data_file, 'w') as f:
                json.dump(self.instances, f, indent=2)
        except IOError as e:
            logger.error(f"Error saving data file: {e}")

    def generate_instance_hash(self, chat_id: str, discord_token: str, telegram_token: str) -> str:
        """Generate SHA256 hash for instance identification"""
        combined = f"{chat_id}{discord_token}{telegram_token}"
        return hashlib.sha256(combined.encode()).hexdigest()

    def get_instance_by_chat_id(self, chat_id: str) -> Optional[Dict]:
        """Get instance by chat ID"""
        for instance in self.instances:
            if instance["chatid"] == chat_id:
                return instance
        return None

    def create_instance(self, chat_id: str, discord_token: str, telegram_token: str, topics_channel_id: str) -> Dict:
        """Create a new TGCrossChat instance"""
        # Generate unique hash for this instance
        instance_hash = self.generate_instance_hash(chat_id, discord_token, telegram_token)

        instance_data = {
            "chatid": chat_id,
            "discord_token": discord_token,
            "telegram_token": telegram_token,
            "topics_channel_id": topics_channel_id,
            "docker_stack_name": instance_hash
        }

        # Clone repository
        instance_path = self.instances_dir / instance_hash
        if instance_path.exists():
            shutil.rmtree(instance_path)

        logger.info(f"Cloning repository to {instance_path}")
        try:
            subprocess.run([
                "git", "clone",
                "https://github.com/zlc1004/tgcrosschat.git",
                str(instance_path)
            ], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Git clone failed: {e}")
            raise Exception(f"Failed to clone repository: {e.stderr}")

        # Create .env file
        env_example_path = instance_path / ".env.example"
        env_path = instance_path / ".env"

        if not env_example_path.exists():
            raise Exception(".env.example not found in cloned repository")

        # Read .env.example and replace values
        with open(env_example_path, 'r') as f:
            env_content = f.read()

        # Replace placeholder values
        env_content = env_content.replace("your_discord_user_token_here", discord_token)
        env_content = env_content.replace("your_telegram_bot_token_here", telegram_token)
        env_content = env_content.replace("your_telegram_topics_channel_id_here", topics_channel_id)

        # Write .env file
        with open(env_path, 'w') as f:
            f.write(env_content)

        # Start Docker Compose
        logger.info(f"Starting Docker Compose for instance {instance_hash}")
        try:
            subprocess.run([
                "docker", "compose", "-p", instance_hash, "up", "-d"
            ], cwd=instance_path, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Docker compose failed: {e}")
            # Clean up on failure
            if instance_path.exists():
                shutil.rmtree(instance_path)
            raise Exception(f"Failed to start Docker containers: {e.stderr}")

        # Save instance data
        self.instances.append(instance_data)
        self.save_data()

        return instance_data

    def stop_instance(self, docker_stack_name: str) -> bool:
        """Stop and remove a TGCrossChat instance"""
        instance_path = self.instances_dir / docker_stack_name

        if not instance_path.exists():
            return False

        try:
            # Stop Docker Compose
            subprocess.run([
                "docker", "compose", "-p", docker_stack_name, "down", "-v"
            ], cwd=instance_path, check=True, capture_output=True, text=True)

            # Remove instance directory
            shutil.rmtree(instance_path)

            # Remove from instances list
            self.instances = [inst for inst in self.instances if inst["docker_stack_name"] != docker_stack_name]
            self.save_data()

            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to stop instance {docker_stack_name}: {e}")
            return False

    def list_instances(self) -> List[Dict]:
        """List all instances"""
        return self.instances.copy()

# Global instance manager
instance_manager = InstanceManager()

async def is_authorized_chat(update: Update) -> bool:
    """Check if the message is from authorized user in DM"""
    if not update.effective_chat or not update.effective_user:
        return False

    # Must be a private chat (DM)
    if update.effective_chat.type != "private":
        return False

    # Must be from the authorized username
    return update.effective_user.username == config.telegram_username

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    if not await is_authorized_chat(update):
        return

    keyboard = [
        [InlineKeyboardButton("📋 List Instances", callback_data="list_instances")],
        [InlineKeyboardButton("➕ Create Instance", callback_data="create_instance")],
        [InlineKeyboardButton("❌ Stop Instance", callback_data="stop_instance")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🤖 **TGCrossChat Manager**\n\n"
        "Welcome to the TGCrossChat instance manager!\n"
        "Use the buttons below to manage your instances.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    if not await is_authorized_chat(update):
        return

    query = update.callback_query
    await query.answer()

    if query.data == "list_instances":
        await list_instances_callback(update, context)
    elif query.data == "create_instance":
        await create_instance_callback(update, context)
    elif query.data == "stop_instance":
        await stop_instance_callback(update, context)
    elif query.data == "help":
        await help_callback(update, context)
    elif query.data.startswith("stop_"):
        stack_name = query.data[5:]  # Remove "stop_" prefix
        await confirm_stop_instance(update, context, stack_name)

async def list_instances_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all instances"""
    instances = instance_manager.list_instances()

    if not instances:
        await update.callback_query.edit_message_text(
            "📭 No instances found.\n\n"
            "Use /start to create your first instance."
        )
        return

    message = "📋 **Active Instances**\n\n"
    for i, instance in enumerate(instances, 1):
        message += f"**{i}.** Instance `{instance['docker_stack_name'][:8]}...`\n"
        message += f"   Chat ID: `{instance['chatid']}`\n"
        message += f"   Topics Channel: `{instance['topics_channel_id']}`\n\n"

    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def create_instance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start instance creation process"""
    chat_id = str(update.effective_chat.id)

    # Check if instance already exists for this chat
    existing_instance = instance_manager.get_instance_by_chat_id(chat_id)
    if existing_instance:
        await update.callback_query.edit_message_text(
            f"⚠️ An instance already exists for this chat!\n\n"
            f"Instance ID: `{existing_instance['docker_stack_name'][:8]}...`\n"
            f"Topics Channel: `{existing_instance['topics_channel_id']}`\n\n"
            f"Please stop the existing instance first if you want to create a new one.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await update.callback_query.edit_message_text(
        "🔐 **Creating New Instance**\n\n"
        "Please provide your **Discord User Token**:\n\n"
        "⚠️ *This should be your Discord user token (selfbot), not a bot token.*\n"
        "*Send the token as a message.*",
        parse_mode=ParseMode.MARKDOWN
    )

    context.user_data['creating_instance'] = True
    context.user_data['chat_id'] = chat_id
    return WAITING_DISCORD_TOKEN

async def stop_instance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show instances that can be stopped"""
    instances = instance_manager.list_instances()

    if not instances:
        await update.callback_query.edit_message_text(
            "📭 No instances to stop.\n\n"
            "Use /start to return to the main menu."
        )
        return

    keyboard = []
    for instance in instances:
        short_id = instance['docker_stack_name'][:8]
        keyboard.append([InlineKeyboardButton(
            f"❌ Stop {short_id}... (Chat: {instance['chatid']})",
            callback_data=f"stop_{instance['docker_stack_name']}"
        )])

    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        "❌ **Stop Instance**\n\n"
        "Select an instance to stop:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def confirm_stop_instance(update: Update, context: ContextTypes.DEFAULT_TYPE, stack_name: str):
    """Confirm and stop instance"""
    success = instance_manager.stop_instance(stack_name)

    if success:
        await update.callback_query.edit_message_text(
            f"✅ **Instance Stopped**\n\n"
            f"Instance `{stack_name[:8]}...` has been stopped and removed.\n\n"
            f"All containers and data have been cleaned up.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.callback_query.edit_message_text(
            f"❌ **Failed to Stop Instance**\n\n"
            f"Could not stop instance `{stack_name[:8]}...`\n\n"
            f"Please check the logs for more details.",
            parse_mode=ParseMode.MARKDOWN
        )

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help information"""
    help_text = """
ℹ️ **TGCrossChat Manager Help**

**Commands:**
• `/start` - Show main menu
• `/status` - Quick status overview

**Features:**
• **Create Instance** - Set up a new TGCrossChat bridge
• **List Instances** - View all active instances
• **Stop Instance** - Remove an instance and clean up

**Instance Creation Process:**
1. Provide Discord user token (selfbot)
2. Provide Telegram bot token
3. Provide Telegram topics channel ID
4. System automatically creates and starts instance

**Notes:**
• Each chat can only have one active instance
• Instances are isolated using Docker containers
• All data is cleaned up when stopping instances
• Use Discord user tokens, not bot tokens
    """

    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        help_text.strip(),
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_discord_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Discord token input"""
    if not context.user_data.get('creating_instance'):
        return ConversationHandler.END

    discord_token = update.message.text.strip()
    context.user_data['discord_token'] = discord_token

    await update.message.reply_text(
        "🤖 **Step 2/3**\n\n"
        "Please provide your **Telegram Bot Token**:\n\n"
        "ℹ️ *Get this from @BotFather on Telegram.*\n"
        "*Send the token as a message.*",
        parse_mode=ParseMode.MARKDOWN
    )

    return WAITING_TELEGRAM_TOKEN

async def handle_telegram_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Telegram token input"""
    if not context.user_data.get('creating_instance'):
        return ConversationHandler.END

    telegram_token = update.message.text.strip()
    context.user_data['telegram_token'] = telegram_token

    await update.message.reply_text(
        "📋 **Step 3/3**\n\n"
        "Please provide your **Telegram Topics Channel ID**:\n\n"
        "ℹ️ *This should be the ID of a Telegram channel/group where topics will be created.*\n"
        "*Send the channel ID as a message (e.g., -1001234567890).*",
        parse_mode=ParseMode.MARKDOWN
    )

    return WAITING_TOPICS_CHANNEL

async def handle_topics_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle topics channel ID and create instance"""
    if not context.user_data.get('creating_instance'):
        return ConversationHandler.END

    topics_channel_id = update.message.text.strip()

    # Validate channel ID format
    try:
        int(topics_channel_id)
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid channel ID format. Please provide a valid numeric channel ID."
        )
        return WAITING_TOPICS_CHANNEL

    # Create instance
    creating_message = await update.message.reply_text(
        "⏳ **Creating Instance...**\n\n"
        "This may take a few minutes. Please wait...\n\n"
        "🔄 Cloning repository...",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        instance_data = instance_manager.create_instance(
            chat_id=context.user_data['chat_id'],
            discord_token=context.user_data['discord_token'],
            telegram_token=context.user_data['telegram_token'],
            topics_channel_id=topics_channel_id
        )

        await creating_message.edit_text(
            "✅ **Instance Created Successfully!**\n\n"
            f"Instance ID: `{instance_data['docker_stack_name'][:8]}...`\n"
            f"Chat ID: `{instance_data['chatid']}`\n"
            f"Topics Channel: `{instance_data['topics_channel_id']}`\n\n"
            "🚀 Your TGCrossChat bridge is now running!\n"
            "The bot will start forwarding messages between Discord and Telegram.",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.error(f"Failed to create instance: {e}")
        await creating_message.edit_text(
            f"❌ **Instance Creation Failed**\n\n"
            f"Error: {str(e)}\n\n"
            f"Please check your tokens and try again.",
            parse_mode=ParseMode.MARKDOWN
        )

    # Clean up user data
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel instance creation"""
    context.user_data.clear()
    await update.message.reply_text("❌ Instance creation cancelled.")
    return ConversationHandler.END

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show quick status overview"""
    if not await is_authorized_chat(update):
        return

    instances = instance_manager.list_instances()

    message = f"📊 **TGCrossChat Manager Status**\n\n"
    message += f"🔧 Active Instances: **{len(instances)}**\n"

    if instances:
        message += f"\n📋 **Instance List:**\n"
        for instance in instances:
            short_id = instance['docker_stack_name'][:8]
            message += f"• `{short_id}...` (Chat: {instance['chatid']})\n"

    message += f"\n💡 Use /start for full management interface."

    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show chat ID - works in any chat"""
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    message = f"🆔 **Chat Information**\n\n"
    message += f"Chat ID: `{chat_id}`\n"
    message += f"Chat Type: `{chat_type}`"

    if update.effective_chat.title:
        message += f"\nChat Title: `{update.effective_chat.title}`"

    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def back_to_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return to main menu"""
    keyboard = [
        [InlineKeyboardButton("📋 List Instances", callback_data="list_instances")],
        [InlineKeyboardButton("➕ Create Instance", callback_data="create_instance")],
        [InlineKeyboardButton("❌ Stop Instance", callback_data="stop_instance")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        "🤖 **TGCrossChat Manager**\n\n"
        "Welcome to the TGCrossChat instance manager!\n"
        "Use the buttons below to manage your instances.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

def main():
    """Main function to run the manager bot"""
    if not config.telegram_bot_token or config.telegram_bot_token == "your_manager_bot_token_here":
        logger.error("Please configure telegram_bot_token in config.py")
        return

    if not config.telegram_username or config.telegram_username == "your_username_here":
        logger.error("Please configure telegram_username in config.py")
        return

    # Create application
    application = Application.builder().token(config.telegram_bot_token).build()

    # Create conversation handler for instance creation
    conversation_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_instance_callback, pattern="^create_instance$")],
        states={
            WAITING_DISCORD_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_discord_token)],
            WAITING_TELEGRAM_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_token)],
            WAITING_TOPICS_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_topics_channel)]
        },
        fallbacks=[CommandHandler("cancel", cancel_creation)],
        allow_reentry=True
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(conversation_handler)
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(list_instances|stop_instance|help|back_to_menu)$"))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^stop_.*"))
    application.add_handler(CallbackQueryHandler(back_to_menu_callback, pattern="^back_to_menu$"))

    logger.info("TGCrossChat Manager Bot starting...")
    logger.info(f"Authorized username: @{config.telegram_username}")
    logger.info("Bot will only respond to DMs from the authorized user")

    # Start the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
