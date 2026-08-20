"""A tiny in-memory Telegram stand-in.

It drives the *real* handlers registered in main.build_application(), so the
patterns, keyboards and message flow under test are the ones that ship.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

from telegram.ext import CallbackQueryHandler, CommandHandler


def run(coro):
    return asyncio.run(coro)


@dataclass
class Msg:
    chat_id: int
    text: str
    reply_markup: Any = None

    def buttons(self) -> list:
        if not self.reply_markup:
            return []
        return [b for row in self.reply_markup.inline_keyboard for b in row]


class FakeChat:
    def __init__(self, world: "World", chat_id: int):
        self.world = world
        self.id = chat_id

    async def send_message(self, text, reply_markup=None, parse_mode=None):
        msg = Msg(self.id, text, reply_markup)
        self.world.inboxes[self.id].append(msg)
        return msg


class FakeUser:
    def __init__(self, user_id: int, username: str | None):
        self.id = user_id
        self.username = username


class FakeQuery:
    def __init__(self, world: "World", msg: Msg, data: str, user: FakeUser):
        self.world = world
        self.message = msg
        self.data = data
        self.from_user = user
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))
        self.world.alerts.append((self.from_user.id, text, show_alert))

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.message.text = text
        self.message.reply_markup = reply_markup

    async def edit_message_reply_markup(self, reply_markup=None):
        self.message.reply_markup = reply_markup


@dataclass
class FakeMessage:
    text: str | None = None
    caption: str | None = None


class FakeUpdate:
    def __init__(self, user: FakeUser, chat: FakeChat, message=None, callback_query=None):
        self.effective_user = user
        self.effective_chat = chat
        self.message = message
        self.callback_query = callback_query


class FakeBot:
    def __init__(self, world: "World"):
        self.world = world

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        msg = Msg(chat_id, text, reply_markup)
        self.world.inboxes[chat_id].append(msg)
        return msg


class FakeContext:
    def __init__(self, world: "World", user_id: int, args: list[str] | None = None):
        self.world = world
        self.bot = world.bot
        self.args = args or []
        self.user_data = world.user_data[user_id]
        self.bot_data = world.bot_data
        self.chat_data = world.chat_data[user_id]


class World:
    """Holds every user's inbox plus the persistent-ish user_data dicts."""

    def __init__(self, application):
        self.handlers = application.handlers[0]
        self.inboxes: dict[int, list[Msg]] = defaultdict(list)
        self.user_data: dict[int, dict] = defaultdict(dict)
        self.chat_data: dict[int, dict] = defaultdict(dict)
        self.bot_data: dict = {}
        self.alerts: list[tuple[int, str | None, bool]] = []
        self.bot = FakeBot(self)

    def command_handler(self, name: str) -> Callable:
        for handler in self.handlers:
            if isinstance(handler, CommandHandler) and name in handler.commands:
                return handler.callback
        raise AssertionError(f"no handler registered for /{name}")

    def callback_handler(self, data: str) -> Callable:
        for handler in self.handlers:
            if isinstance(handler, CallbackQueryHandler) and handler.pattern and handler.pattern.search(data):
                return handler.callback
        raise AssertionError(f"no handler matches callback data {data!r}")

    def message_handler(self) -> Callable:
        from telegram.ext import MessageHandler

        for handler in self.handlers:
            if isinstance(handler, MessageHandler):
                return handler.callback
        raise AssertionError("no message handler registered")


class Session:
    """One Telegram user talking to the bot."""

    def __init__(self, world: World, user_id: int, username: str | None):
        self.world = world
        self.user = FakeUser(user_id, username)
        self.chat = FakeChat(world, user_id)

    # ----------------------------------------------------------- inspection
    @property
    def inbox(self) -> list[Msg]:
        return self.world.inboxes[self.user.id]

    @property
    def last(self) -> Msg:
        assert self.inbox, "no messages received"
        return self.inbox[-1]

    def texts(self) -> list[str]:
        return [m.text for m in self.inbox]

    def all_text(self) -> str:
        return "\n".join(self.texts())

    def clear(self) -> None:
        self.inbox.clear()

    def find_button(self, needle: str):
        """Newest message first — returns (msg, button)."""
        for msg in reversed(self.inbox):
            for button in msg.buttons():
                if needle.lower() in button.text.lower():
                    return msg, button
        raise AssertionError(f"no button matching {needle!r}. Inbox:\n" + self.debug())

    def has_button(self, needle: str) -> bool:
        try:
            self.find_button(needle)
            return True
        except AssertionError:
            return False

    def debug(self) -> str:
        return "\n---\n".join(
            f"{m.text}\n[buttons: {', '.join(b.text for b in m.buttons())}]" for m in self.inbox
        )

    # -------------------------------------------------------------- actions
    def command(self, name: str, *args: str):
        handler = self.world.command_handler(name)
        update = FakeUpdate(self.user, self.chat, message=FakeMessage(text=f"/{name} {' '.join(args)}".strip()))
        return run(handler(update, FakeContext(self.world, self.user.id, list(args))))

    def say(self, text: str):
        handler = self.world.message_handler()
        update = FakeUpdate(self.user, self.chat, message=FakeMessage(text=text))
        return run(handler(update, FakeContext(self.world, self.user.id)))

    def tap(self, needle: str):
        msg, button = self.find_button(needle)
        return self.tap_data(button.callback_data, msg)

    def tap_data(self, data: str, msg: Msg | None = None):
        # Falls back to a synthetic message so a "stale button" can always be pressed.
        target = msg or (self.inbox[-1] if self.inbox else Msg(self.user.id, "(old message)"))
        handler = self.world.callback_handler(data)
        query = FakeQuery(self.world, target, data, self.user)
        update = FakeUpdate(self.user, self.chat, callback_query=query)
        run(handler(update, FakeContext(self.world, self.user.id)))
        return query
