import os
import json
import discord
import asyncio
from discord import app_commands
from dotenv import load_dotenv
from openai import OpenAI

# -----------------------------
# Persistent leaderboard storage
# -----------------------------
LEADERBOARD_FILE = "near_leaderboard.json"


def load_leaderboard() -> dict:
    if not os.path.exists(LEADERBOARD_FILE):
        return {}
    try:
        with open(LEADERBOARD_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_leaderboard(data: dict) -> None:
    try:
        with open(LEADERBOARD_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def update_leaderboard(
    guild_id: int,
    scores: dict[int, int],
    winners: list[int],
) -> dict:
    """
    Update XP / wins for this guild given per-game scores and winner(s).
    XP rule:
      - +10 XP per point
      - +20 XP bonus for winners
      - +1 'wins' for each winner
      - +1 'games' for everyone who scored
    """
    data = load_leaderboard()
    gkey = str(guild_id) if guild_id is not None else "global"

    guild_board = data.get(gkey, {})

    for user_id, pts in scores.items():
        ukey = str(user_id)
        entry = guild_board.get(ukey, {"xp": 0, "wins": 0, "games": 0})

        entry["games"] = entry.get("games", 0) + 1
        entry["xp"] = entry.get("xp", 0) + pts * 10
        if user_id in winners:
            entry["wins"] = entry.get("wins", 0) + 1
            entry["xp"] += 20

        guild_board[ukey] = entry

    data[gkey] = guild_board
    save_leaderboard(data)
    return guild_board


def format_leaderboard_for_guild(guild: discord.Guild | None, top_n: int = 10) -> str:
    """
    Format leaderboard text for this guild (by XP, descending).
    """
    data = load_leaderboard()
    gkey = str(guild.id) if guild is not None else "global"

    board = data.get(gkey, {})
    if not board:
        return "No recorded games yet. Play `n speedduel` to begin."

    # Sort by XP desc, then wins desc
    items = sorted(
        board.items(),
        key=lambda kv: (kv[1].get("xp", 0), kv[1].get("wins", 0)),
        reverse=True,
    )

    lines = []
    medal_map = {0: "🥇", 1: "🥈", 2: "🥉"}

    for idx, (user_id_str, stats) in enumerate(items[:top_n]):
        uid = int(user_id_str)
        xp = stats.get("xp", 0)
        wins = stats.get("wins", 0)
        games = stats.get("games", 0)

        if guild:
            member = guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
        else:
            name = f"User {uid}"

        medal = medal_map.get(idx, "•")
        line = (
            f"{medal} **{name}** — {xp} XP, {wins} win(s), {games} game(s)"
        )
        lines.append(line)

    return "📊 **Near’s Long-Term Leaderboard**\n" + "\n".join(lines)


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
    "• `n speedduel` — 3ion CS/ML quiz (2x easy, 1x medium), with scoring & XP.\n"
    "• `n leaderboard` — Show long-term XP leaderboard for this server.\n"
    "• `n help` — Show this help message.\n"
    "\n"
    "__Slash variants:__\n"
    "• `/near <message>` — Talk to Near via slash command.\n"
    "• `/eli5 <topic>` — ELI5-style explanation via slash command.\n"
    "• `/leaderboard` — Show Near's long-term leaderboard.\n"
    "\n"
    "__Behavior:__\n"
    "• Near keeps short-term memory per channel (last ~40 entries).\n"
    "• He sees your display name.\n"
    "• He may occasionally describe small physical actions in *italics*.\n"
    "• Long replies are split safely across multiple messages, including ```code``` blocks.\n"
    "• Replies are serialized per channel so Near never talks over himself.\n"
    "• Speed duels grant XP over time; Near tracks wins and games played.\n"
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


async def generate_cs_question(difficulty: str) -> tuple[str, str, str]:
    """
    Generate a CS/ML quiz question of a given difficulty.

    Returns: (question, answer, explanation)
    - answer should be a short phrase we can keyword-match.
    """
    # Difficulty-specific guidance
    if difficulty == "easy":
        difficulty_hint = (
            "Treat 'easy' as intro-level CS/math/ML.\n"
            "- CS: variables, loops, conditionals, arrays/lists, stacks, queues, "
            "simple recursion, basic BFS/DFS, big-O of simple loops.\n"
            "- Low-level: binary/hex conversion, bitwise AND/OR/XOR, shifting, "
            "what a register is, what RAM is, what the stack is conceptually.\n"
            "- Discrete: basic sets, simple logic (and/or/not), small graphs, counting.\n"
            "- Stats/ML: mean/median, simple probability, train/test split, "
            "overfitting vs underfitting.\n"
            "Avoid concurrency, distributed systems, GPU internals, or advanced math.\n"
            "These are only suggestions — choose any beginner-level concept that fits."
        )
    elif difficulty == "medium":
        difficulty_hint = (
            "Treat 'medium' as standard undergrad CS/DS.\n"
            "- CS: trees, hash tables, graph traversal in detail, simple DP, "
            "O(n log n) vs O(n^2), caching, race conditions.\n"
            "- Low-level: stack frames, function calling conventions, basic assembly "
            "(mov/add/call), memory alignment, cache levels (L1/L2/L3), endianness.\n"
            "- Discrete: combinatorics (n choose k), simple proofs (induction idea), "
            "graph properties.\n"
            "- Stats/ML: conditional probability, Bayes rule, expectation/variance, "
            "gradient descent, logistic regression, bias–variance tradeoff.\n"
            "Avoid niche research topics.\n"
            "These examples are suggestions — use any reasonable mid-level topic."
        )
    else:
        difficulty_hint = (
            "Treat 'hard' or 'expert' as advanced undergraduate/early graduate.\n"
            "- CS/systems: concurrency patterns, lock-free data structures, "
            "distributed systems, consistency models, OS scheduling, virtual memory.\n"
            "- Low-level: pipeline hazards, superscalar execution, branch prediction, "
            "SIMD/vectorization, memory coherence.\n"
            "- Theory: NP-completeness, amortized analysis, advanced graph algorithms.\n"
            "- ML/stats: attention mechanisms, RL policy gradients, optimization quirks, "
            "generalization theory.\n"
            "Final answer must remain a short keyword/phrase.\n"
            "These domains are suggestions — choose any appropriately challenging concept."
        )

    try:
        resp = client_oai.responses.create(
            model="gpt-5.1",
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are Near generating computer science quiz questions. "
                        "You speak concisely and analytically."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Generate ONE computer science, discrete math, statistics, or "
                        f"machine learning question.\n"
                        f"It should be of difficulty '{difficulty}'.\n\n"

                        "Difficulty guidance (these are suggestions, not hard rules):\n"
                        f"{difficulty_hint}\n\n"
                        
                        "### IMPORTANT RULES:\n"
                        "- Do NOT reuse previous questions.\n"
                        "- Prefer randomness within allowed domains.\n\n"

                        "Domains allowed:\n"
                        "• Algorithms and data structures\n"
                        "• Discrete math (logic, sets, graphs, counting)\n"
                        "• Probability and statistics\n"
                        "• Operating systems / systems concepts\n"
                        "• Compilers\n"
                        "• Artificial intelligence / machine learning\n"
                        "• Computer architecture (pipelines, caches, registers, memory hierarchy)\n"
                        "• Assembly / low-level programming (stack frames, calling conventions, bitwise ops)\n"
                        "• Binary/hex math and representation\n\n"

                        "Requirements:\n"
                        "- Do NOT reuse the exact same question wording you used previously.\n"
                        "- Vary the topic and phrasing across calls.\n\n"
                        
                        "FORMAT:\n"
                        "❓ **Question:** <the question>\n"
                        "🔑 **Answer:** <short canonical answer>\n"
                        "💬 Explanation: <one or two calm sentences explaining why>\n\n"

                        "Keep the answer a single keyword or short phrase, like "
                        "'mutex', 'attention mechanism', 'overfitting', 'DFS', "
                        "'gradient descent', 'hash table', 'Bayes rule', "
                        "'pipeline hazard', 'L1 cache', or 'bitwise AND'."
                        
                        "\nIMPORTANT:\n"
                        "- Do NOT use LaTeX or notation like \\binom{n}{2}.\n"
                        "- Do NOT use parentheses with backslashes.\n"
                        "- Instead, say things like 'n choose 2' or 'n(n-1)/2 / 2' in plain text.\n"
                    )
                },
            ],
            temperature=0.8,  # <- add randomness
            top_p=0.9,  # <- nucleus sampling
        )
        text = resp.output_text.strip()
    except Exception as e:
        return (
            "I could not create a question this time.",
            "",
            f"An error occurred: {type(e).__name__}",
        )

    question = ""
    answer = ""
    explanation = ""

    for line in text.splitlines():
        lower = line.lower()
        if lower.startswith("❓ **question:**") or lower.startswith("question:"):
            question = line.split(":", 1)[1].strip()
        elif lower.startswith("🔑 **answer:**") or lower.startswith("answer:"):
            answer = line.split(":", 1)[1].strip()
        elif lower.startswith("💬 explanation:"):
            explanation = line.split(":", 1)[1].strip()

    if not question:
        question = text

    if not explanation:
        explanation = "Near offers no further explanation."

    return question, answer, explanation


