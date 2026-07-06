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
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, BaseMiddleware, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
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


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0") or "0")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "").strip()
ALLOW_ALL_TOOLS = _get_bool("ALLOW_ALL_TOOLS", True)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "900") or "900")

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


def build_command(prompt: str) -> list[str]:
    args = list(COPILOT_ARGV) + ["-p", prompt, "--silent"]
    if COPILOT_MODEL:
        args += ["--model", COPILOT_MODEL]
    if ALLOW_ALL_TOOLS:
        args.append("--allow-all-tools")
    for tool in DENY_TOOLS:
        args += ["--deny-tool", tool]
    args += ["-C", str(WORKING_DIR)]
    return args


async def run_copilot(prompt: str) -> str:
    args = build_command(prompt)
    log.info("copilot -p %r (deny=%d tools)", prompt[:80], len(DENY_TOOLS))
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(WORKING_DIR),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"\u23f1 Timed out after {REQUEST_TIMEOUT}s."

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
        "/start  \u2013 status banner\n"
        "/status \u2013 current configuration\n"
        "/help   \u2013 this message",
    )


@router.message(Command("status"))
async def on_status(message: Message) -> None:
    await message.answer(
        "Configuration:\n"
        f"\u2022 working dir: {WORKING_DIR}\n"
        f"\u2022 model: {COPILOT_MODEL or 'default'}\n"
        f"\u2022 allow-all-tools: {ALLOW_ALL_TOOLS}\n"
        f"\u2022 denied tools: {', '.join(DENY_TOOLS) or 'none'}\n"
        f"\u2022 timeout: {REQUEST_TIMEOUT}s\n"
        f"\u2022 launcher: {' '.join(COPILOT_ARGV)}",
    )


@router.message(F.voice | F.audio)
async def on_voice(message: Message) -> None:
    await message.answer(
        "\U0001f3a4 Voice input is on the roadmap. For now, please send text.",
    )


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot) -> None:
    prompt = message.text or ""
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


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing (set it in .env).")
    if not ALLOWED_USER_ID:
        raise SystemExit("ALLOWED_USER_ID is missing (set it in .env).")

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
