import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import time
import sqlite3
from gtts import gTTS
from gtts.lang import tts_langs
import tempfile
import platform

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
CLIENT_ID = int(os.getenv("CLIENT_ID"))
GUILD_ID = int(os.getenv("GUILD_ID"))
START_TIME = time.time()

# ── Database ─────────────────────────────────────────────────────────────────
conn = sqlite3.connect("settings.db", check_same_thread=False)
c    = conn.cursor()

c.executescript("""
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id       INTEGER PRIMARY KEY,
    setup_channel  INTEGER,
    prefix         TEXT DEFAULT '!',
    autojoin       INTEGER DEFAULT 0,
    botignore      INTEGER DEFAULT 1,
    xsaid          INTEGER DEFAULT 1,
    server_language TEXT DEFAULT 'en',
    max_time       INTEGER DEFAULT 30,
    repeated_chars INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_settings (
    guild_id INTEGER,
    user_id  INTEGER,
    language TEXT DEFAULT 'en',
    nick     TEXT,
    PRIMARY KEY (guild_id, user_id)
);
""")
conn.commit()

def get_guild(guild_id: int) -> dict:
    c.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        conn.commit()
        c.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))
        row = c.fetchone()
    keys = ["guild_id","setup_channel","prefix","autojoin","botignore",
            "xsaid","server_language","max_time","repeated_chars"]
    return dict(zip(keys, row))

