"""The one place markup is mutated. Every text/html response the portal sends passes through
MutationMiddleware, which calls mutate_html -- the same function the tests and the answer key
call."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import lxml.etree
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from testbench.mutations.base import CatalogueEntry, apply_all, parse, serialise
from testbench.portal.middleware import day_of
from testbench.schedule import mutations_for, seed_for

TARGET = "testbench"
SEED_META = "sd-mutation-seed"


def inject_seed_meta(html: str, seed: str, names: Sequence[str]) -> str:
    doc = parse(html)
    head = doc.find("head")
    if head is None:
        head = doc.makeelement("head", {})
        doc.insert(0, head)
    for old in head.findall(f'meta[@name="{SEED_META}"]'):
        head.remove(old)
    lxml.etree.SubElement(head, "meta", {"name": SEED_META, "content": f"{seed};{','.join(names)}"})
    return serialise(doc)


def mutate_html(
    day: dt.date,
    html: str,
    *,
    target: str = TARGET,
    catalogue: Sequence[CatalogueEntry] | None = None,
) -> str:
    applied = mutations_for(day, target, catalogue)
    return inject_seed_meta(
        apply_all(applied, html), seed_for(day, target), [m.name for m in applied]
    )


def _is_html(start: Message) -> bool:
    return Headers(raw=start["headers"]).get("content-type", "").startswith("text/html")


class MutationMiddleware:
    """Buffers each text/html response, replaces its body with mutate_html(day, body), fixes
    Content-Length, and passes every other response through untouched and unbuffered."""

    def __init__(
        self, app: ASGIApp, *, catalogue: Sequence[CatalogueEntry] | None, target: str = TARGET
    ) -> None:
        self.app = app
        self._catalogue = catalogue
        self._target = target

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        day = day_of(scope)
        held: Message | None = None
        body = bytearray()

        async def rewrite(message: Message) -> None:
            nonlocal held
            if message["type"] == "http.response.start":
                if _is_html(message):
                    held = message
                    return
                await send(message)
                return
            if message["type"] == "http.response.body" and held is not None:
                body.extend(message.get("body", b""))
                if message.get("more_body", False):
                    return
                html = mutate_html(
                    day, body.decode("utf-8"), target=self._target, catalogue=self._catalogue
                )
                payload = html.encode("utf-8")
                headers = MutableHeaders(raw=list(held["headers"]))
                headers["content-length"] = str(len(payload))
                await send({**held, "headers": headers.raw})
                await send({"type": "http.response.body", "body": payload, "more_body": False})
                return
            await send(message)

        await self.app(scope, receive, rewrite)
