"""
title: RP Toy
description: Lets a roleplay character control the user's toy (vibration patterns) through the RP Media server's Intiface bridge.
version: 1.0.0
requirements: httpx
"""

from typing import Any, Awaitable, Callable, Literal, Optional

import httpx
from pydantic import BaseModel, Field

Pattern = Literal["steady", "pulse", "wave", "ramp", "heartbeat", "tease", "random"]


class Tools:
    class Valves(BaseModel):
        api_base_url: str = Field(
            default="http://192.168.1.10:8090",
            description="URL the Open WebUI *server* uses to reach the RP Media API (internal LAN address is fine).",
        )
        api_key: str = Field(default="", description="TOOL_API_KEY of the RP Media server.")
        max_intensity: int = Field(default=100, description="Ceiling (0-100) on what the character may request. The server has its own cap too.")
        max_duration_s: int = Field(default=180, description="Ceiling in seconds per call. The server caps at TOY_MAX_DURATION_S as well.")
        timeout_s: float = Field(default=15.0, description="HTTP timeout.")
        show_status: bool = Field(default=True, description="Show a status line in the chat when the toy changes.")

    def __init__(self):
        self.valves = self.Valves()

    async def control_toy(
        self,
        action: Literal["start", "stop"] = "start",
        pattern: Pattern = "wave",
        intensity: int = 40,
        duration_s: int = 60,
        __event_emitter__: Optional[Callable[[dict], Awaitable[Any]]] = None,
    ) -> str:
        """
        Control the toy the user is wearing. Starting a new pattern replaces the current one; it runs on its own and
        stops automatically after duration_s, so call it again to keep going, change it, or stop early.
        Use it when the scene calls for it, when the user asks, and always stop immediately if the user asks you to stop.
        Escalate gradually: begin low (20-40) and raise intensity over several turns. Never mention tools, apps or devices.

        :param action: "start" to run a pattern (also to change the current one), "stop" to switch the toy off.
        :param pattern: steady (constant), pulse (on/off each second), wave (slow rise and fall), ramp (climbs then drops), heartbeat (double thump), tease (builds up then backs off), random (wandering). All repeat until the time is up.
        :param intensity: Strength 0-100. 20-40 is gentle, 50-70 is strong, 80+ is intense.
        :param duration_s: How long to run, in seconds (up to 180). Pick a length that covers the time until your next message; a following call can extend or change it.
        """
        emit = __event_emitter__ or (lambda _e: _noop())

        async def status(text: str, done: bool = True) -> None:
            if self.valves.show_status:
                await emit({"type": "status", "data": {"description": text, "done": done, "hidden": False}})

        intensity = max(0, min(int(intensity), int(self.valves.max_intensity)))
        duration_s = max(1, min(int(duration_s), int(self.valves.max_duration_s)))
        payload = {
            "action": "stop" if action == "stop" else "play",
            "pattern": pattern,
            "intensity": intensity,
            "duration_s": duration_s,
        }

        try:
            async with httpx.AsyncClient(timeout=self.valves.timeout_s) as client:
                r = await client.post(
                    f"{self.valves.api_base_url.rstrip('/')}/api/toy",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.valves.api_key}"},
                )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            await status(f"🎛️ Toy server error: {type(e).__name__}")
            return "The toy could not be reached. Continue the conversation in character without it and don't mention any technical problem."

        st = data.get("status")
        if st == "stopped":
            await status("🎛️ Toy stopped")
            return "The toy is now off. Continue in character."
        if st == "playing":
            await status(f"🎛️ Toy: {data['pattern']} at {data['intensity']}% for {data['duration_s']}s")
            return (
                f"The toy is now running the '{data['pattern']}' pattern at {data['intensity']}% and will stop by itself "
                f"after {data['duration_s']} seconds unless you call control_toy again. Continue your reply in character, "
                "reacting to what the user feels. Never mention tools, apps, devices or settings."
            )
        if st == "no_device":
            await status("🎛️ No toy connected")
        else:
            await status(f"🎛️ Toy unavailable: {data.get('reason', 'unknown')}")
        return "The toy isn't available right now. Stay in character and carry on with words only; don't mention the reason."


async def _noop() -> None:
    return None
