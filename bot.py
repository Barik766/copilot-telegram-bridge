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

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, BaseMiddleware, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
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
    "SAME language as the user's latest message, regardless of the "
    "surrounding conversation language \u2014 at most 3-4 sentences, no code "
    "blocks and no long file or diff dumps. Say what you did and where; the full "
    "diff stays on the machine for review in the editor. If you genuinely need a "
    "decision, ask one short question instead."
)
SUMMARY_INSTRUCTION = (
    os.getenv("SUMMARY_INSTRUCTION", "").strip() or DEFAULT_SUMMARY_INSTRUCTION
)

# VS Code bridge: push prompts into the real VS Code Copilot chat via the extension.
BRIDGE_URL = os.getenv("BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "").strip()
VSCODE_MODE = _get_bool("VSCODE_MODE", False)
NOTIFY_PORT = _get_int("NOTIFY_PORT", 8766)

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
vscode_mode = VSCODE_MODE
active_persona: str | None = None
deletable_ids: list[int] = []

PERSONAS: dict[str, tuple[str, str]] = {
    "friendly": (
        "\U0001f60a Friendly",
        "Warm, upbeat and casual, like a supportive buddy.",
    ),
    "critic": (
        "\U0001f9d0 Critic",
        "A sharp, skeptical critic: challenge my assumptions and point out flaws, "
        "risks and weak spots bluntly and honestly.",
    ),
    "perfectionist": (
        "\U0001f913 Perfectionist",
        "A meticulous, obsessive perfectionist: dig deep, chase edge cases, and "
        "refuse to leave anything sloppy or half-done.",
    ),
    "concise": (
        "\u26a1 Concise",
        "Terse and to the point. Minimal words, no fluff.",
    ),
}


def _track(msg: Message | None) -> None:
    if msg is not None:
        deletable_ids.append(msg.message_id)
        if len(deletable_ids) > 800:
            del deletable_ids[:400]


def _apply_persona(prompt: str) -> str:
    if active_persona and active_persona in PERSONAS:
        instr = PERSONAS[active_persona][1]
        return f"[Persona for how you talk to me: {instr}]\n\n{prompt}"
    return prompt


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
        _track(await message.answer(chunk))
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


async def inject_vscode(prompt: str) -> tuple[bool, str]:
    payload: dict = {"prompt": prompt}
    if BRIDGE_TOKEN:
        payload["token"] = BRIDGE_TOKEN
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{BRIDGE_URL}/inject", json=payload) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200 and data.get("ok"):
                    return True, str(data.get("via", ""))
                return False, str(data.get("error", f"HTTP {resp.status}"))
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def bridge_new_chat() -> None:
    payload: dict = {"token": BRIDGE_TOKEN} if BRIDGE_TOKEN else {}
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await session.post(f"{BRIDGE_URL}/new", json=payload)
    except Exception:  # noqa: BLE001
        pass


async def process_prompt(message: Message, bot: Bot, prompt: str) -> None:
    _track(message)
    prompt = _apply_persona(prompt)
    if vscode_mode:
        ok, info = await inject_vscode(prompt)
        if not ok:
            _track(
                await message.answer(
                    f"\u26a0 VS Code bridge unreachable: {info}\n"
                    "Is VS Code open with the extension running (toast on :8765)?"
                )
            )
        return
    if run_lock.locked():
        _track(await message.answer("\u23f3 Still working on the previous request\u2026"))
    async with run_lock:
        stop = asyncio.Event()
        typing = asyncio.create_task(_keep_typing(bot, message.chat.id, stop))
        try:
            result = await run_copilot(prompt)
        finally:
            stop.set()
            await typing
        await send_long(message, result)


MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="\u2630 Menu"),
            KeyboardButton(text="\U0001f9f9 Clear"),
        ]
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def build_menu():
    mode = "brief" if summary_enabled else "raw"
    target = "VS Code" if vscode_mode else "CLI"
    persona = PERSONAS[active_persona][0] if active_persona in PERSONAS else "Default"
    sess = active_resume_id[:8] if active_resume_id else "new / latest"
    lines = [
        "\U0001f916 <b>Claudy</b> \u2014 your Copilot on the phone\n",
        f"\U0001f3af Target: <b>{target}</b>",
        f"\U0001f3ad Persona: <b>{persona}</b>",
    ]
    if not vscode_mode:
        lines.append(f"\U0001f4c1 Workspace: <code>{active_workdir.name}</code>")
        lines.append(f"\U0001f5c2 Session: <code>{sess}</code>")
    lines.append(f"\U0001f9fe Mode: <b>{mode}</b>\n")
    lines.append("Send a message or a voice note \u2014 or use the buttons:")
    text = "\n".join(lines)
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=f"\U0001f3af Target: {target}", callback_data="menu:target"),
    )
    if not vscode_mode:
        b.row(
            InlineKeyboardButton(text="\U0001f4c1 Workspace", callback_data="menu:workspace"),
            InlineKeyboardButton(text="\U0001f5c2 Sessions", callback_data="menu:sessions"),
        )
    b.row(
        InlineKeyboardButton(text="\U0001f3ad Persona", callback_data="menu:persona"),
        InlineKeyboardButton(text=f"\U0001f9fe Mode: {mode}", callback_data="menu:mode"),
    )
    b.row(
        InlineKeyboardButton(text="\U0001f195 New chat", callback_data="menu:new"),
        InlineKeyboardButton(text="\U0001f9f9 Clear chat", callback_data="menu:clear"),
    )
    b.row(InlineKeyboardButton(text="\u2753 Help", callback_data="menu:help"))
    return text, b.as_markup()