def get_user(guild_id: int, user_id: int) -> dict:
    c.execute("SELECT * FROM user_settings WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = c.fetchone()
    if not row:
        c.execute("INSERT OR IGNORE INTO user_settings (guild_id, user_id) VALUES (?,?)", (guild_id, user_id))
        conn.commit()
        c.execute("SELECT * FROM user_settings WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = c.fetchone()
    keys = ["guild_id","user_id","language","nick"]
    return dict(zip(keys, row))

def set_guild(guild_id: int, **kwargs):
    for key, val in kwargs.items():
        c.execute(f"UPDATE guild_settings SET {key}=? WHERE guild_id=?", (val, guild_id))
    conn.commit()

def set_user(guild_id: int, user_id: int, **kwargs):
    for key, val in kwargs.items():
        c.execute(f"UPDATE user_settings SET {key}=? WHERE guild_id=? AND user_id=?", (val, guild_id, user_id))
    conn.commit()

# ── Bot Setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# TTS queue per guild: {guild_id: asyncio.Queue}
tts_queues: dict[int, asyncio.Queue] = {}
tts_tasks:  dict[int, asyncio.Task]  = {}

# ── TTS helpers ───────────────────────────────────────────────────────────────
async def ensure_queue(guild_id: int):
    if guild_id not in tts_queues:
        tts_queues[guild_id] = asyncio.Queue()
        tts_tasks[guild_id]  = asyncio.create_task(tts_player(guild_id))

async def tts_player(guild_id: int):
    while True:
        item = await tts_queues[guild_id].get()
        vc: discord.VoiceClient = item["vc"]
        text: str               = item["text"]
        lang: str               = item["lang"]
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                path = f.name
            tts = gTTS(text=text, lang=lang)
            tts.save(path)
            if vc and vc.is_connected():
                event = asyncio.Event()
                vc.play(discord.FFmpegPCMAudio(path),
                        after=lambda e: event.set())
                await event.wait()
            os.unlink(path)
        except Exception as e:
            print(f"TTS error: {e}")
        tts_queues[guild_id].task_done()

def apply_repeated_chars(text: str, limit: int) -> str:
    if limit == 0:
        return text
    result = []
    count  = 1
    for i, ch in enumerate(text):
        if i > 0 and ch == text[i-1]:
            count += 1
            if count <= limit:
                result.append(ch)
        else:
            count = 1
            result.append(ch)
    return "".join(result)

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    await tree.sync()
    print(f"✅  Logged in as {bot.user} ({bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    if not message.guild:
        return
    gs = get_guild(message.guild.id)

    # Only process setup channel messages for auto-TTS
    if gs["setup_channel"] and message.channel.id == gs["setup_channel"]:
        if message.author.bot and gs["botignore"]:
            return
        if message.author == bot.user:
            return

        # Check autojoin
        vc = message.guild.voice_client
        if gs["autojoin"] and not vc:
            if message.author.voice and message.author.voice.channel:
                vc = await message.author.voice.channel.connect()

        if not vc or not vc.is_connected():
            return

        us   = get_user(message.guild.id, message.author.id)
        lang = us["language"] or gs["server_language"]
        nick = us["nick"] or message.author.display_name
        text = message.clean_content

        # Apply filters
        rc = gs["repeated_chars"]
        if rc:
            text = apply_repeated_chars(text, rc)

        mt = gs["max_time"]
        # rough char limit: ~14 chars/sec for TTS
        char_limit = mt * 14
        if len(text) > char_limit:
            text = text[:char_limit]

        if gs["xsaid"]:
            text = f"{nick} said {text}"

        await ensure_queue(message.guild.id)
        await tts_queues[message.guild.id].put({"vc": vc, "text": text, "lang": lang})

    await bot.process_commands(message)

# ── Slash Commands ────────────────────────────────────────────────────────────

@tree.command(name="botstats", description="Shows various different stats")
async def botstats(interaction: discord.Interaction):
    uptime_s  = int(time.time() - START_TIME)
    h, rem    = divmod(uptime_s, 3600)
    m, s      = divmod(rem, 60)
    guilds    = len(bot.guilds)
    users     = sum(g.member_count for g in bot.guilds)
    latency   = round(bot.latency * 1000)
    embed = discord.Embed(title="📊 Bot Stats", color=0x5865F2)
    embed.add_field(name="Uptime",   value=f"{h}h {m}m {s}s", inline=True)
    embed.add_field(name="Servers",  value=str(guilds),         inline=True)
    embed.add_field(name="Users",    value=str(users),          inline=True)
    embed.add_field(name="Ping",     value=f"{latency}ms",      inline=True)
    embed.add_field(name="Python",   value=platform.python_version(), inline=True)
    embed.add_field(name="discord.py", value=discord.__version__, inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name="channel", description="Shows the current setup channel")
async def channel(interaction: discord.Interaction):
    gs = get_guild(interaction.guild_id)
    ch = gs["setup_channel"]
    if ch:
        await interaction.response.send_message(f"📢 Setup channel: <#{ch}>")
    else:
        await interaction.response.send_message("❌ No setup channel configured. Use `/setup` first.")


@tree.command(name="ping", description="Gets current ping to Discord")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! `{latency}ms`")


@tree.command(name="uptime", description="Shows how long TTS Bot has been online")
async def uptime(interaction: discord.Interaction):
    uptime_s = int(time.time() - START_TIME)
    h, rem   = divmod(uptime_s, 3600)
    m, s     = divmod(rem, 60)
    await interaction.response.send_message(f"⏱️ Uptime: `{h}h {m}m {s}s`")


@tree.command(name="invite", description="Sends instructions to invite TTS Bot")
async def invite(interaction: discord.Interaction):
    url = f"https://discord.com/oauth2/authorize?client_id={CLIENT_ID}&permissions=3148800&scope=bot%20applications.commands"
    embed = discord.Embed(title="📨 Invite TTS Bot", color=0x57F287)
    embed.description = f"[Click here to invite the bot]({url})\n\nFor support, join our server!"
    await interaction.response.send_message(embed=embed)


@tree.command(name="donate", description="Shows how you can support TTS Bot")
async def donate(interaction: discord.Interaction):
    embed = discord.Embed(title="❤️ Support TTS Bot", color=0xEB459E)
    embed.description = "Hosting costs money! If you enjoy the bot, consider donating.\n\n💳 **Ko-fi:** https://ko-fi.com\n⭐ **Star on GitHub** to show support!"
    await interaction.response.send_message(embed=embed)


@tree.command(name="join", description="Joins the voice channel you're in")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ You need to be in a voice channel first!", ephemeral=True)
    vc = interaction.guild.voice_client
    channel = interaction.user.voice.channel
    if vc:
        await vc.move_to(channel)
    else:
        await channel.connect()
    await interaction.response.send_message(f"✅ Joined **{channel.name}**!")


@tree.command(name="leave", description="Leaves the voice channel")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("❌ I'm not in a voice channel!", ephemeral=True)
    await vc.disconnect()
    await interaction.response.send_message("👋 Left the voice channel.")


@tree.command(name="skip", description="Clears the message queue")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
    q = tts_queues.get(interaction.guild_id)
    if q:
        while not q.empty():
            try:
                q.get_nowait()
                q.task_done()
            except:
                break
    await interaction.response.send_message("⏭️ Skipped and cleared the queue!")


@tree.command(name="tts", description="Generates TTS and sends it in the current text channel")
@app_commands.describe(text="The text to read aloud")
async def tts_cmd(interaction: discord.Interaction, text: str):
    vc = interaction.guild.voice_client
    if not vc:
        if interaction.user.voice:
            vc = await interaction.user.voice.channel.connect()
        else:
            return await interaction.response.send_message("❌ Join a voice channel or use `/join` first!", ephemeral=True)

    us   = get_user(interaction.guild_id, interaction.user.id)
    gs   = get_guild(interaction.guild_id)
    lang = us["language"] or gs["server_language"]

    await ensure_queue(interaction.guild_id)
    await tts_queues[interaction.guild_id].put({"vc": vc, "text": text, "lang": lang})
    await interaction.response.send_message(f"🔊 Queued: *{text[:100]}*")


@tree.command(name="setup", description="Setup the bot to read messages from a channel")
@app_commands.describe(channel="The channel to read messages from")
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need **Manage Server** permission.", ephemeral=True)
    get_guild(interaction.guild_id)
    set_guild(interaction.guild_id, setup_channel=channel.id)
    await interaction.response.send_message(f"✅ Setup complete! I'll read messages from {channel.mention}")


@tree.command(name="settings", description="Displays the current settings")
async def settings(interaction: discord.Interaction):
    gs = get_guild(interaction.guild_id)
    ch = f"<#{gs['setup_channel']}>" if gs["setup_channel"] else "Not set"
    embed = discord.Embed(title="⚙️ Server Settings", color=0xFEE75C)
    embed.add_field(name="Setup Channel",    value=ch,                              inline=False)
    embed.add_field(name="Prefix",           value=gs["prefix"],                    inline=True)
    embed.add_field(name="Auto Join",        value="✅" if gs["autojoin"] else "❌", inline=True)
    embed.add_field(name="Bot Ignore",       value="✅" if gs["botignore"] else "❌",inline=True)
    embed.add_field(name="XSaid",            value="✅" if gs["xsaid"] else "❌",    inline=True)
    embed.add_field(name="Server Language",  value=gs["server_language"],           inline=True)
    embed.add_field(name="Max Read Time",    value=f"{gs['max_time']}s",            inline=True)
    embed.add_field(name="Repeated Chars",   value=str(gs["repeated_chars"]),       inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name="voices", description="Lists all available TTS languages/voices")
async def voices(interaction: discord.Interaction):
    langs = tts_langs()
    lines = [f"`{code}` — {name}" for code, name in list(langs.items())[:40]]
    embed = discord.Embed(title="🌐 Available Languages (first 40)", color=0x5865F2,
                          description="\n".join(lines))
    embed.set_footer(text="Use /set language or /set server_language to change.")
    await interaction.response.send_message(embed=embed)


@tree.command(name="suggest", description="Suggests a new feature")
@app_commands.describe(suggestion="Your feature suggestion")
async def suggest(interaction: discord.Interaction, suggestion: str):
    await interaction.response.send_message(f"💡 Thanks for your suggestion: *{suggestion}*\nWe'll review it!")


# ── /set group ────────────────────────────────────────────────────────────────
set_group = app_commands.Group(name="set", description="Change bot settings")

@set_group.command(name="autojoin", description="Bot joins your VC when you type in the setup channel")
@app_commands.describe(enabled="Enable or disable autojoin")
async def set_autojoin(interaction: discord.Interaction, enabled: bool):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need **Manage Server** permission.", ephemeral=True)
    get_guild(interaction.guild_id)
    set_guild(interaction.guild_id, autojoin=int(enabled))
    await interaction.response.send_message(f"✅ Autojoin {'enabled' if enabled else 'disabled'}.")

@set_group.command(name="botignore", description="Ignore messages from bots and webhooks")
@app_commands.describe(enabled="Enable or disable bot ignore")
async def set_botignore(interaction: discord.Interaction, enabled: bool):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need **Manage Server** permission.", ephemeral=True)
    get_guild(interaction.guild_id)
    set_guild(interaction.guild_id, botignore=int(enabled))
    await interaction.response.send_message(f"✅ Bot ignore {'enabled' if enabled else 'disabled'}.")

@set_group.command(name="language", description="Changes the language your messages are read in")
@app_commands.describe(language="Language code, e.g. en, fr, es, de")
async def set_language(interaction: discord.Interaction, language: str):
    if language not in tts_langs():
        return await interaction.response.send_message(f"❌ Invalid language code. Use `/voices` to see valid codes.", ephemeral=True)
    get_user(interaction.guild_id, interaction.user.id)
    set_user(interaction.guild_id, interaction.user.id, language=language)
    await interaction.response.send_message(f"✅ Your language set to `{language}`.")

@set_group.command(name="max_time_to_read", description="Max seconds for a TTS'd message")
@app_commands.describe(seconds="Maximum seconds (1-120)")
async def set_max_time(interaction: discord.Interaction, seconds: int):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need **Manage Server** permission.", ephemeral=True)
    if not 1 <= seconds <= 120:
        return await interaction.response.send_message("❌ Must be between 1 and 120 seconds.", ephemeral=True)
    get_guild(interaction.guild_id)
    set_guild(interaction.guild_id, max_time=seconds)
    await interaction.response.send_message(f"✅ Max read time set to `{seconds}s`.")

@set_group.command(name="nick", description="Replaces your username in '<user> said' with a given name")
@app_commands.describe(name="Your nickname for TTS (leave empty to reset)")
async def set_nick(interaction: discord.Interaction, name: str = ""):
    get_user(interaction.guild_id, interaction.user.id)
    set_user(interaction.guild_id, interaction.user.id, nick=name or None)
    if name:
        await interaction.response.send_message(f"✅ Your TTS nick set to `{name}`.")
    else:
        await interaction.response.send_message("✅ Your TTS nick has been reset.")

@set_group.command(name="prefix", description="The prefix used before commands")
@app_commands.describe(prefix="New prefix character(s)")
async def set_prefix(interaction: discord.Interaction, prefix: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need **Manage Server** permission.", ephemeral=True)
    get_guild(interaction.guild_id)
    set_guild(interaction.guild_id, prefix=prefix)
    await interaction.response.send_message(f"✅ Prefix set to `{prefix}`.")

@set_group.command(name="repeated_chars", description="Max repetition of a character (0 = off)")
@app_commands.describe(limit="Max repeated chars (0 to disable)")
async def set_repeated_chars(interaction: discord.Interaction, limit: int):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need **Manage Server** permission.", ephemeral=True)
    if limit < 0:
        return await interaction.response.send_message("❌ Must be 0 or higher.", ephemeral=True)
    get_guild(interaction.guild_id)
    set_guild(interaction.guild_id, repeated_chars=limit)
    await interaction.response.send_message(f"✅ Repeated chars limit set to `{limit}` ({'off' if limit==0 else 'on'}).")

@set_group.command(name="server_language", description="Changes the default language messages are read in")
@app_commands.describe(language="Language code, e.g. en, fr, es, de")
async def set_server_language(interaction: discord.Interaction, language: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need **Manage Server** permission.", ephemeral=True)
    if language not in tts_langs():
        return await interaction.response.send_message(f"❌ Invalid language code. Use `/voices` to see valid codes.", ephemeral=True)
    get_guild(interaction.guild_id)
    set_guild(interaction.guild_id, server_language=language)
    await interaction.response.send_message(f"✅ Server language set to `{language}`.")

@set_group.command(name="xsaid", description="Makes the bot say '<user> said' before each message")
@app_commands.describe(enabled="Enable or disable xsaid")
async def set_xsaid(interaction: discord.Interaction, enabled: bool):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need **Manage Server** permission.", ephemeral=True)
    get_guild(interaction.guild_id)
    set_guild(interaction.guild_id, xsaid=int(enabled))
    await interaction.response.send_message(f"✅ XSaid {'enabled' if enabled else 'disabled'}.")

tree.add_command(set_group)

# ── /debug group ──────────────────────────────────────────────────────────────
debug_group = app_commands.Group(name="debug", description="Debug commands")

@debug_group.command(name="info", description="Shows info for debug usage")
async def debug_info(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    gs = get_guild(interaction.guild_id)
    q  = tts_queues.get(interaction.guild_id)
    embed = discord.Embed(title="🔧 Debug Info", color=0xED4245)
    embed.add_field(name="Bot User",       value=str(bot.user),                             inline=False)
    embed.add_field(name="Guild ID",       value=str(interaction.guild_id),                 inline=True)
    embed.add_field(name="Voice Client",   value=str(vc),                                   inline=True)
    embed.add_field(name="Queue Size",     value=str(q.qsize() if q else 0),                inline=True)
    embed.add_field(name="Setup Channel",  value=str(gs["setup_channel"]),                  inline=True)
    embed.add_field(name="Latency",        value=f"{round(bot.latency*1000)}ms",            inline=True)
    embed.add_field(name="discord.py",     value=discord.__version__,                       inline=True)
    await interaction.response.send_message(embed=embed)

@debug_group.command(name="invoke", description="Manually invokes a TTS message for debug")
@app_commands.describe(text="Text to speak", language="Language code (default: en)")
async def debug_invoke(interaction: discord.Interaction, text: str, language: str = "en"):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    vc = interaction.guild.voice_client
    if not vc:
        if interaction.user.voice:
            vc = await interaction.user.voice.channel.connect()
        else:
            return await interaction.response.send_message("❌ Join a VC first.", ephemeral=True)
    await ensure_queue(interaction.guild_id)
    await tts_queues[interaction.guild_id].put({"vc": vc, "text": text, "lang": language})
    await interaction.response.send_message(f"🔧 Debug TTS queued: `{text}` in `{language}`")

tree.add_command(debug_group)

# ── Run ───────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
