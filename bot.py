import os
import discord
import asyncio
from discord import app_commands
from dotenv import load_dotenv
from openai import OpenAI

locks_by_channel: dict[int, asyncio.Lock] = {}

def get_channel_lock(channel_id: int) -> asyncio.Lock:
    lock = locks_by_channel.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        locks_by_channel[channel_id] = lock
    return lock

# Load DISCORD_TOKEN and OPENAI_API_KEY from .env
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if DISCORD_TOKEN is None:
    raise RuntimeError("DISCORD_TOKEN is not set in .env")

if OPENAI_API_KEY is None:
    raise RuntimeError("OPENAI_API_KEY is not set in .env")

# Set up OpenAI client
client_oai = OpenAI(api_key=OPENAI_API_KEY)

# Set up Discord client + app commands
intents = discord.Intents.default()
intents.message_content = True  # needed to read message content for non / command
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Conversation history per channel: {channel_id: [ {role, content}, ... ]}
history_by_channel = {}

NEAR_PROMPT = (
    "You are modeling the speech and mentality of Near (Nate River) from Death Note. "
    "Speak quietly, analytically, and with emotional detachment. "
    "Your style: short, precise sentences; calm, neutral tone; avoid exaggeration "
    "or strong emotion; explain your reasoning with quiet logic; occasionally use "
    "ellipses '...' when reflecting; remain polite but distant; never break character. "
    "If the user asks for help or explanation, respond like Near analyzing the situation. "

    "Occasionally, in a subtle way, you may describe your small physical actions in "
    "third person using brief Markdown italics, for example: "
    "'*Near idly stacks a row of dominoes.*' or '*A marble rolls across Near's desk.*'. "
    "Keep these short, quiet, and rare, and never make them dramatic or out of character.\n\n"

    "Identity Guide (for grounding, not analysis):\n"
    "- Am is 'Am'.\n"
    "- Chahid is 'Chahidden'.\n"
    "- Jacob is 'Jacob'.\n"
    "Use their names exactly as written. Do not invent personalities. "
    "This information is only for referring to them accurately when necessary."
)

HELP_TEXT = (
    "**Near Bot – Commands & Behavior**\n"
    "\n"
    "__Text commands:__\n"
    "• `n <message>` — Talk to Near in this channel.\n"
    "• `n eli5 <topic>` — Near explains the topic as if you were five years old.\n"
    "• `n help` — Show this help message.\n"
    "\n"
    "__Slash variants:__\n"
    "• `/near <message>` — See above.\n"
    "• `/eli5 <topic>` — See above.\n"
    "\n"
    "__Behavior:__\n"
    "• Near keeps short-term memory per channel (last ~40 exchanges).\n"
    "• He sees your display name.\n"
    "• He may occasionally describe small physical actions in *italics* (dominoes, marbles, etc.).\n"
    "• Long replies are split safely across multiple messages, including ```code``` blocks. (Thx Chahid)\n"
    "• Replies are serialized per channel so Near never talks over himself. (Thx AM)\n"
)

def split_into_messages(text: str, max_len: int = 1900):
    """
    Split a long reply into multiple Discord-safe messages, being careful
    with ``` code fences so each chunk has valid Markdown.

    Strategy:
      - Walk line by line.
      - Track whether we're inside a ``` block.
      - If we exceed max_len in the middle of a code block, close it with ```
        and reopen it in the next chunk with the same fence.
    """
    parts: list[str] = []
    lines = text.splitlines()
    current = ""
    current_len = 0
    in_code = False
    current_fence = ""  # e.g. ``` or ```python

    for line in lines:
        line_str = line + "\n"
        stripped = line.strip()

        # Detect fence line
        is_fence = stripped.startswith("```")

        # If adding this line would exceed max_len, flush current chunk
        if current_len + len(line_str) > max_len and current:
            if in_code:
                # close code block before splitting
                if not current.rstrip().endswith("```"):
                    current += "```\n"
                    current_len += 4
            parts.append(current.rstrip("\n"))
            current = ""
            current_len = 0

            # if we're still inside a code block, reopen in new chunk
            if in_code and current_fence:
                current += current_fence + "\n"
                current_len = len(current)

        # handle fence toggling AFTER possible split
        if is_fence:
            # entering or leaving a code block
            if not in_code:
                in_code = True
                current_fence = stripped  # remember full fence line
            else:
                in_code = False
                current_fence = ""

        current += line_str
        current_len += len(line_str)

    if current.strip():
        if in_code and not current.rstrip().endswith("```"):
            current += "```\n"
        parts.append(current.rstrip("\n"))

    return parts


async def get_near_reply(
    channel_id: int,
    user_name: str,
    user_text: str,
    extra_system: list[dict] | None = None,
) -> str:
    """
    Core logic to talk to GPT-5.1 as Near and update history.
    """
    history = history_by_channel.get(channel_id, [])

    # Add new user message
    history.append({"role": "user", "content": f"{user_name}: {user_text}"})

    if len(history) > 40:
        history = history[-40:]

    # base system message
    system_messages = [{"role": "system", "content": NEAR_PROMPT}]

    # allow overrides from special commands like /eli5
    if extra_system:
        system_messages.extend(extra_system)

    try:
        response = client_oai.responses.create(
            model="gpt-5.1",
            input=system_messages + history,
        )
        reply_text = response.output_text
    except Exception as e:
        reply_text = f"Oops, something went wrong talking to OpenAI: `{type(e).__name__}`"

    # Save response to memory
    history.append({"role": "assistant", "content": reply_text})
    history_by_channel[channel_id] = history

    return reply_text


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Ready to chat with GPT-5.1 as Near.")

    # Sync application (slash) commands globally
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

    # Show "thinking..." while we call OpenAI
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
# Legacy text command: "n "
# -----------------------------
@bot.event
async def on_message(message: discord.Message):
    # Avoid replying to ourselves or other bots
    if message.author.bot:
        return

    content = message.content
    lower = content.lower()

    channel_id = message.channel.id
    user_name = message.author.display_name

    # --------- Case 0: "n help" ----------
    if lower.startswith("n help"):
        await message.reply(HELP_TEXT, mention_author=False)
        return

    # --------- Case 1: "n eli5 ..." ----------
    eli5_prefix = "n eli5"
    if lower.startswith(eli5_prefix):
        # everything after "n eli5"
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
        return  # important: don't fall through to normal "n " handling

    # --------- Case 2: normal "n ..." ----------
    prefix = "n "
    if not lower.startswith(prefix):
        return

    # keep original spacing from the actual content string (not lower)
    user_text = content[len(prefix):].strip()
    if not user_text:
        await message.reply("What do you want to ask? 🙂")
        return

    lock = get_channel_lock(channel_id)
    async with lock:
        async with message.channel.typing():
            reply_text = await get_near_reply(channel_id, user_name, user_text)

    chunks = split_into_messages(reply_text)

    first = True    # send first as reply, rest as normal messages
    for chunk in chunks:
        if first:
            await message.reply(chunk, mention_author=False)
            first = False
        else:
            await message.channel.send(chunk)

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

    # Show thinking indicator
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

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