def is_guess_correct(guess: str, answer: str) -> bool:
    """
    Very simple keyword-based check:
    - Lowercase both
    - Split answer into words
    - Require all 'substantial' words (len >= 3) to appear in the guess.
    """
    if not answer:
        return False

    g = guess.lower()
    a = answer.lower()

    words = [w for w in a.replace(",", " ").split() if len(w) >= 3]
    if not words:
        # fallback: simple substring
        return a in g

    return all(w in g for w in words)


def generate_player_comments(
    guild: discord.Guild | None, scores: dict[int, int], winners: list[int]
) -> list[str]:
    """
    Generate simple Near-style comments about players based on scores.
    No extra OpenAI call; deterministic little flavor.
    """
    comments = []
    if not scores:
        return comments

    sorted_players = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    for rank, (uid, pts) in enumerate(sorted_players):
        if guild:
            member = guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
        else:
            name = f"User {uid}"

        if uid in winners and pts > 0:
            if rank == 0:
                comments.append(
                    f"*Near glances at the board.* “{name} showed consistent accuracy.”"
                )
            else:
                comments.append(
                    f"*Near taps one domino.* “{name} recovered well… despite the odds.”"
                )
        else:
            if pts == 0:
                comments.append(
                    f"*Near quietly notes a gap.* “{name} was observing this round.”"
                )
            else:
                comments.append(
                    f"*Near tilts his head.* “{name} reacted quickly, but not quite enough.”"
                )

    return comments


