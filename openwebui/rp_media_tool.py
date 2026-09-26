"""
title: RP Media
description: Lets a roleplay character send photos and videos from its pre-tagged media library (served by the RP Media server).
version: 1.0.0
requirements: httpx
"""

import html
from typing import Any, Awaitable, Callable, Literal, Optional

import httpx
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        api_base_url: str = Field(
            default="http://192.168.1.10:8090",
            description="URL the Open WebUI *server* uses to reach the RP Media API (internal LAN address is fine).",
        )
        api_key: str = Field(default="", description="TOOL_API_KEY of the RP Media server.")
        library_override: str = Field(
            default="",
            description="Optional library slug to always use. Leave empty to pick the library linked to the chat's model.",
        )
        max_height_px: int = Field(default=520, description="Maximum rendered height of media in the chat.")
        timeout_s: float = Field(default=60.0, description="HTTP timeout (the first call may need to load the embedding model).")
        debug: bool = Field(default=False, description="Show match scores in the status line.")

    def __init__(self):
        self.valves = self.Valves()

    async def send_media(
        self,
        description: str,
        media_type: Literal["image", "video", "any"] = "image",
        user_requested: bool = False,
        __model__: Optional[dict] = None,
        __chat_id__: Optional[str] = None,
        __message_id__: Optional[str] = None,
        __messages__: Optional[list] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[Any]]] = None,
    ) -> Any:
        """
        Send the user a real photo or short video of yourself (your character) from your personal camera roll.
        Use this when the user asks for a picture/selfie/video, or occasionally on your own when showing
        something would feel natural in the scene (e.g. "just got to the beach, look!").
        Do not use it in every message. After it returns, continue replying in character and react to what
        the media actually shows. Never mention files, links, tools or URLs.

        :param description: What the photo/video should show, written as a visual description: framing (selfie, mirror selfie, close-up, full body), location, outfit, activity, mood and time of day. Example: "mirror selfie in the bedroom wearing a black dress, playful smile, night".
        :param media_type: "image" for a photo, "video" for a short clip, "any" if either is fine.
        :param user_requested: true if the user explicitly asked for a picture or video in their last message, false if you are sending it spontaneously.
        """
        emit = __event_emitter__ or (lambda _e: _noop())

        async def status(text: str, done: bool = False) -> None:
            await emit({"type": "status", "data": {"description": text, "done": done, "hidden": False}})

        await status("📷 Picking something to send…")

        model_id = (__model__ or {}).get("id")
        user_turn = sum(1 for m in (__messages__ or []) if m.get("role") == "user")
        payload = {
            "description": description,
            "media_type": media_type,
            "user_requested": bool(user_requested),
            "model_id": model_id,
            "library": self.valves.library_override or None,
            "chat_id": __chat_id__,
            "message_id": __message_id__,
            "user_turn": user_turn,
        }

        try:
            async with httpx.AsyncClient(timeout=self.valves.timeout_s) as client:
                r = await client.post(
                    f"{self.valves.api_base_url.rstrip('/')}/api/send",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.valves.api_key}"},
                )
            if r.status_code == 404:
                await status("📷 No media library is linked to this model", done=True)
                return "No media is available for you right now. Continue the conversation without sending anything; do not mention this."
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            await status(f"📷 Media server error: {type(e).__name__}", done=True)
            return "Sending media failed for technical reasons. Continue the conversation naturally without it; do not mention the failure."

        if data.get("status") == "cooldown":
            await status("📷 Holding off on sending more media for now", done=True)
            return (
                "You sent media very recently, so don't send another one yet. "
                "Continue replying in character with text only, and don't mention this."
            )
        if data.get("status") != "sent":
            await status("📷 Nothing suitable in the camera roll", done=True)
            if user_requested:
                return (
                    "You don't have a photo/video matching that request. Stay in character: give a natural in-story reason "
                    "you can't send that exact one right now, or offer something else. Don't mention tools or libraries."
                )
            return "You have nothing fitting to send right now. Continue in character with text only; don't mention this."

        media = data["media"]
        kind = "photo" if media["kind"] == "image" else "video"
        extra = f" (score {data.get('score')})" if self.valves.debug else ""
        await status(f"📷 Sent a {kind}{extra}", done=True)

        context = (
            f"The {kind} was delivered and the user can now see it. It shows: {media['caption']} "
            "Continue your reply in character, consistent with what the media actually shows. "
            "Don't describe it in exhaustive detail, don't claim it shows anything else, and never mention files, links or URLs."
        )
        return (
            HTMLResponse(content=self._render(media), headers={"Content-Disposition": "inline"}),
            context,
        )

    def _render(self, media: dict) -> str:
        url = html.escape(media["url"], quote=True)
        alt = html.escape(media.get("caption") or "", quote=True)
        maxh = int(self.valves.max_height_px)
        if media["kind"] == "video":
            poster = f' poster="{html.escape(media["poster_url"], quote=True)}"' if media.get("poster_url") else ""
            body = f'<video src="{url}"{poster} controls playsinline loop preload="metadata"></video>'
        else:
            body = f'<a href="{url}" target="_blank" rel="noopener"><img src="{url}" alt="{alt}" title="{alt}"></a>'
        return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;padding:0;background:transparent;overflow:hidden}}
img,video{{display:block;max-width:100%;max-height:{maxh}px;height:auto;border-radius:14px}}
</style></head><body>{body}
<script>
function h(){{parent.postMessage({{type:'iframe:height',height:document.documentElement.scrollHeight}},'*')}}
window.addEventListener('load',h);new ResizeObserver(h).observe(document.body);
document.querySelectorAll('img,video').forEach(e=>{{e.addEventListener('load',h);e.addEventListener('loadedmetadata',h)}});
</script></body></html>"""


async def _noop() -> None:
    return None
