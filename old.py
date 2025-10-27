import logging
import requests
import discord

import threading

from telegram import ForceReply, Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

webhook = "https://discord.com/api/webhooks/"

application: Application = Application
bot = discord.Client(intents=discord.Intents.all())
channelToWebhook = {
    1: "1"
}

webhookIds = [int(i.split("/")[0]) for i in channelToWebhook.values()]

tgToDc = {
    -1: 1
}
dcToTg = {}

for k, v in tgToDc.items():
    dcToTg[v] = k


replace = {
    "ur": "your",
    "urr": "you are",
    "ru": "are you",
    "u": "you",
    "r": "are",
    "y": "why",
    "k": "ok"
}

separators = list(" ?.!,")

def sumOfList(x):
    out = []
    for i in x:
        out += i
    return out

def separate(text,separators):
    out = [text]
    for separator in separators:
        tmp = []
        for i in out:
            tmp2=[]
            x = i.split(separator)
            for y in x:
                tmp2.append(y)
                tmp2.append(separator)
            tmp2.pop()
            tmp.append(tmp2)
        out = sumOfList(tmp)
    return out

def allInclude(txt, include):
    for i in txt:
        if not i in include:
            return False
    return True

def formatStr(string,toFormat):
    lower = "abcdefghijklmnopqrstuvwxyz"
    upper = lower.upper()
    if string[0] in upper and allInclude(string[1:],lower):
        return toFormat.title()
    if allInclude(string,upper):
        return toFormat.upper()
    return toFormat


def splitReplace(separators,replaceFrom,replaceTo,text):
    out = []
    text = separate(text, separators)
    for i in text:
        if i.lower() == replaceFrom:
            out.append(formatStr(i,replaceTo))
        else:
            out.append(i)
    return "".join(out)

def removeSlash(text):
    out = []
    text = separate(text, separators)
    for i in text:
        if i.startswith("\\"):
            out.append(i[1:])
        else:
            out.append(i)
    return "".join(out)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")


@bot.event
async def on_message(message: discord.Message):
    tgbot: Bot = application.bot
    if message.author == bot.user:
        return
    if message.webhook_id in webhookIds:
        return
    if message.channel.id in tgToDc.values():
        if message.reference:
            reference = await message.channel.fetch_message(message.reference.message_id)
            replyingTo = f"replying to {reference.author.display_name}(@{reference.author.name}): \"{reference.content[:50]}\":\n"
            if reference.webhook_id:
                replyingTo = f"replying to {reference.author.display_name}: \"{reference.content[:50]}\":\n"
            msg = await tgbot.send_message(
                chat_id=dcToTg[message.channel.id],
                text=f"{message.author.display_name}(@{message.author.name}):\n{replyingTo}{message.content}"
            )
        else:
            msg = await tgbot.send_message(
                chat_id=dcToTg[message.channel.id],
                text=f"{message.author.display_name}(@{message.author.name}):\n{message.content}"
            )
        for attachment in message.attachments:
            print(attachment)
            print(attachment.content_type)
            print(attachment.url)
            if attachment.content_type == None:
                await msg.reply_text(
                    attachment.url
                )
            elif attachment.content_type.startswith("image/"):
                await msg.reply_photo(
                    attachment.url
                )
            else:
                await msg.reply_document(
                    attachment.url
                )


# Define a few command handlers. These usually take the two arguments update and
# context.
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}!",
        reply_markup=ForceReply(selective=True),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text("Help!")


