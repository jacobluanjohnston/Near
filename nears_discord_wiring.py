# near_bot.py
import os
import discord
from discord import app_commands
from dotenv import load_dotenv

import nears_brain
from nears_brain import (
    get_channel_lock,
    add_message_to_history,
    split_into_messages,
    generate_riddle_text,
    get_near_reply,
)

# -----------------------------
# Help text (Discord-facing)
# -----------------------------
HELP_TEXT = (
    "**Near Bot – Commands**\n"
    "\n"
    "__Text commands:__\n"
    "• `n <message>` — Talk to Near in this channel.\n"
    "• `n eli5 <topic>` — Near explains the topic as if you were five years old.\n"
    "• `n riddle` — Near gives a cryptic CS/AI riddle (answer in spoilers).\n"
    "• `n help` — Show this help message.\n"
    "\n"
    "__Slash variants:__\n"
    "• `/near <message>` — Talk to Near via slash command.\n"
    "• `/eli5 <topic>` — ELI5-style explanation via slash command."
)

# -----------------------------
# Discord / env setup
# -----------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if DISCORD_TOKEN is None:
    raise RuntimeError("DISCORD_TOKEN is not set in .env")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Ready to chat with GPT-5.1 as Near.")

    try:
        await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


# -----------------------------
# Slash command: /near
# -----------------------------
@tree.command(
    name="near",
    description="Talk to Near (GPT-5.1) about anything.",
)
@app_commands.describe(prompt="What do you want to say to Near?")
async def near_cmd(interaction: discord.Interaction, prompt: str):
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message(
            "I can't see a channel context for this interaction.", ephemeral=True
        )
        return

    channel_id = channel.id
    user_name = interaction.user.display_name

    # add to history as context
    add_message_to_history(channel_id, user_name, f"/near {prompt}")

    await interaction.response.defer(thinking=True)

    lock = get_channel_lock(channel_id)
    async with lock:
        reply_text = await get_near_reply(channel_id, user_name, prompt)

    chunks = split_into_messages(reply_text)
    first = True
    for chunk in chunks:
        if first:
            await interaction.followup.send(chunk)
            first = False
        else:
            await interaction.followup.send(chunk)


# -----------------------------
# Slash command: /eli5
# -----------------------------
@tree.command(
    name="eli5",
    description="Ask Near to explain something as if you were five years old.",
)
@app_commands.describe(prompt="What do you want Near to explain simply?")
async def eli5_cmd(interaction: discord.Interaction, prompt: str):
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message(
            "I can't see a channel context for this interaction.", ephemeral=True
        )
        return

    channel_id = channel.id
    user_name = interaction.user.display_name

    add_message_to_history(channel_id, user_name, f"/eli5 {prompt}")

    await interaction.response.defer(thinking=True)

    lock = get_channel_lock(channel_id)
    async with lock:
        extra_system = [
            {
                "role": "system",
                "content": (
                    "For this reply only, explain the topic as if you were "
                    "speaking to a five-year-old child. "
                    "Use very simple words, short sentences, gentle tone, and "
                    "tiny analogies. Maintain Near's quiet, calm personality, "
                    "but simplify everything drastically."
                ),
            }
        ]
        reply_text = await get_near_reply(
            channel_id,
            user_name,
            prompt,
            extra_system=extra_system,
        )

    chunks = split_into_messages(reply_text)
    first = True
    for chunk in chunks:
        if first:
            await interaction.followup.send(chunk)
            first = False
        else:
            await interaction.followup.send(chunk)


# -----------------------------
# Legacy text commands: n ...
# -----------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content
    lower = content.lower()
    channel_id = message.channel.id
    user_name = message.author.display_name

    # record everything as context
    add_message_to_history(channel_id, user_name, content)

    # n help
    if lower.startswith("n help"):
        await message.reply(HELP_TEXT, mention_author=False)
        return

    # n riddle
    if lower.startswith("n riddle"):
        riddle_text = await generate_riddle_text()
        await message.reply(riddle_text, mention_author=False)

        # 🔹 add riddle to history so Near can reference it later
        history = nears_brain.history_by_channel.get(channel_id, [])
        history.append({"role": "assistant", "content": riddle_text})
        if len(history) > 40:
            history = history[-40:]
        nears_brain.history_by_channel[channel_id] = history

        return

    # n eli5 ...
    eli5_prefix = "n eli5"
    if lower.startswith(eli5_prefix):
        user_text = content[len(eli5_prefix):].strip(" ,:-").strip()

        if not user_text:
            await message.reply("What do you want me to explain simply? 🙂")
            return

        extra_system = [
            {
                "role": "system",
                "content": (
                    "For this reply only, explain the topic as if you were "
                    "speaking to a five-year-old child. "
                    "Use very simple words, short sentences, gentle tone, and "
                    "tiny analogies. Maintain Near's quiet, calm personality, "
                    "but simplify everything drastically."
                ),
            }
        ]

        lock = get_channel_lock(channel_id)
        async with lock:
            async with message.channel.typing():
                reply_text = await get_near_reply(
                    channel_id,
                    user_name,
                    user_text,
                    extra_system=extra_system,
                )

        chunks = split_into_messages(reply_text)
        first = True
        for chunk in chunks:
            if first:
                await message.reply(chunk, mention_author=False)
                first = False
            else:
                await message.channel.send(chunk)
        return

    # plain n ...
    prefix = "n "
    if not lower.startswith(prefix):
        return

    user_text = content[len(prefix):].strip()
    if not user_text:
        await message.reply("What do you want to ask? 🙂")
        return

    lock = get_channel_lock(channel_id)
    async with lock:
        async with message.channel.typing():
            reply_text = await get_near_reply(channel_id, user_name, user_text)

    chunks = split_into_messages(reply_text)
    first = True
    for chunk in chunks:
        if first:
            await message.reply(chunk, mention_author=False)
            first = False
        else:
            await message.channel.send(chunk)


# -----------------------------
# Run bot
# -----------------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
