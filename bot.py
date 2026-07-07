"""
copilot-telegram-bridge
------------------------
A tiny, security-first Telegram bridge to the official GitHub Copilot CLI.

Send a message (or /command) to your private bot and the real `copilot` agent
runs on YOUR machine, in a directory you choose, and replies in the chat.

Design goals:
  * Single-user: the bot answers only ONE whitelisted Telegram user id.
  * Sandboxed: Copilot runs with `-C <WORKING_DIR>` so it stays in one project.
  * Guard-railed: a denylist of destructive commands is always enforced.
  * Robust launch: the CLI is invoked via `node <cli.js>` so arbitrary prompt
    text (quotes, &&, %, pipes) is never re-parsed by a Windows .cmd/.bat shim.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, BaseMiddleware, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("copilot-bridge")

load_dotenv()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = _get_int("ALLOWED_USER_ID", 0)
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "").strip()
ALLOW_ALL_TOOLS = _get_bool("ALLOW_ALL_TOOLS", True)
CONTINUE_SESSION = _get_bool("CONTINUE_SESSION", True)
REQUEST_TIMEOUT = _get_int("REQUEST_TIMEOUT", 900)
ENABLE_VOICE = _get_bool("ENABLE_VOICE", True)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small").strip() or "small"
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "").strip()

# Telegram gets a short human summary while the agent still does the full work.
SUMMARY_MODE = _get_bool("SUMMARY_MODE", True)
DEFAULT_SUMMARY_INSTRUCTION = (
    "You are answering the user on their PHONE via Telegram while they are away "
    "from the computer. Do the FULL task with your tools (edit files, run "
    "commands, use git). Then reply with a SHORT, warm, casual message in the "
    "user's language \u2014 at most 3-4 sentences, no code blocks and no long file "
    "or diff dumps. Say what you did and where; the full diff stays on the "
    "machine for review in the editor. If you genuinely need a decision, ask one "
    "short question instead."
)
SUMMARY_INSTRUCTION = (
    os.getenv("SUMMARY_INSTRUCTION", "").strip() or DEFAULT_SUMMARY_INSTRUCTION
)

_DEFAULT_DENY = (
    "shell(rm),shell(rmdir),shell(rd),shell(del),shell(Remove-Item),"
    "shell(format),shell(git push),shell(sudo),shell(shutdown),shell(reboot)"
)
DENY_TOOLS = [
    t.strip()
    for t in os.getenv("COPILOT_DENY_TOOLS", _DEFAULT_DENY).split(",")
    if t.strip()
]

# Working directory = Copilot's sandbox. Defaults to ./workspace next to this file.
_wd = os.getenv("WORKING_DIR", "").strip()
WORKING_DIR = Path(_wd) if _wd else (Path(__file__).parent / "workspace")
WORKING_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_LIMIT = 4000  # keep below Telegram's 4096 hard cap


# --------------------------------------------------------------------------- #
# Resolve how to launch the Copilot CLI
# --------------------------------------------------------------------------- #
def _resolve_copilot_argv() -> list[str]:
    """
    Prefer `node <npm-global>/@github/copilot/<bin>.js` so the prompt is passed
    as a real argv entry (node honours it verbatim). Fall back to the PATH shim.
    """
    try:
        npm = shutil.which("npm")
        if npm:
            root = subprocess.run(
                [npm, "root", "-g"], capture_output=True, text=True, timeout=30
            )
            if root.returncode == 0:
                pkg_dir = Path(root.stdout.strip()) / "@github" / "copilot"
                pkg_json = pkg_dir / "package.json"
                if pkg_json.is_file():
                    data = json.loads(pkg_json.read_text(encoding="utf-8"))
                    bin_field = data.get("bin")
                    rel = (
                        bin_field
                        if isinstance(bin_field, str)
                        else (bin_field or {}).get("copilot")
                    )
                    if rel:
                        js = pkg_dir / rel
                        node = shutil.which("node")
                        if js.is_file() and node:
                            log.info("Launching Copilot via node: %s", js)
                            return [node, str(js)]
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("node resolution failed (%s); falling back to PATH shim", exc)

    shim = shutil.which("copilot") or "copilot"
    log.info("Launching Copilot via PATH shim: %s", shim)
    return [shim]


COPILOT_ARGV = _resolve_copilot_argv()


_session_started = False
summary_enabled = SUMMARY_MODE
active_workdir = WORKING_DIR
active_resume_id: str | None = None


def build_command(prompt: str) -> list[str]:
    args = list(COPILOT_ARGV) + ["-p", prompt, "--silent"]
    if COPILOT_MODEL:
        args += ["--model", COPILOT_MODEL]
    if ALLOW_ALL_TOOLS:
        args += ["--allow-all-tools", "--allow-all-paths"]
    for tool in DENY_TOOLS:
        args += ["--deny-tool", tool]
    if active_resume_id:
        args += ["--resume", active_resume_id]
    elif CONTINUE_SESSION and _session_started:
        args.append("--continue")
    args += ["-C", str(active_workdir)]
    return args


async def run_copilot(prompt: str) -> str:
    global _session_started
    log.info(
        "copilot -p %r (ws=%s, resume=%s, summary=%s)",
        prompt[:60], active_workdir.name, active_resume_id, summary_enabled,
    )
    if summary_enabled:
        prompt = f"{prompt}\n\n---\n{SUMMARY_INSTRUCTION}"
    args = build_command(prompt)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(active_workdir),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"\u23f1 Timed out after {REQUEST_TIMEOUT}s."

    if proc.returncode == 0:
        _session_started = True
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return "(empty response)"
    if "No authentication information found" in text:
        text += (
            "\n\n\u26a0 The Copilot CLI is not logged in. Run `copilot` in a "
            "terminal, then `/login`, and try again."
        )
    return text


# --------------------------------------------------------------------------- #
# Telegram plumbing
# --------------------------------------------------------------------------- #
class AuthMiddleware(BaseMiddleware):
    """Silently drop every update that is not from the whitelisted user."""

    def __init__(self, allowed_id: int) -> None:
        self.allowed_id = allowed_id

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id != self.allowed_id:
            if user is not None:
                log.warning("Dropped update from unauthorized user id=%s", user.id)
            return None
        return await handler(event, data)


router = Router()


async def _keep_typing(bot: Bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            continue


async def send_long(message: Message, text: str) -> None:
    while text:
        chunk = text[:TELEGRAM_LIMIT]
        if len(text) > TELEGRAM_LIMIT:
            nl = chunk.rfind("\n")
            if nl > TELEGRAM_LIMIT // 2:
                chunk = chunk[:nl]
        await message.answer(chunk)
        text = text[len(chunk):].lstrip("\n")


run_lock = asyncio.Lock()
_whisper_model = None


async def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        loop = asyncio.get_running_loop()
        log.info("Loading Whisper model %r (first use)\u2026", WHISPER_MODEL)
        _whisper_model = await loop.run_in_executor(
            None,
            lambda: WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8"),
        )
    return _whisper_model


async def transcribe(path: str) -> str:
    model = await _get_whisper()
    loop = asyncio.get_running_loop()

    def _run() -> str:
        segments, _info = model.transcribe(path, language=WHISPER_LANGUAGE or None)
        return " ".join(seg.text for seg in segments).strip()

    return await loop.run_in_executor(None, _run)


async def process_prompt(message: Message, bot: Bot, prompt: str) -> None:
    if run_lock.locked():
        await message.answer("\u23f3 Still working on the previous request\u2026")
    async with run_lock:
        stop = asyncio.Event()
        typing = asyncio.create_task(_keep_typing(bot, message.chat.id, stop))
        try:
            result = await run_copilot(prompt)
        finally:
            stop.set()
            await typing
        await send_long(message, result)


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "\U0001f916 *Copilot bridge is online.*\n\n"
        f"Working directory: `{WORKING_DIR}`\n"
        f"Model: `{COPILOT_MODEL or 'default'}`\n"
        f"Denied tools: `{len(DENY_TOOLS)}`\n\n"
        "Send me a message and the Copilot agent will work on your machine.\n"
        "Commands: /status  /help",
    )


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(
        "Send any text and it becomes a prompt for the Copilot CLI, running in "
        f"`{WORKING_DIR}`.\n\n"
        "/start     \u2013 status banner\n"
        "/workspace \u2013 pick a project folder\n"
        "/sessions  \u2013 resume a saved session\n"
        "/status    \u2013 current configuration\n"
        "/new       \u2013 reset conversation context\n"
        "/brief     \u2013 short phone-style replies (default)\n"
        "/raw       \u2013 full Copilot output\n"
        "/help      \u2013 this message",
    )


@router.message(Command("status"))
async def on_status(message: Message) -> None:
    await message.answer(
        "Configuration:\n"
        f"\u2022 workspace: {active_workdir}\n"
        f"\u2022 session: {active_resume_id or 'auto (latest)'}\n"
        f"\u2022 model: {COPILOT_MODEL or 'default'}\n"
        f"\u2022 allow-all-tools: {ALLOW_ALL_TOOLS}\n"
        f"\u2022 denied tools: {', '.join(DENY_TOOLS) or 'none'}\n"
        f"\u2022 timeout: {REQUEST_TIMEOUT}s\n"
        f"\u2022 reply mode: {'brief' if summary_enabled else 'raw'}\n"
        f"\u2022 launcher: {' '.join(COPILOT_ARGV)}",
    )


@router.message(Command("new"))
async def on_new(message: Message) -> None:
    global _session_started
    _session_started = False
    await message.answer("\U0001f504 Fresh conversation \u2014 context reset.")


@router.message(Command("brief"))
async def on_brief(message: Message) -> None:
    global summary_enabled
    summary_enabled = True
    await message.answer(
        "\U0001f4f1 Brief mode \u2014 short phone-style summaries (full work still done)."
    )


@router.message(Command("raw"))
async def on_raw(message: Message) -> None:
    global summary_enabled
    summary_enabled = False
    await message.answer("\U0001f4c4 Raw mode \u2014 full Copilot output.")


COPILOT_SESSIONS_DIR = Path.home() / ".copilot" / "session-state"


def _session_title(session_dir: Path) -> str:
    try:
        files = sorted(
            (p for p in session_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        for f in files:
            try:
                with f.open("r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if '"user' not in line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        if str(obj.get("type", "")).startswith("user"):
                            content = obj.get("data", {}).get("content", "")
                            if isinstance(content, list):
                                content = " ".join(
                                    p.get("text", "") if isinstance(p, dict) else str(p)
                                    for p in content
                                )
                            content = str(content).split("\n\n---")[0].strip()
                            if content:
                                return content[:48]
            except Exception:
                continue
    except Exception:
        pass
    return "(untitled)"


def list_sessions(limit: int = 8) -> list[tuple[str, str, float]]:
    if not COPILOT_SESSIONS_DIR.is_dir():
        return []
    dirs = [d for d in COPILOT_SESSIONS_DIR.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return [(d.name, _session_title(d), d.stat().st_mtime) for d in dirs[:limit]]


@router.message(Command("sessions"))
async def on_sessions(message: Message) -> None:
    loop = asyncio.get_running_loop()
    sessions = await loop.run_in_executor(None, list_sessions, 8)
    if not sessions:
        await message.answer("No saved sessions yet \u2014 just send a message to start one.")
        return
    kb = InlineKeyboardBuilder()
    for sid, title, mtime in sessions:
        ts = datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
        kb.button(text=f"{title} \u00b7 {ts}", callback_data=f"sess:{sid}")
    kb.button(text="\U0001f195 New session", callback_data="sess:new")
    kb.adjust(1)
    await message.answer("Pick a session to resume:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("sess:"))
async def on_pick_session(cb: CallbackQuery) -> None:
    global active_resume_id, _session_started
    val = cb.data.split(":", 1)[1]
    if val == "new":
        active_resume_id = None
        _session_started = False
        await cb.answer("New session")
        await cb.message.answer("\U0001f195 New session \u2014 send your first message.")
    else:
        active_resume_id = val
        await cb.answer("Resumed")
        await cb.message.answer(
            f"\u25b6\ufe0f Resumed {val[:8]}. Ask away, e.g. \u00abна \u0447\u0451м остановились?\u00bb"
        )


@router.message(Command("workspace"))
async def on_workspace(message: Message) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"\U0001f4c1 {WORKING_DIR.name} (all)", callback_data="ws:*")
    if WORKING_DIR.is_dir():
        subdirs = sorted(
            d for d in WORKING_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        for d in subdirs[:20]:
            kb.button(text=d.name, callback_data=f"ws:{d.name}")
    kb.adjust(1)
    await message.answer("Pick a workspace:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("ws:"))
async def on_pick_workspace(cb: CallbackQuery) -> None:
    global active_workdir, active_resume_id, _session_started
    name = cb.data.split(":", 1)[1]
    active_workdir = WORKING_DIR if name == "*" else (WORKING_DIR / name)
    active_resume_id = None
    _session_started = False
    await cb.answer("Workspace set")
    await cb.message.answer(
        f"\U0001f4c1 Workspace: {active_workdir}\nNow /sessions or just send a message."
    )


@router.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot) -> None:
    if not ENABLE_VOICE:
        await message.answer("\U0001f3a4 Voice is disabled (set ENABLE_VOICE=true).")
        return
    media = message.voice or message.audio
    tmp = os.path.join(tempfile.gettempdir(), f"tgvoice_{message.message_id}.oga")
    try:
        await bot.download(media, destination=tmp)
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        text = await transcribe(tmp)
    except Exception as exc:  # noqa: BLE001
        log.exception("voice transcription failed")
        await message.answer(f"\U0001f3a4 Transcription failed: {exc}")
        return
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not text:
        await message.answer("\U0001f3a4 Couldn't make out any speech.")
        return
    await message.answer(f"\U0001f3a4 heard: {text}")
    await process_prompt(message, bot, text)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot) -> None:
    await process_prompt(message, bot, message.text or "")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def main() -> None:
    if not TELEGRAM_BOT_TOKEN or "PASTE_YOUR" in TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN in .env (get it from @BotFather)."
        )
    if not ALLOWED_USER_ID:
        raise SystemExit(
            "Set ALLOWED_USER_ID in .env to your numeric id (from @userinfobot)."
        )

    bot = Bot(TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.message.middleware(AuthMiddleware(ALLOWED_USER_ID))
    dp.include_router(router)

    log.info("Whitelisted user id: %s", ALLOWED_USER_ID)
    log.info("Copilot sandbox: %s", WORKING_DIR)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
