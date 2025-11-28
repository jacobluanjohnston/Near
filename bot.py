import os
import discord
import asyncio
from discord import app_commands
from dotenv import load_dotenv
from openai import OpenAI

# -----------------------------
# Locks per channel (no overlap)
# -----------------------------
locks_by_channel: dict[int, asyncio.Lock] = {}
history_by_channel = {}  # {channel_id: [ {role, content}, ... ]}


def get_channel_lock(channel_id: int) -> asyncio.Lock:
    lock = locks_by_channel.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        locks_by_channel[channel_id] = lock
    return lock


def add_message_to_history(channel_id: int, user_name: str, text: str):
    """
    Record any message in the channel as contextual history.

    We store these as 'system' messages with a [Context] prefix so Near
    understands they are background conversation, not direct instructions.
    Near is allowed to ignore irrelevant context.
    """
    history = history_by_channel.get(channel_id, [])
    history.append(
        {
            "role": "system",
            "content": f"[Context] {user_name} said: {text}",
        }
    )

    # keep last 40 entries
    if len(history) > 40:
        history = history[-40:]

    history_by_channel[channel_id] = history


# -----------------------------
# Environment / OpenAI / Discord
# -----------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if DISCORD_TOKEN is None:
    raise RuntimeError("DISCORD_TOKEN is not set in .env")

if OPENAI_API_KEY is None:
    raise RuntimeError("OPENAI_API_KEY is not set in .env")

client_oai = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -----------------------------
# Prompts / Help text
# -----------------------------
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

    "You will sometimes see prior channel messages as '[Context] <name> said: ...'. "
    "These are background conversation only. Use them if they help your analysis, "
    "but you are free to ignore any context that seems irrelevant.\n\n"

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
    "• `n riddle` — Near gives a cryptic CS/AI riddle (answer in spoilers).\n"
    "• `n help` — Show this help message.\n"
    "\n"
    "__Slash variants:__\n"
    "• `/near <message>` — Talk to Near via slash command.\n"
    "• `/eli5 <topic>` — ELI5-style explanation via slash command.\n"
    "\n"
    "__Behavior:__\n"
    "• Near keeps short-term memory per channel (last ~40 entries).\n"
    "• He sees your display name.\n"
    "• He may occasionally describe small physical actions in *italics*.\n"
    "• Long replies are split safely across multiple messages, including ```code``` blocks.\n"
    "• Replies are serialized per channel so Near never talks over himself.\n"
)

# -----------------------------
# Message splitting (code-aware)
# -----------------------------
def split_into_messages(text: str, max_len: int = 1900):
    """
    Split a long reply into multiple Discord-safe messages, being careful
    with ``` code fences so each chunk has valid Markdown.
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

# -----------------------------
# Game helpers: riddle & quiz
# -----------------------------
async def generate_riddle_text() -> str:
    """
    Ask GPT to generate a single cryptic CS/ML/AI riddle with answer hidden
    in spoiler tags.
    """
    try:
        resp = client_oai.responses.create(
            model="gpt-5.1",
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are Near creating short, cryptic riddles about "
                        "computer science or mathematics or artificial intelligence. "
                        "You speak quietly, analytically, and with emotional detachment."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Create ONE short riddle about a computer science, machine learning, or "
                        "artificial intelligence concept.\n"
                        "Format it like this:\n"
                        "🧩 **Riddle:** <your riddle>\n\n"
                        "Then write:\n"
                        "||<short answer>||\n"
                        "No explanation unless asked.\n"
                        "Use a quiet, analytical Near-like tone with occasional subtle italics."
                    ),
                },
            ],
        )
        return resp.output_text.strip()
    except Exception as e:
        return f"Oops… I could not create a riddle this time. `{type(e).__name__}`"

# -----------------------------
# Core Near call
# -----------------------------
async def get_near_reply(
    channel_id: int,
    user_name: str,
    user_text: str,
    extra_system: list[dict] | None = None,
) -> str:
    """
    Core logic to talk to GPT-5.1 as Near and update history.
    Uses stored context plus the current user message.
    """
    history = history_by_channel.get(channel_id, [])
    if len(history) > 40:
        history = history[-40:]

    # base system message
    system_messages = [{"role": "system", "content": NEAR_PROMPT}]

    # allow overrides from special commands like /eli5
    if extra_system:
        system_messages.extend(extra_system)

    # current message as explicit user turn
    user_turn = {"role": "user", "content": f"{user_name}: {user_text}"}

    try:
        response = client_oai.responses.create(
            model="gpt-5.1",
            input=system_messages + history + [user_turn],
        )
        reply_text = response.output_text

        # --- cost calculation footer ---
        usage = getattr(response, "usage", None)
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", 0)
            output_tokens = getattr(usage, "output_tokens", 0)

            # Pricing:
            #  - input:  $1.25 per 1M tokens
            #  - output: $10.00 per 1M tokens
            input_cost = (input_tokens / 1_000_000) * 1.25
            output_cost = (output_tokens / 1_000_000) * 10.0
            total_cost = input_cost + output_cost

            cost_footer = (
                f"\n\n_(approx cost this reply: "
                f"${total_cost:.5f} — input {input_tokens} tok, "
                f"output {output_tokens} tok)_"
            )
            reply_text = reply_text + cost_footer

    except Exception as e:
        reply_text = f"Oops, something went wrong talking to OpenAI: `{type(e).__name__}`"

    # Save Near's reply as assistant message in history
    history.append({"role": "assistant", "content": reply_text})
    history_by_channel[channel_id] = history

    return reply_text

# -----------------------------
# Events
# -----------------------------
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
        channel_id = message.channel.id
        history = history_by_channel.get(channel_id, [])
        history.append({"role": "assistant", "content": riddle_text})
        if len(history) > 40:
            history = history[-40:]
        history_by_channel[channel_id] = history

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
