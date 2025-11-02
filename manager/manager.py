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
            "docker_stack_name": instance_hash,
            "status": "running"
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

    def pause_instance(self, docker_stack_name: str) -> bool:
        """Pause a TGCrossChat instance (stop containers but keep data)"""
        instance_path = self.instances_dir / docker_stack_name

        if not instance_path.exists():
            return False

        try:
            # Stop Docker Compose (without removing volumes)
            subprocess.run([
                "docker", "compose", "-p", docker_stack_name, "stop"
            ], cwd=instance_path, check=True, capture_output=True, text=True)

            # Update instance status
            for instance in self.instances:
                if instance["docker_stack_name"] == docker_stack_name:
                    instance["status"] = "stopped"
                    break
            self.save_data()

            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to pause instance {docker_stack_name}: {e}")
            return False

    def resume_instance(self, docker_stack_name: str) -> bool:
        """Resume a TGCrossChat instance"""
        instance_path = self.instances_dir / docker_stack_name

        if not instance_path.exists():
            return False

        try:
            # Start Docker Compose
            subprocess.run([
                "docker", "compose", "-p", docker_stack_name, "up", "-d"
            ], cwd=instance_path, check=True, capture_output=True, text=True)

            # Update instance status
            for instance in self.instances:
                if instance["docker_stack_name"] == docker_stack_name:
                    instance["status"] = "running"
                    break
            self.save_data()

            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to resume instance {docker_stack_name}: {e}")
            return False

    def stop_instance(self, docker_stack_name: str) -> bool:
        """Stop and remove a TGCrossChat instance completely"""
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

    def get_instance_status(self, docker_stack_name: str) -> str:
        """Get the current status of an instance"""
        instance_path = self.instances_dir / docker_stack_name

        if not instance_path.exists():
            return "missing"

        try:
            # Check if containers are running
            result = subprocess.run([
                "docker", "compose", "-p", docker_stack_name, "ps", "-q"
            ], cwd=instance_path, capture_output=True, text=True)

            if result.returncode == 0 and result.stdout.strip():
                # Check if containers are actually running
                container_ids = result.stdout.strip().split('\n')
                for container_id in container_ids:
                    if container_id:
                        inspect_result = subprocess.run([
                            "docker", "inspect", "-f", "{{.State.Running}}", container_id
                        ], capture_output=True, text=True)
                        if inspect_result.returncode == 0 and "true" in inspect_result.stdout:
                            return "running"
                return "stopped"
            else:
                return "stopped"
        except subprocess.CalledProcessError:
            return "unknown"

    def get_instance_details(self, docker_stack_name: str) -> dict:
        """Get detailed Docker information for an instance"""
        instance_path = self.instances_dir / docker_stack_name

        if not instance_path.exists():
            return {"error": "Instance directory not found"}

        try:
            # Get container information using docker compose ps with JSON format
            compose_result = subprocess.run([
                "docker", "compose", "-p", docker_stack_name, "ps", "--format", "json"
            ], cwd=instance_path, capture_output=True, text=True)

            details = {
                "instance_path": str(instance_path),
                "stack_name": docker_stack_name,
                "containers": []
            }

            if compose_result.returncode == 0 and compose_result.stdout.strip():
                # Parse JSON output
                import json
                for line in compose_result.stdout.strip().split('\n'):
                    if line:
                        try:
                            container_info = json.loads(line)
                            details["containers"].append(container_info)
                        except json.JSONDecodeError:
                            continue

            # Get additional container details if containers exist
            if details["containers"]:
                for container in details["containers"]:
                    container_id = container.get("ID", "")
                    if container_id:
                        # Get detailed container inspection
                        inspect_result = subprocess.run([
                            "docker", "inspect", container_id
                        ], capture_output=True, text=True)

                        if inspect_result.returncode == 0:
                            try:
                                import json
                                inspect_data = json.loads(inspect_result.stdout)[0]

                                # Add useful information
                                container["detailed_info"] = {
                                    "created": inspect_data.get("Created", ""),
                                    "started_at": inspect_data.get("State", {}).get("StartedAt", ""),
                                    "finished_at": inspect_data.get("State", {}).get("FinishedAt", ""),
                                    "restart_count": inspect_data.get("RestartCount", 0),
                                    "platform": inspect_data.get("Platform", ""),
                                    "image": inspect_data.get("Config", {}).get("Image", ""),
                                    "ports": inspect_data.get("NetworkSettings", {}).get("Ports", {}),
                                    "mounts": [
                                        {
                                            "source": mount.get("Source", ""),
                                            "destination": mount.get("Destination", ""),
                                            "type": mount.get("Type", "")
                                        }
                                        for mount in inspect_data.get("Mounts", [])
                                    ],
                                    "memory_usage": self._get_container_stats(container_id)
                                }
                            except (json.JSONDecodeError, IndexError):
                                container["detailed_info"] = {"error": "Failed to parse inspect data"}

            return details

        except subprocess.CalledProcessError as e:
            return {"error": f"Docker command failed: {e}"}
        except Exception as e:
            return {"error": f"Unexpected error: {e}"}

    def _get_container_stats(self, container_id: str) -> dict:
        """Get container resource usage statistics"""
        try:
            stats_result = subprocess.run([
                "docker", "stats", container_id, "--no-stream", "--format",
                "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}"
            ], capture_output=True, text=True, timeout=5)

            if stats_result.returncode == 0:
                lines = stats_result.stdout.strip().split('\n')
                if len(lines) > 1:  # Skip header
                    parts = lines[1].split('\t')
                    if len(parts) >= 5:
                        return {
                            "cpu_usage": parts[1],
                            "memory_usage": parts[2],
                            "network_io": parts[3],
                            "block_io": parts[4]
                        }
            return {"error": "Stats not available"}
        except subprocess.TimeoutExpired:
            return {"error": "Stats timeout"}
        except Exception:
            return {"error": "Stats unavailable"}

    def edit_instance_preserve_db(self, docker_stack_name: str, env_key: str, new_value: str) -> bool:
        """Edit instance by stopping containers, editing .env, and restarting"""
        instance_path = self.instances_dir / docker_stack_name

        if not instance_path.exists():
            logger.error(f"Instance path does not exist: {instance_path}")
            return False

        env_path = instance_path / ".env"
        if not env_path.exists():
            logger.error(f".env file does not exist: {env_path}")
            return False

        try:
            # Stop containers
            logger.info(f"Stopping containers for instance {docker_stack_name}")
            subprocess.run([
                "docker", "compose", "-p", docker_stack_name, "down"
            ], cwd=instance_path, check=True, capture_output=True, text=True)

            # Read current .env file
            with open(env_path, 'r') as f:
                env_content = f.read()

            # Replace the specific environment variable
            lines = env_content.split('\n')
            updated_lines = []
            key_found = False

            for line in lines:
                if line.startswith(f"{env_key}="):
                    updated_lines.append(f"{env_key}={new_value}")
                    key_found = True
                else:
                    updated_lines.append(line)

            # If key wasn't found, add it
            if not key_found:
                updated_lines.append(f"{env_key}={new_value}")

            # Write updated .env file
            with open(env_path, 'w') as f:
                f.write('\n'.join(updated_lines))

            # Start containers
            logger.info(f"Starting containers for instance {docker_stack_name}")
            subprocess.run([
                "docker", "compose", "-p", docker_stack_name, "up", "-d"
            ], cwd=instance_path, check=True, capture_output=True, text=True)

            # Update instance status
            for instance in self.instances:
                if instance["docker_stack_name"] == docker_stack_name:
                    instance["status"] = "running"
                    break
            self.save_data()

            logger.info(f"Successfully updated {env_key} for instance {docker_stack_name}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to edit instance {docker_stack_name}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error editing instance {docker_stack_name}: {e}")
            return False

    def recreate_instance(self, instance_index: int, discord_token: str = None, telegram_token: str = None, topics_channel_id: str = None) -> bool:
        """Recreate instance by deleting and creating new one"""
        if instance_index < 0 or instance_index >= len(self.instances):
            logger.error(f"Invalid instance index: {instance_index}")
            return False

        instance = self.instances[instance_index]
        old_stack_name = instance['docker_stack_name']
        chatid = instance['chatid']

        # Use new values or keep existing ones
        new_discord_token = discord_token or instance['discord_token']
        new_telegram_token = telegram_token or instance['telegram_token']
        new_topics_channel_id = topics_channel_id or instance['topics_channel_id']

        try:
            # Stop and remove old instance
            logger.info(f"Removing old instance {old_stack_name}")
            self.stop_instance(old_stack_name)

            # Create new instance with updated values
            logger.info(f"Creating new instance for chat {chatid}")
            new_instance = self.create_instance(chatid, new_discord_token, new_telegram_token, new_topics_channel_id)

            logger.info(f"Successfully recreated instance. Old: {old_stack_name}, New: {new_instance['docker_stack_name']}")
            return True

        except Exception as e:
            logger.error(f"Failed to recreate instance {old_stack_name}: {e}")
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
        [InlineKeyboardButton("⚙️ Manage Instances", callback_data="stop_instance")],
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
    elif query.data.startswith("manage_"):
        try:
            instance_index = int(query.data[7:])  # Remove "manage_" prefix
            await manage_instance_callback(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Instance Selection**\n\n"
                "Please try again or return to the main menu.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("pause_"):
        try:
            instance_index = int(query.data[6:])  # Remove "pause_" prefix
            await pause_instance_action(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("resume_"):
        try:
            instance_index = int(query.data[7:])  # Remove "resume_" prefix
            await resume_instance_action(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("edit_"):
        try:
            instance_index = int(query.data[5:])  # Remove "edit_" prefix
            await edit_instance_callback(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("delete_"):
        try:
            instance_index = int(query.data[7:])  # Remove "delete_" prefix
            await delete_instance_action(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("confirm_delete_"):
        try:
            instance_index = int(query.data[15:])  # Remove "confirm_delete_" prefix
            await confirm_delete_instance(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("details_"):
        try:
            instance_index = int(query.data[8:])  # Remove "details_" prefix
            await show_instance_details(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("edit_discord_token_"):
        try:
            instance_index = int(query.data[19:])  # Remove "edit_discord_token_" prefix
            await edit_discord_token_callback(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("edit_telegram_token_"):
        try:
            instance_index = int(query.data[20:])  # Remove "edit_telegram_token_" prefix
            await edit_telegram_token_callback(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("edit_topics_channel_"):
        try:
            instance_index = int(query.data[20:])  # Remove "edit_topics_channel_" prefix
            await edit_topics_channel_callback(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("preserve_db_"):
        try:
            instance_index = int(query.data[12:])  # Remove "preserve_db_" prefix
            await toggle_preserve_db(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("start_edit_discord_"):
        try:
            instance_index = int(query.data[19:])  # Remove "start_edit_discord_" prefix
            await start_edit_discord_token(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("start_edit_telegram_"):
        try:
            instance_index = int(query.data[20:])  # Remove "start_edit_telegram_" prefix
            await start_edit_telegram_token(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )
    elif query.data.startswith("start_edit_topics_"):
        try:
            instance_index = int(query.data[18:])  # Remove "start_edit_topics_" prefix
            await start_edit_topics_channel(update, context, instance_index)
        except ValueError:
            await query.edit_message_text(
                "❌ **Invalid Action**\n\n"
                "Please try again.",
                parse_mode=ParseMode.MARKDOWN
            )

async def list_instances_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all instances with status"""
    instances = instance_manager.list_instances()

    if not instances:
        await update.callback_query.edit_message_text(
            "📭 No instances found.\n\n"
            "Use /start to create your first instance."
        )
        return

    message = "📋 **Instance List**\n\n"
    for i, instance in enumerate(instances, 1):
        short_id = instance['docker_stack_name'][:8]

        # Get real-time status
        real_status = instance_manager.get_instance_status(instance['docker_stack_name'])
        status_emoji = "🟢" if real_status == "running" else "🔴" if real_status == "stopped" else "🟡"

        message += f"**{i}.** Instance `{short_id}...` {status_emoji}\n"
        message += f"   Chat ID: `{instance['chatid']}`\n"
        message += f"   Topics Channel: `{instance['topics_channel_id']}`\n"
        message += f"   Status: {real_status.title()}\n\n"

    message += "🟢 Running | 🔴 Stopped | 🟡 Unknown"

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
    """Show instance management menu"""
    instances = instance_manager.list_instances()

    if not instances:
        await update.callback_query.edit_message_text(
            "📭 No instances found.\n\n"
            "Use /start to return to the main menu."
        )
        return

    keyboard = []
    for i, instance in enumerate(instances):
        short_id = instance['docker_stack_name'][:8]
        current_status = instance.get('status', 'unknown')

        # Get real-time status
        real_status = instance_manager.get_instance_status(instance['docker_stack_name'])

        # Update stored status if different
        if real_status != current_status:
            instance['status'] = real_status
            instance_manager.save_data()

        status_emoji = "🟢" if real_status == "running" else "🔴" if real_status == "stopped" else "🟡"

        keyboard.append([InlineKeyboardButton(
            f"⚙️ Manage {short_id}... {status_emoji}",
            callback_data=f"manage_{i}"
        )])

    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        "⚙️ **Manage Instances**\n\n"
        "Select an instance to manage:\n"
        "🟢 Running | 🔴 Stopped | 🟡 Unknown",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def manage_instance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Show controls for a specific instance"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    short_id = instance['docker_stack_name'][:8]
    current_status = instance_manager.get_instance_status(instance['docker_stack_name'])

    # Update stored status
    instance['status'] = current_status
    instance_manager.save_data()

    status_emoji = "���" if current_status == "running" else "🔴" if current_status == "stopped" else "🟡"
    status_text = current_status.title()

    message = f"⚙️ **Instance Management**\n\n"
    message += f"Instance: `{short_id}...`\n"
    message += f"Chat ID: `{instance['chatid']}`\n"
    message += f"Topics Channel: `{instance['topics_channel_id']}`\n"
    message += f"Status: {status_emoji} {status_text}\n\n"
    message += f"Choose an action:"

    keyboard = []

    if current_status == "running":
        keyboard.append([InlineKeyboardButton("⏸️ Pause Instance", callback_data=f"pause_{instance_index}")])
    elif current_status == "stopped":
        keyboard.append([InlineKeyboardButton("▶️ Resume Instance", callback_data=f"resume_{instance_index}")])

    keyboard.append([InlineKeyboardButton("🔄 Refresh Status", callback_data=f"manage_{instance_index}")])
    keyboard.append([InlineKeyboardButton("📊 View Details", callback_data=f"details_{instance_index}")])
    keyboard.append([InlineKeyboardButton("✏️ Edit Instance", callback_data=f"edit_{instance_index}")])
    keyboard.append([InlineKeyboardButton("🗑️ Delete Instance", callback_data=f"delete_{instance_index}")])
    keyboard.append([InlineKeyboardButton("🔙 Back to List", callback_data="stop_instance")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def pause_instance_action(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Pause an instance"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    stack_name = instance['docker_stack_name']
    short_id = stack_name[:8]

    # Show processing message
    await update.callback_query.edit_message_text(
        f"⏸️ **Pausing Instance**\n\n"
        f"Stopping containers for `{short_id}...`\n"
        f"Please wait...",
        parse_mode=ParseMode.MARKDOWN
    )

    success = instance_manager.pause_instance(stack_name)

    if success:
        await update.callback_query.edit_message_text(
            f"✅ **Instance Paused**\n\n"
            f"Instance `{short_id}...` has been paused.\n"
            f"Containers are stopped but data is preserved.\n\n"
            f"Use Resume to start it again.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.callback_query.edit_message_text(
            f"❌ **Failed to Pause Instance**\n\n"
            f"Could not pause instance `{short_id}...`\n\n"
            f"Please check the logs for more details.",
            parse_mode=ParseMode.MARKDOWN
        )

async def resume_instance_action(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Resume an instance"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    stack_name = instance['docker_stack_name']
    short_id = stack_name[:8]

    # Show processing message
    await update.callback_query.edit_message_text(
        f"▶️ **Resuming Instance**\n\n"
        f"Starting containers for `{short_id}...`\n"
        f"Please wait...",
        parse_mode=ParseMode.MARKDOWN
    )

    success = instance_manager.resume_instance(stack_name)

    if success:
        await update.callback_query.edit_message_text(
            f"✅ **Instance Resumed**\n\n"
            f"Instance `{short_id}...` is now running.\n"
            f"The bridge should be active again.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.callback_query.edit_message_text(
            f"❌ **Failed to Resume Instance**\n\n"
            f"Could not resume instance `{short_id}...`\n\n"
            f"Please check the logs for more details.",
            parse_mode=ParseMode.MARKDOWN
        )

async def delete_instance_action(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Delete an instance completely"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    stack_name = instance['docker_stack_name']
    short_id = stack_name[:8]

    # Show confirmation
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_{instance_index}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"manage_{instance_index}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        f"🗑️ **Confirm Deletion**\n\n"
        f"Are you sure you want to **permanently delete** instance `{short_id}...`?\n\n"
        f"⚠️ This will:\n"
        f"• Stop all containers\n"
        f"• Remove all data and volumes\n"
        f"• Delete the instance directory\n\n"
        f"**This action cannot be undone!**",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def show_instance_details(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Show detailed Docker information for an instance"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    stack_name = instance['docker_stack_name']
    short_id = stack_name[:8]

    # Show loading message
    await update.callback_query.edit_message_text(
        f"📊 **Loading Details**\n\n"
        f"Gathering Docker information for `{short_id}...`\n"
        f"Please wait...",
        parse_mode=ParseMode.MARKDOWN
    )

    # Get detailed information
    details = instance_manager.get_instance_details(stack_name)

    if "error" in details:
        await update.callback_query.edit_message_text(
            f"❌ **Error Getting Details**\n\n"
            f"Instance: `{short_id}...`\n"
            f"Error: {details['error']}\n\n"
            f"The instance may not be running or accessible.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Format the details message
    message = f"📊 **Instance Details**\n\n"
    message += f"**Instance:** `{short_id}...`\n"
    message += f"**Stack Name:** `{stack_name}`\n"
    message += f"**Chat ID:** `{instance['chatid']}`\n"
    message += f"**Topics Channel:** `{instance['topics_channel_id']}`\n\n"

    # Container information
    containers = details.get("containers", [])
    if containers:
        message += f"**🐳 Containers ({len(containers)}):**\n\n"

        for i, container in enumerate(containers, 1):
            name = container.get("Name", "Unknown")
            service = container.get("Service", "Unknown")
            state = container.get("State", "Unknown")
            status = container.get("Status", "Unknown")

            state_emoji = "🟢" if state == "running" else "🔴" if state == "exited" else "🟡"

            message += f"**{i}. {service}**\n"
            message += f"   Name: `{name}`\n"
            message += f"   State: {state_emoji} {state}\n"
            message += f"   Status: `{status}`\n"

            # Add detailed info if available
            detailed = container.get("detailed_info", {})
            if detailed and "error" not in detailed:
                if detailed.get("image"):
                    message += f"   Image: `{detailed['image']}`\n"
                if detailed.get("created"):
                    created = detailed["created"][:19].replace("T", " ")  # Format timestamp
                    message += f"   Created: `{created}`\n"
                if detailed.get("restart_count", 0) > 0:
                    message += f"   Restarts: `{detailed['restart_count']}`\n"

                # Resource usage
                memory_stats = detailed.get("memory_usage", {})
                if memory_stats and "error" not in memory_stats:
                    message += f"   CPU: `{memory_stats.get('cpu_usage', 'N/A')}`\n"
                    message += f"   Memory: `{memory_stats.get('memory_usage', 'N/A')}`\n"
                    message += f"   Network: `{memory_stats.get('network_io', 'N/A')}`\n"
                    message += f"   Disk I/O: `{memory_stats.get('block_io', 'N/A')}`\n"

            message += "\n"
    else:
        message += "**🐳 Containers:** None found\n\n"

    # Truncate if message is too long (Telegram limit ~4096 characters)
    if len(message) > 4000:
        message = message[:3950] + "\n\n... (truncated)"

    keyboard = [
        [InlineKeyboardButton("🔄 Refresh Details", callback_data=f"details_{instance_index}")],
        [InlineKeyboardButton("🔙 Back to Instance", callback_data=f"manage_{instance_index}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def confirm_delete_instance(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Confirm and delete instance"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    stack_name = instance['docker_stack_name']
    short_id = stack_name[:8]

    # Show processing message
    await update.callback_query.edit_message_text(
        f"🗑️ **Deleting Instance**\n\n"
        f"Removing instance `{short_id}...`\n"
        f"Please wait...",
        parse_mode=ParseMode.MARKDOWN
    )

    success = instance_manager.stop_instance(stack_name)

    if success:
        await update.callback_query.edit_message_text(
            f"✅ **Instance Deleted**\n\n"
            f"Instance `{short_id}...` has been permanently deleted.\n\n"
            f"All containers and data have been removed.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.callback_query.edit_message_text(
            f"❌ **Failed to Delete Instance**\n\n"
            f"Could not delete instance `{short_id}...`\n\n"
            f"Please check the logs for more details.",
            parse_mode=ParseMode.MARKDOWN
        )

async def edit_instance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Show edit options for a specific instance"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    short_id = instance['docker_stack_name'][:8]

    message = f"✏️ **Edit Instance**\n\n"
    message += f"Instance: `{short_id}...`\n"
    message += f"Chat ID: `{instance['chatid']}`\n"
    message += f"Topics Channel: `{instance['topics_channel_id']}`\n\n"
    message += f"Choose what to edit:"

    keyboard = [
        [InlineKeyboardButton("🔑 Discord Token", callback_data=f"edit_discord_token_{instance_index}")],
        [InlineKeyboardButton("🤖 Telegram Bot Token", callback_data=f"edit_telegram_token_{instance_index}")],
        [InlineKeyboardButton("📋 Topics Channel ID", callback_data=f"edit_topics_channel_{instance_index}")],
        [InlineKeyboardButton("🔙 Back to Instance", callback_data=f"manage_{instance_index}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def edit_discord_token_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Show preserve DB option for Discord token edit"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    short_id = instance['docker_stack_name'][:8]

    # Store edit context in user data
    context.user_data['edit_type'] = 'discord_token'
    context.user_data['instance_index'] = instance_index
    preserve_db = context.user_data.get('preserve_db', True)  # Default true

    message = f"🔑 **Edit Discord Token**\n\n"
    message += f"Instance: `{short_id}...`\n\n"
    message += f"**Preserve Database:** {'✅ Yes' if preserve_db else '❌ No'}\n\n"
    message += f"• **Yes (Default):** Stop container → Edit token → Restart\n"
    message += f"• **No:** Delete instance → Recreate with new token\n\n"
    message += f"Toggle the preserve database option:"

    keyboard = [
        [InlineKeyboardButton(f"🔄 Preserve DB: {'✅ Yes' if preserve_db else '❌ No'}", callback_data=f"preserve_db_{instance_index}")],
        [InlineKeyboardButton("✏️ Continue Edit", callback_data=f"start_edit_discord_{instance_index}")],
        [InlineKeyboardButton("🔙 Back to Edit Menu", callback_data=f"edit_{instance_index}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def edit_telegram_token_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Show preserve DB option for Telegram token edit"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    short_id = instance['docker_stack_name'][:8]

    # Store edit context in user data
    context.user_data['edit_type'] = 'telegram_token'
    context.user_data['instance_index'] = instance_index
    preserve_db = context.user_data.get('preserve_db', True)  # Default true

    message = f"🤖 **Edit Telegram Bot Token**\n\n"
    message += f"Instance: `{short_id}...`\n\n"
    message += f"**Preserve Database:** {'✅ Yes' if preserve_db else '❌ No'}\n\n"
    message += f"• **Yes (Default):** Stop container → Edit token → Restart\n"
    message += f"• **No:** Delete instance → Recreate with new token\n\n"
    message += f"Toggle the preserve database option:"

    keyboard = [
        [InlineKeyboardButton(f"🔄 Preserve DB: {'✅ Yes' if preserve_db else '❌ No'}", callback_data=f"preserve_db_{instance_index}")],
        [InlineKeyboardButton("✏️ Continue Edit", callback_data=f"start_edit_telegram_{instance_index}")],
        [InlineKeyboardButton("��� Back to Edit Menu", callback_data=f"edit_{instance_index}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def edit_topics_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Show preserve DB option for Topics channel edit"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    short_id = instance['docker_stack_name'][:8]

    # Store edit context in user data
    context.user_data['edit_type'] = 'topics_channel'
    context.user_data['instance_index'] = instance_index
    preserve_db = context.user_data.get('preserve_db', True)  # Default true

    message = f"📋 **Edit Topics Channel ID**\n\n"
    message += f"Instance: `{short_id}...`\n\n"
    message += f"**Preserve Database:** {'✅ Yes' if preserve_db else '❌ No'}\n\n"
    message += f"• **Yes (Default):** Stop container → Edit channel → Restart\n"
    message += f"• **No:** Delete instance → Recreate with new channel\n\n"
    message += f"Toggle the preserve database option:"

    keyboard = [
        [InlineKeyboardButton(f"🔄 Preserve DB: {'✅ Yes' if preserve_db else '❌ No'}", callback_data=f"preserve_db_{instance_index}")],
        [InlineKeyboardButton("✏️ Continue Edit", callback_data=f"start_edit_topics_{instance_index}")],
        [InlineKeyboardButton("🔙 Back to Edit Menu", callback_data=f"edit_{instance_index}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def toggle_preserve_db(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Toggle preserve database setting"""
    current_preserve = context.user_data.get('preserve_db', True)
    context.user_data['preserve_db'] = not current_preserve

    edit_type = context.user_data.get('edit_type')

    # Redirect back to appropriate edit callback
    if edit_type == 'discord_token':
        await edit_discord_token_callback(update, context, instance_index)
    elif edit_type == 'telegram_token':
        await edit_telegram_token_callback(update, context, instance_index)
    elif edit_type == 'topics_channel':
        await edit_topics_channel_callback(update, context, instance_index)

async def start_edit_discord_token(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Start editing Discord token"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    short_id = instance['docker_stack_name'][:8]
    preserve_db = context.user_data.get('preserve_db', True)

    message = f"🔑 **Edit Discord Token**\n\n"
    message += f"Instance: `{short_id}...`\n"
    message += f"Preserve Database: {'✅ Yes' if preserve_db else '❌ No'}\n\n"
    message += f"Please send the new Discord user token:"

    context.user_data['editing_discord_token'] = True
    context.user_data['edit_instance_index'] = instance_index

    await update.callback_query.edit_message_text(
        message,
        parse_mode=ParseMode.MARKDOWN
    )

async def start_edit_telegram_token(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Start editing Telegram token"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    short_id = instance['docker_stack_name'][:8]
    preserve_db = context.user_data.get('preserve_db', True)

    message = f"🤖 **Edit Telegram Bot Token**\n\n"
    message += f"Instance: `{short_id}...`\n"
    message += f"Preserve Database: {'✅ Yes' if preserve_db else '❌ No'}\n\n"
    message += f"Please send the new Telegram bot token:"

    context.user_data['editing_telegram_token'] = True
    context.user_data['edit_instance_index'] = instance_index

    await update.callback_query.edit_message_text(
        message,
        parse_mode=ParseMode.MARKDOWN
    )

async def start_edit_topics_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, instance_index: int):
    """Start editing Topics channel ID"""
    instances = instance_manager.list_instances()

    if instance_index < 0 or instance_index >= len(instances):
        await update.callback_query.edit_message_text(
            "❌ **Invalid Instance**\n\n"
            "The selected instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instance = instances[instance_index]
    short_id = instance['docker_stack_name'][:8]
    preserve_db = context.user_data.get('preserve_db', True)

    message = f"📋 **Edit Topics Channel ID**\n\n"
    message += f"Instance: `{short_id}...`\n"
    message += f"Preserve Database: {'✅ Yes' if preserve_db else '❌ No'}\n\n"
    message += f"Please send the new Topics channel ID:"

    context.user_data['editing_topics_channel'] = True
    context.user_data['edit_instance_index'] = instance_index

    await update.callback_query.edit_message_text(
        message,
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle input for editing instance parameters"""
    # Check what's being edited
    if context.user_data.get('editing_discord_token'):
        return await handle_discord_token_edit(update, context)
    elif context.user_data.get('editing_telegram_token'):
        return await handle_telegram_token_edit(update, context)
    elif context.user_data.get('editing_topics_channel'):
        return await handle_topics_channel_edit(update, context)

    # If not editing, pass to other handlers
    return ConversationHandler.END

async def handle_discord_token_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Discord token edit"""
    new_token = update.message.text.strip()
    instance_index = context.user_data.get('edit_instance_index')
    preserve_db = context.user_data.get('preserve_db', True)

    instances = instance_manager.list_instances()
    if instance_index is None or instance_index < 0 or instance_index >= len(instances):
        await update.message.reply_text(
            "❌ **Edit Failed**\n\n"
            "Instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data.clear()
        return ConversationHandler.END

    instance = instances[instance_index]
    stack_name = instance['docker_stack_name']
    short_id = stack_name[:8]

    # Show processing message
    await update.message.reply_text(
        f"⚙️ **Updating Discord Token**\n\n"
        f"Instance: `{short_id}...`\n"
        f"Preserve Database: {'✅ Yes' if preserve_db else '❌ No'}\n\n"
        f"Please wait...",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        if preserve_db:
            # Stop container, edit .env, restart
            success = instance_manager.edit_instance_preserve_db(stack_name, 'DISCORD_TOKEN', new_token)
        else:
            # Delete and recreate instance
            success = instance_manager.recreate_instance(instance_index, discord_token=new_token)

        if success:
            # Update data.json
            instances[instance_index]['discord_token'] = new_token
            instance_manager.save_data()

            await update.message.reply_text(
                f"✅ **Discord Token Updated**\n\n"
                f"Instance `{short_id}...` has been updated successfully.\n\n"
                f"Method: {'Preserve Database' if preserve_db else 'Recreate Instance'}",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                f"❌ **Failed to Update Discord Token**\n\n"
                f"Could not update instance `{short_id}...`\n\n"
                f"Please check the logs for more details.",
                parse_mode=ParseMode.MARKDOWN
            )

    except Exception as e:
        await update.message.reply_text(
            f"❌ **Edit Failed**\n\n"
            f"Error: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )

    # Clear edit state
    context.user_data.clear()
    return ConversationHandler.END

async def handle_telegram_token_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Telegram token edit"""
    new_token = update.message.text.strip()
    instance_index = context.user_data.get('edit_instance_index')
    preserve_db = context.user_data.get('preserve_db', True)

    instances = instance_manager.list_instances()
    if instance_index is None or instance_index < 0 or instance_index >= len(instances):
        await update.message.reply_text(
            "❌ **Edit Failed**\n\n"
            "Instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data.clear()
        return ConversationHandler.END

    instance = instances[instance_index]
    stack_name = instance['docker_stack_name']
    short_id = stack_name[:8]

    # Show processing message
    await update.message.reply_text(
        f"⚙️ **Updating Telegram Bot Token**\n\n"
        f"Instance: `{short_id}...`\n"
        f"Preserve Database: {'✅ Yes' if preserve_db else '❌ No'}\n\n"
        f"Please wait...",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        if preserve_db:
            # Stop container, edit .env, restart
            success = instance_manager.edit_instance_preserve_db(stack_name, 'TELEGRAM_BOT_TOKEN', new_token)
        else:
            # Delete and recreate instance
            success = instance_manager.recreate_instance(instance_index, telegram_token=new_token)

        if success:
            # Update data.json
            instances[instance_index]['telegram_token'] = new_token
            instance_manager.save_data()

            await update.message.reply_text(
                f"✅ **Telegram Bot Token Updated**\n\n"
                f"Instance `{short_id}...` has been updated successfully.\n\n"
                f"Method: {'Preserve Database' if preserve_db else 'Recreate Instance'}",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                f"❌ **Failed to Update Telegram Bot Token**\n\n"
                f"Could not update instance `{short_id}...`\n\n"
                f"Please check the logs for more details.",
                parse_mode=ParseMode.MARKDOWN
            )

    except Exception as e:
        await update.message.reply_text(
            f"❌ **Edit Failed**\n\n"
            f"Error: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )

    # Clear edit state
    context.user_data.clear()
    return ConversationHandler.END

async def handle_topics_channel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Topics channel edit"""
    new_channel_id = update.message.text.strip()
    instance_index = context.user_data.get('edit_instance_index')
    preserve_db = context.user_data.get('preserve_db', True)

    # Validate channel ID
    try:
        int(new_channel_id)
    except ValueError:
        await update.message.reply_text(
            "❌ **Invalid Channel ID**\n\n"
            "Please send a valid Topics channel ID (numbers only):",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    instances = instance_manager.list_instances()
    if instance_index is None or instance_index < 0 or instance_index >= len(instances):
        await update.message.reply_text(
            "❌ **Edit Failed**\n\n"
            "Instance no longer exists.",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data.clear()
        return ConversationHandler.END

    instance = instances[instance_index]
    stack_name = instance['docker_stack_name']
    short_id = stack_name[:8]

    # Show processing message
    await update.message.reply_text(
        f"⚙️ **Updating Topics Channel ID**\n\n"
        f"Instance: `{short_id}...`\n"
        f"Preserve Database: {'✅ Yes' if preserve_db else '❌ No'}\n\n"
        f"Please wait...",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        if preserve_db:
            # Stop container, edit .env, restart
            success = instance_manager.edit_instance_preserve_db(stack_name, 'TOPICS_CHANNEL_ID', new_channel_id)
        else:
            # Delete and recreate instance
            success = instance_manager.recreate_instance(instance_index, topics_channel_id=new_channel_id)

        if success:
            # Update data.json
            instances[instance_index]['topics_channel_id'] = new_channel_id
            instance_manager.save_data()

            await update.message.reply_text(
                f"✅ **Topics Channel ID Updated**\n\n"
                f"Instance `{short_id}...` has been updated successfully.\n\n"
                f"New Channel ID: `{new_channel_id}`\n"
                f"Method: {'Preserve Database' if preserve_db else 'Recreate Instance'}",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                f"❌ **Failed to Update Topics Channel ID**\n\n"
                f"Could not update instance `{short_id}...`\n\n"
                f"Please check the logs for more details.",
                parse_mode=ParseMode.MARKDOWN
            )

    except Exception as e:
        await update.message.reply_text(
            f"❌ **Edit Failed**\n\n"
            f"Error: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )

    # Clear edit state
    context.user_data.clear()
    return ConversationHandler.END

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
        [InlineKeyboardButton("⚙️ Manage Instances", callback_data="stop_instance")],
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
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(list_instances|create_instance|stop_instance|help|back_to_menu)$"))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(manage|pause|resume|delete|confirm_delete|details|edit)_\d+$"))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(edit_discord_token_|edit_telegram_token_|edit_topics_channel_|preserve_db_|start_edit_discord_|start_edit_telegram_|start_edit_topics_)\d+$"))
    application.add_handler(CallbackQueryHandler(back_to_menu_callback, pattern="^back_to_menu$"))
    # Add message handler for edit inputs (lower priority)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_input))

    logger.info("TGCrossChat Manager Bot starting...")
    logger.info(f"Authorized username: @{config.telegram_username}")
    logger.info("Bot will only respond to DMs from the authorized user")

    # Start the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