async def _safe_edit(cb: CallbackQuery, text: str, markup=None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "\U0001f916 Claudy is online. Tap \u2630 Menu anytime.",
        reply_markup=MENU_KB,
    )
    text, markup = build_menu()
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(Command("menu"))
async def on_menu_cmd(message: Message) -> None:
    text, markup = build_menu()
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(F.text == "\u2630 Menu")
async def on_menu_btn(message: Message) -> None:
    text, markup = build_menu()
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(F.text == "\U0001f9f9 Clear")
async def on_clear_btn(message: Message, bot: Bot) -> None:
    tap_id = message.message_id
    await _do_clear(message.chat.id, bot)
    try:
        await bot.delete_message(message.chat.id, tap_id)
    except Exception:
        pass


@router.callback_query(F.data == "menu:home")
async def cb_home(cb: CallbackQuery) -> None:
    text, markup = build_menu()
    await _safe_edit(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data == "menu:workspace")
async def cb_workspace(cb: CallbackQuery) -> None:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"\U0001f4c1 {WORKING_DIR.name} (all)", callback_data="ws:*"))
    if WORKING_DIR.is_dir():
        subdirs = sorted(
            d for d in WORKING_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        for d in subdirs[:16]:
            b.row(InlineKeyboardButton(text=d.name, callback_data=f"ws:{d.name}"))
    b.row(InlineKeyboardButton(text="\U0001f3e0 Back", callback_data="menu:home"))
    await _safe_edit(cb, "\U0001f4c1 Choose a workspace (folder the agent works in):", b.as_markup())
    await cb.answer()


@router.callback_query(F.data == "menu:sessions")
async def cb_sessions(cb: CallbackQuery) -> None:
    loop = asyncio.get_running_loop()
    sessions = await loop.run_in_executor(None, list_sessions, 10)
    b = InlineKeyboardBuilder()
    for sid, title, mtime in sessions:
        ts = datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
        b.row(InlineKeyboardButton(text=f"{title} \u00b7 {ts}", callback_data=f"sess:{sid}"))
    b.row(InlineKeyboardButton(text="\U0001f195 New chat", callback_data="menu:new"))
    b.row(InlineKeyboardButton(text="\U0001f3e0 Back", callback_data="menu:home"))
    head = (
        "\U0001f5c2 Your Copilot CLI sessions (this bot's history \u2014 separate "
        "from VS Code chats):"
        if sessions
        else "No sessions yet \u2014 send a message to start one."
    )
    await _safe_edit(cb, head, b.as_markup())
    await cb.answer()


@router.callback_query(F.data == "menu:mode")
async def cb_mode(cb: CallbackQuery) -> None:
    global summary_enabled
    summary_enabled = not summary_enabled
    text, markup = build_menu()
    await _safe_edit(cb, text, markup)
    await cb.answer("brief" if summary_enabled else "raw")


@router.callback_query(F.data == "menu:target")
async def cb_target(cb: CallbackQuery) -> None:
    global vscode_mode
    vscode_mode = not vscode_mode
    text, markup = build_menu()
    await _safe_edit(cb, text, markup)
    await cb.answer("VS Code" if vscode_mode else "CLI")


@router.callback_query(F.data == "menu:persona")
async def cb_persona(cb: CallbackQuery) -> None:
    b = InlineKeyboardBuilder()
    for key, (label, _instr) in PERSONAS.items():
        mark = " \u2713" if key == active_persona else ""
        b.row(InlineKeyboardButton(text=f"{label}{mark}", callback_data=f"persona:{key}"))
    b.row(InlineKeyboardButton(text="Default", callback_data="persona:none"))
    b.row(InlineKeyboardButton(text="\U0001f3e0 Back", callback_data="menu:home"))
    await _safe_edit(cb, "\U0001f3ad Pick how the agent talks to you:", b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("persona:"))
async def cb_pick_persona(cb: CallbackQuery) -> None:
    global active_persona
    key = cb.data.split(":", 1)[1]
    active_persona = None if key == "none" else key
    text, markup = build_menu()
    await _safe_edit(cb, text, markup)
    label = PERSONAS[active_persona][0] if active_persona in PERSONAS else "Default"
    await cb.answer(label)


async def _do_clear(chat_id: int, bot: Bot) -> int:
    ids = list(deletable_ids)
    deletable_ids.clear()
    deleted = 0
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            pass
    return deleted


@router.callback_query(F.data == "menu:clear")
async def cb_clear(cb: CallbackQuery, bot: Bot) -> None:
    deleted = await _do_clear(cb.message.chat.id, bot)
    await cb.answer(f"Cleared {deleted}")
    text, markup = build_menu()
    await _safe_edit(cb, text, markup)


@router.callback_query(F.data == "menu:new")
async def cb_new(cb: CallbackQuery) -> None:
    global active_resume_id, _session_started
    active_resume_id = None
    _session_started = False
    if vscode_mode:
        await bridge_new_chat()
    text, markup = build_menu()
    await _safe_edit(cb, text, markup)
    await cb.answer("New chat")


@router.callback_query(F.data == "menu:help")
async def cb_help(cb: CallbackQuery) -> None:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="\U0001f3e0 Back", callback_data="menu:home"))
    txt = (
        "\u2753 <b>How Claudy works</b>\n\n"
        "\u2022 Send text or a voice note \u2192 the Copilot agent works in your "
        "chosen workspace and replies with a short summary.\n"
        "\u2022 \U0001f9fe <b>Mode</b>: brief (phone summary) \u2194 raw (full output).\n"
        "\u2022 \U0001f4c1 <b>Workspace</b>: which project the agent touches.\n"
        "\u2022 \U0001f5c2 <b>Sessions</b>: resume a previous conversation.\n"
        "\u2022 \U0001f195 <b>New chat</b>: start fresh.\n\n"
        "Sessions are stored by Copilot CLI in ~/.copilot/session-state \u2014 "
        "separate from your VS Code chats (different app, different store)."
    )
    await _safe_edit(cb, txt, b.as_markup())
    await cb.answer()


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


