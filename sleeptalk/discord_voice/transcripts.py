"""Optional, coalesced live captions; Discord I/O never blocks microphone input."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import discord

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class Caption:
    name: str
    text: str = ""
    final: bool = False
    sealed: bool = False
    message: Any = None
    task: asyncio.Task | None = None
    sent: str = ""
    last_sent: float = float("-inf")
    failed: bool = False


class LiveTranscripts:
    def __init__(self, channel: Any, *, interval: float = 1.0) -> None:
        self.channel = channel
        self.interval = interval
        self._active: dict[int, Caption] = {}
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    def update(self, user: Any, text: str) -> None:
        if self._closed or not text:
            return
        caption = self._active.setdefault(user.id, Caption(str(user.display_name)))
        if text == caption.text:
            return
        caption.text = text
        self._schedule(caption)

    def seal(self, user: Any) -> Caption:
        caption = self._active.pop(user.id, None) or Caption(str(user.display_name))
        caption.sealed = True
        return caption

    def finish(self, caption: Caption, text: str) -> None:
        caption.text = text
        caption.final = True
        self._schedule(caption)

    def _schedule(self, caption: Caption) -> None:
        if self._closed or caption.failed or (caption.task and not caption.task.done()):
            return
        caption.task = asyncio.create_task(self._drain(caption))
        self._tasks.add(caption.task)
        caption.task.add_done_callback(self._tasks.discard)

    def _content(self, caption: Caption) -> str:
        prefix = "heard" if caption.final else "hearing"
        # Keep spoken Markdown inert, disable mentions separately, and leave
        # room for the status/name within Discord's message size limit.
        name = discord.utils.escape_markdown(caption.name)[:100]
        text = discord.utils.escape_markdown(caption.text)
        return f"{prefix} {name}: {text}"[:2000]

    async def _drain(self, caption: Caption) -> None:
        try:
            while not self._closed:
                if caption.sealed and not caption.final:
                    return
                content = self._content(caption)
                if content == caption.sent:
                    return
                delay = self.interval - (asyncio.get_running_loop().time() - caption.last_sent)
                if delay > 0:
                    await asyncio.sleep(delay)
                if caption.sealed and not caption.final:
                    return
                content = self._content(caption)
                async with asyncio.timeout(5):
                    if caption.message is None:
                        caption.message = await self.channel.send(
                            content, allowed_mentions=discord.AllowedMentions.none(),
                        )
                    else:
                        await caption.message.edit(
                            content=content, allowed_mentions=discord.AllowedMentions.none(),
                        )
                caption.sent = content
                caption.last_sent = asyncio.get_running_loop().time()
        except (discord.HTTPException, TimeoutError):
            caption.failed = True
            logger.debug("Live transcript delivery unavailable", exc_info=True)

    def close(self) -> None:
        self._closed = True
        self._active.clear()
        for task in self._tasks:
            task.cancel()

    async def wait_closed(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