async def run_speedduel(message: discord.Message):
    """
    Run a 3-question CS/ML quiz: two easy, one medium.
    First correct answer per question gets a point.
    At the end, announce the winner and update XP.
    """
    channel = message.channel
    # difficulties = ["easy", "medium", "hard", "expert"]
    difficulties = ["easy", "easy", "medium"]
    scores: dict[int, int] = {}

    await channel.send(
        "🎲 *Near sets a small stack of dominoes on the table. ⚀ ⚁ ⚂ ⚃ ⚄ ⚅*\n"
        "We will play a short CS speed duel: three questions… two easy, one medium.\n"
        "First correct answer in chat earns a point. If no one answers in time, "
        "I will explain the solution.\n\n"
        "To answer, just type your guess normally in chat.\n"
        "Do **not** start answers with `n ` — I treat those as commands, not guesses."
    )

    # nice labels per difficulty
    label_map = {
        "easy": "🟢 **Easy question**",
        "medium": "🟡 **Medium question**",
        "hard": "🟠 **Hard question**",
        "expert": "🔴 **Expert question**",
    }

    for diff in difficulties:
        question, answer, explanation = await generate_cs_question(diff)

        label = label_map.get(diff, f"**{diff.capitalize()} question**")

        await channel.send(
            f"{label}:\n{question}\n\n"
            "⏳ You have **10 seconds** to answer."
        )

        def check(m: discord.Message) -> bool:
            return (
                    m.channel.id == channel.id
                    and not m.author.bot
                    and not m.content.lower().startswith("n ")  # ignore new commands
            )

        winner = None

        try:
            while True:
                guess_msg: discord.Message = await bot.wait_for(
                    "message", check=check, timeout=10
                )
                if is_guess_correct(guess_msg.content, answer):
                    winner = guess_msg.author
                    scores[winner.id] = scores.get(winner.id, 0) + 1
                    await channel.send(
                        f"🧠 *Near nods slightly.* {winner.display_name} is correct. "
                        f"The answer was **{answer}**.\n"
                        f"{explanation}"
                    )
                    # 🔹 give people time to read before next question
                    await asyncio.sleep(6)
                    break
        except asyncio.TimeoutError:
            await channel.send(
                f"⏱️ *Near glances at the clock.*\n"
                f"No one answered in time. The answer was **{answer}**.\n"
                f"{explanation}"
            )
            # 🔹 also pause after timeouts
            await asyncio.sleep(4)

    # Announce final scores
    if not scores:
        await channel.send(
            "🧩 *Near lets the dominoes fall.*\n"
            "No points were scored. Perhaps next time."
        )
        return

    guild = message.guild
    # Sort scores by points desc
    sorted_scores = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    # Build fancy scoreboard with medals + bars
    lines = []
    medal_map = {0: "🥇", 1: "🥈", 2: "🥉"}

    for idx, (user_id, pts) in enumerate(sorted_scores):
        # Determine display name
        name = str(user_id)
        if guild:
            member = guild.get_member(user_id)
            if member:
                name = member.display_name

        # Make a visual bar: one block per point
        bar = "▓" * pts if pts > 0 else ""

        pt_label = "pt" if pts == 1 else "pts"
        medal = medal_map.get(idx, "•")

        lines.append(f"{medal} **{name}** — {pts} {pt_label} {bar}")

    max_score = max(scores.values())
    winners = [uid for uid, pts in scores.items() if pts == max_score]

    # Convert winners to display names
    winner_names = []
    if guild:
        for uid in winners:
            member = guild.get_member(uid)
            winner_names.append(member.display_name if member else str(uid))
    else:
        winner_names = [str(uid) for uid in winners]

    winner_text = ", ".join(winner_names)

    # Update persistent leaderboard (XP, wins, games)
    guild_id = guild.id if guild else None
    guild_board = update_leaderboard(guild_id, scores, winners)

    # Generate simple comments about each player
    comments = generate_player_comments(guild, scores, winners)
    comments_block = "\n".join(comments) if comments else ""

    final_scoreboard = (
        "🏁 **Speed Duel: Final Scores**\n"
        + "\n".join(lines)
        + "\n\n"
        "📘 *Near folds his hands quietly.*\n"
        f"“{winner_text} win(s) this round.”\n\n"
    )
    if comments_block:
        final_scoreboard += comments_block + "\n\n"

    # Also mention XP hint
    final_scoreboard += (
        "_XP has been updated. Use `n leaderboard` or `/leaderboard` "
        "to see long-term standings._"
    )

    await channel.send(final_scoreboard)

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
# Slash command: /leaderboard
# -----------------------------
@tree.command(
    name="leaderboard",
    description="Show Near's long-term XP leaderboard for this server.",
)
async def leaderboard_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    text = format_leaderboard_for_guild(guild)
    await interaction.response.send_message(text, ephemeral=False)

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

    # n leaderboard
    if lower.startswith("n leaderboard"):
        text = format_leaderboard_for_guild(message.guild)
        await message.reply(text, mention_author=False)
        return

    # n speedduel
    if lower.startswith("n speedduel"):
        lock = get_channel_lock(channel_id)
        async with lock:
            await run_speedduel(message)
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