async def updateData(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(update.effective_chat.id)


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id in tgToDc.keys():
        try:
            avatar_url = (
                await (
                    await update.get_bot().get_user_profile_photos(
                        update.effective_user.id, limit=1
                    )
                )
                .photos[0][0]
                .get_file()
            ).file_path
        except Exception as e:
            print(f"Failed to get avatar: {e}")
            avatar_url = "https://discord.com/assets/411d8a698dd15ddf.png"
        print(update.message.document)
        messageContent = update.message.text
        if update.effective_user.username == "kobosh_com":
            messageContent = messageContent.replace("‘", "'").replace("’", "'")
            for k,v in replace.items():
                messageContent = splitReplace(separators, k, v, messageContent)
            messageContent = removeSlash(messageContent)
        # Send the message to the Discord channel
        data = {
            "content": messageContent,
            "username": f"{update.effective_user.full_name}(@{update.effective_user.username})",
            "avatar_url": avatar_url,
        }
        response = requests.post(
            webhook + channelToWebhook[tgToDc[update.effective_chat.id]], json=data
        )
        if response.status_code != 204 and response.status_code != 200:
            print(f"Failed to send message: {response.status_code}")

async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id in tgToDc.keys():
        try:
            avatar_url = (
                await (
                    await update.get_bot().get_user_profile_photos(
                        update.effective_user.id, limit=1
                    )
                )
                .photos[0][0]
                .get_file()
            ).file_path
        except Exception as e:
            print(f"Failed to get avatar: {e}")
            avatar_url = "https://discord.com/assets/411d8a698dd15ddf.png"
        print(update.message.reply_to_message)
        messageContent = update.message.text
        if update.effective_user.username == "kobosh_com":
            messageContent = messageContent.replace("‘", "'").replace("’", "'")
            for k,v in replace.items():
                messageContent = splitReplace(separators, k, v, messageContent)
            messageContent = removeSlash(messageContent)
        # Send the message to the Discord channel
        replyingTo = f"replying to {update.message.reply_to_message.from_user.full_name}(@{update.message.reply_to_message.from_user.username}): \"{update.message.reply_to_message.text[:50]}\""
        if update.message.reply_to_message.from_user.username == "kobosh_bot":
            replyingTo = f"replying to {update.message.reply_to_message.text.split(': ')[0].split('replying to ')[-1]}: \"{''.join(update.message.reply_to_message.text.split(': ')[1:])[:50]}\""
        data = {
            "content": f"{replyingTo}: \n{messageContent}",
            "username": f"{update.effective_user.full_name}(@{update.effective_user.username})",
            "avatar_url": avatar_url,
        }
        response = requests.post(
            webhook + channelToWebhook[tgToDc[update.effective_chat.id]], json=data
        )
        if response.status_code != 204 and response.status_code != 200:
            print(f"Failed to send message: {response.status_code}")

async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id in tgToDc.keys():
        try:
            avatar_url = (
                await (
                    await update.get_bot().get_user_profile_photos(
                        update.effective_user.id, limit=1
                    )
                )
                .photos[0][0]
                .get_file()
            ).file_path
        except Exception as e:
            print(f"Failed to get avatar: {e}")
            avatar_url = "https://discord.com/assets/411d8a698dd15ddf.png"
        messageContent = (update.message.text or "")
        if update.effective_user.username == "kobosh_com":
            messageContent = messageContent.replace("‘", "'").replace("’", "'")
            for k,v in replace.items():
                messageContent = splitReplace(separators, k, v, messageContent)
            messageContent = removeSlash(messageContent)
        messageContent += "\n" + (await update.message.photo[-1].get_file()).file_path
        # Send the message to the Discord channel
        data = {
            "content": messageContent,
            "username": f"{update.effective_user.full_name}(@{update.effective_user.username})",
            "avatar_url": avatar_url
        }
        response = requests.post(
            webhook + channelToWebhook[tgToDc[update.effective_chat.id]], json=data
        )
        if response.status_code != 204 and response.status_code != 200:
            print(f"Failed to send message: {response.status_code}")

async def replyPhoto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id in tgToDc.keys():
        try:
            avatar_url = (
                await (
                    await update.get_bot().get_user_profile_photos(
                        update.effective_user.id, limit=1
                    )
                )
                .photos[0][0]
                .get_file()
            ).file_path
        except Exception as e:
            print(f"Failed to get avatar: {e}")
            avatar_url = "https://discord.com/assets/411d8a698dd15ddf.png"
        messageContent = (update.message.text or "")
        if update.effective_user.username == "kobosh_com":
            messageContent = messageContent.replace("‘", "'").replace("’", "'")
            for k,v in replace.items():
                messageContent = splitReplace(separators, k, v, messageContent)
            messageContent = removeSlash(messageContent)
        messageContent += "\n" + (await update.message.photo[-1].get_file()).file_path
        replyingTo = f"replying to {update.message.reply_to_message.from_user.full_name}(@{update.message.reply_to_message.from_user.username}): \"{update.message.reply_to_message.text[:50]}\""
        if update.message.reply_to_message.from_user.username == "kobosh_bot":
            replyingTo = f"replying to {update.message.reply_to_message.text.split(': ')[0]}: \"{''.join(update.message.reply_to_message.text.split(': ')[1:])[:50]}\""
        data = {
            "content": f"{replyingTo}: \n{messageContent}",
            "username": f"{update.effective_user.full_name}(@{update.effective_user.username})",
            "avatar_url": avatar_url,
        }
        response = requests.post(
            webhook + channelToWebhook[tgToDc[update.effective_chat.id]], json=data
        )
        if response.status_code != 204 and response.status_code != 200:
            print(f"Failed to send message: {response.status_code}")
def main() -> None:
    global application
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = (
        Application.builder()
        .token("<TOKEN>")
        .build()
    )

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("data", updateData))
    # on non command i.e message - echo the message on Telegram
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.PHOTO & ~filters.REPLY, echo))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND & ~filters.REPLY, photo))
    application.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND & ~filters.PHOTO, reply))
    application.add_handler(MessageHandler(filters.REPLY & filters.PHOTO & ~filters.COMMAND, replyPhoto))
    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    threading.Thread(target=bot.run, args=("<TOKEN>",)).start()
    main()