def _session_first_message(session_dir: Path) -> str | None:
    events = session_dir / "events.jsonl"
    if not events.is_file():
        return None
    try:
        with events.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"user.message"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "user.message":
                    content = str(obj.get("data", {}).get("content", ""))
                    content = content.split("\n\n---")[0]
                    content = " ".join(content.split())
                    return content or None
    except Exception:
        return None
    return None


def list_sessions(limit: int = 10) -> list[tuple[str, str, float]]:
    if not COPILOT_SESSIONS_DIR.is_dir():
        return []
    dirs = [d for d in COPILOT_SESSIONS_DIR.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    out: list[tuple[str, str, float]] = []
    for d in dirs:
        title = _session_first_message(d)
        if not title:
            continue
        out.append((d.name, title[:40], d.stat().st_mtime))
        if len(out) >= limit:
            break
    return out


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
        await cb.answer("New chat")
    else:
        active_resume_id = val
        await cb.answer("Resumed")
    text, markup = build_menu()
    await _safe_edit(cb, text, markup)


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
    await cb.answer(f"Workspace: {active_workdir.name}")
    text, markup = build_menu()
    await _safe_edit(cb, text, markup)


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
    _track(await message.answer(f"\U0001f3a4 \u00ab{text}\u00bb"))
    await process_prompt(message, bot, text)


@router.message(
    F.text
    & ~F.text.startswith("/")
    & (F.text != "\u2630 Menu")
    & (F.text != "\U0001f9f9 Clear")
)
async def on_text(message: Message, bot: Bot) -> None:
    await process_prompt(message, bot, message.text or "")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def _start_notify_server(bot: Bot) -> None:
    async def handle(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        if BRIDGE_TOKEN and str(data.get("token", "")) != BRIDGE_TOKEN:
            return web.json_response({"ok": False, "error": "bad token"}, status=401)
        text = str(data.get("text", "")).strip()[:4000]
        if text:
            try:
                sent = await bot.send_message(ALLOWED_USER_ID, f"\U0001f5a5\ufe0f {text}")
                _track(sent)
            except Exception as exc:  # noqa: BLE001
                log.warning("notify send failed: %s", exc)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/notify", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", NOTIFY_PORT)
    await site.start()
    log.info("Notify server on 127.0.0.1:%d", NOTIFY_PORT)


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

    await _start_notify_server(bot)

    log.info("Whitelisted user id: %s", ALLOWED_USER_ID)
    log.info("Copilot sandbox: %s", WORKING_DIR)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
