"""Toy bridge: one long-lived Buttplug (Intiface Central) websocket plus a repeating-pattern runner.

Why a bridge: Intiface stops every device the moment a client disconnects, so a "connect, send, return"
tool call would kill the vibration instantly. This module keeps the connection open for the life of the
server, reconnects with backoff, and runs patterns on its own timer so API calls return immediately and
the hard duration cap (TOY_MAX_DURATION_S) is enforced here, never trusted to the chat model.

Only ScalarCmd/Vibrate actuators are driven (Lovense Gush 2 and similar). Protocol: Buttplug v3 JSON.
"""

import asyncio
import json
import logging
import math
import random
from dataclasses import dataclass, field
from typing import Callable

from .config import get_settings

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 3
CALL_TIMEOUT = 5.0

# --- patterns ---------------------------------------------------------------
# Each factory returns f(t_seconds) -> 0..1, a multiplier on the requested intensity. All of them repeat
# for as long as the run lasts, so a 3-minute "wave" is 45 waves, not one long fade.


def _steady(_seed: int) -> Callable[[float], float]:
    return lambda t: 1.0


def _pulse(_seed: int) -> Callable[[float], float]:
    return lambda t: 1.0 if (t % 1.0) < 0.55 else 0.0


def _wave(_seed: int) -> Callable[[float], float]:
    return lambda t: 0.3 + 0.7 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 4.0))


def _ramp(_seed: int) -> Callable[[float], float]:
    return lambda t: 0.2 + 0.8 * ((t % 6.0) / 6.0)


def _heartbeat(_seed: int) -> Callable[[float], float]:
    def f(t: float) -> float:
        p = t % 1.2
        if p < 0.15:
            return 1.0
        if 0.3 <= p < 0.45:
            return 0.8
        return 0.15

    return f


def _tease(_seed: int) -> Callable[[float], float]:
    # Slow build over 18 s, then drop to a low hum for 6 s, and again.
    def f(t: float) -> float:
        p = t % 24.0
        return 0.25 + 0.75 * (p / 18.0) if p < 18.0 else 0.25

    return f


def _random(seed: int) -> Callable[[float], float]:
    rng = random.Random(seed)
    targets = [rng.uniform(0.2, 1.0) for _ in range(4096)]  # a new target every 1.5 s; ~1.7 h before it repeats

    def f(t: float) -> float:
        seg, frac = divmod(t / 1.5, 1.0)
        i = int(seg) % len(targets)
        a, b = targets[i], targets[(i + 1) % len(targets)]
        return a + (b - a) * frac

    return f


PATTERNS: dict[str, tuple[str, Callable[[int], Callable[[float], float]]]] = {
    "steady": ("constant level", _steady),
    "pulse": ("on/off about once a second", _pulse),
    "wave": ("smooth rise and fall every 4 s", _wave),
    "ramp": ("climbs for 6 s, drops, repeats", _ramp),
    "heartbeat": ("double thump every 1.2 s", _heartbeat),
    "tease": ("builds for 18 s, backs off for 6 s, repeats", _tease),
    "random": ("smoothly wandering level", _random),
}


def make_pattern(name: str, seed: int = 0) -> Callable[[float], float]:
    return PATTERNS[name][1](seed)


# --- bridge -----------------------------------------------------------------


@dataclass
class Device:
    index: int
    name: str
    vibrators: list[tuple[int, int]] = field(default_factory=list)  # (actuator index, step count)


@dataclass
class Active:
    pattern: str
    intensity: int  # 0..100 as requested
    duration_s: float
    started_at: float
    task: asyncio.Task


class ToyBridge:
    def __init__(self, url: str | None = None, rate_hz: float | None = None):
        self._url = url
        self._rate_hz = rate_hz
        self.ws = None
        self.connected = False
        self.error: str | None = None
        self.devices: dict[int, Device] = {}
        self.active: Active | None = None
        self._task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._closing = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- config
    @property
    def url(self) -> str:
        return self._url if self._url is not None else get_settings().intiface_url

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def vibrating_devices(self) -> list[Device]:
        return [d for d in self.devices.values() if d.vibrators]

    def status(self) -> dict:
        a = self.active
        active = None
        if a and not a.task.done():
            elapsed = self._loop.time() - a.started_at if self._loop else 0.0
            active = {
                "pattern": a.pattern,
                "intensity": a.intensity,
                "duration_s": round(a.duration_s),
                "remaining_s": max(0, round(a.duration_s - elapsed)),
            }
        return {
            "configured": self.configured,
            "url": self.url,
            "connected": self.connected,
            "devices": [d.name for d in self.vibrating_devices()],
            "error": self.error,
            "active": active,
        }

    # -- lifecycle
    def start(self) -> None:
        """Call from inside the running event loop (app lifespan)."""
        if not self.configured or self._task:
            return
        self._loop = asyncio.get_running_loop()
        self._closing = False
        self._task = self._loop.create_task(self._connection_loop(), name="toy-bridge")

    async def stop(self) -> None:
        self._closing = True
        await self.stop_pattern()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _connection_loop(self) -> None:
        from websockets.asyncio.client import connect

        backoff = 2.0
        while not self._closing:
            reader = pinger = None
            try:
                async with connect(self.url, open_timeout=5, ping_interval=None, max_size=2**20) as ws:
                    self.ws = ws
                    reader = asyncio.create_task(self._reader(ws))
                    info = await self._call("RequestServerInfo", {"ClientName": "RP Media", "MessageVersion": PROTOCOL_VERSION})
                    if info.get("MessageVersion", PROTOCOL_VERSION) < PROTOCOL_VERSION:
                        raise RuntimeError(f"Intiface speaks protocol v{info.get('MessageVersion')}, need v{PROTOCOL_VERSION}")
                    max_ping = int(info.get("MaxPingTime") or 0)
                    if max_ping > 0:
                        pinger = asyncio.create_task(self._pinger(max_ping))
                    self.connected = True
                    self.error = None
                    backoff = 2.0
                    log.info("toy: connected to %s (%s)", self.url, info.get("ServerName", "?"))
                    dl = await self._call("RequestDeviceList", {})
                    self._set_devices(dl.get("Devices", []))
                    try:
                        await self._call("StartScanning", {})
                    except Exception as e:  # scanning is best-effort; known devices auto-connect anyway
                        log.info("toy: StartScanning: %s", e)
                    await reader  # blocks until the socket closes
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}".strip(": ")
                log.warning("toy: %s (retry in %.0fs)", self.error, backoff)
            finally:
                was_connected = self.connected
                self.connected = False
                self.ws = None
                self.devices.clear()
                for t in (reader, pinger):
                    if t and not t.done():
                        t.cancel()
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("intiface disconnected"))
                self._pending.clear()
                if self.active and not self.active.task.done():
                    self.active.task.cancel()
                if was_connected:
                    log.warning("toy: disconnected from %s", self.url)
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _reader(self, ws) -> None:
        async for raw in ws:
            try:
                msgs = json.loads(raw)
            except ValueError:
                log.warning("toy: unparseable message %r", raw[:200])
                continue
            for msg in msgs if isinstance(msgs, list) else [msgs]:
                for name, body in msg.items():
                    self._handle(name, body or {})

    def _handle(self, name: str, body: dict) -> None:
        mid = body.get("Id")
        if mid in self._pending:
            fut = self._pending.pop(mid)
            if fut.done():
                return
            if name == "Error":
                fut.set_exception(RuntimeError(f"Intiface error {body.get('ErrorCode')}: {body.get('ErrorMessage')}"))
            else:
                fut.set_result(body)
            return
        if name == "DeviceAdded":
            self._add_device(body)
            log.info("toy: device added: %s", body.get("DeviceName"))
        elif name == "DeviceRemoved":
            d = self.devices.pop(int(body.get("DeviceIndex", -1)), None)
            if d:
                log.info("toy: device removed: %s", d.name)
        # ScanningFinished, Ok/Error for unknown ids etc. are ignored.

    def _set_devices(self, devices: list[dict]) -> None:
        self.devices.clear()
        for d in devices:
            self._add_device(d)
        log.info("toy: %d device(s): %s", len(self.devices), ", ".join(d.name for d in self.devices.values()) or "-")

    def _add_device(self, d: dict) -> None:
        idx = int(d["DeviceIndex"])
        dev = Device(index=idx, name=d.get("DeviceDisplayName") or d.get("DeviceName") or f"device {idx}")
        for i, feat in enumerate((d.get("DeviceMessages") or {}).get("ScalarCmd") or []):
            if feat.get("ActuatorType") == "Vibrate":
                dev.vibrators.append((i, int(feat.get("StepCount") or 20)))
        self.devices[idx] = dev

    async def _pinger(self, max_ping_ms: int) -> None:
        interval = max(max_ping_ms / 2000.0, 0.5)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._call("Ping", {})
            except Exception:
                return

    async def _call(self, name: str, payload: dict) -> dict:
        ws = self.ws
        if ws is None:
            raise ConnectionError("intiface not connected")
        mid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await ws.send(json.dumps([{name: {"Id": mid, **payload}}]))
            return await asyncio.wait_for(fut, CALL_TIMEOUT)
        finally:
            self._pending.pop(mid, None)

    # -- commands
    async def scan(self) -> None:
        await self._call("StartScanning", {})

    async def set_level(self, level: float) -> None:
        """Send one absolute level (0..1) to every vibrator, quantised to each device's step count."""
        level = max(0.0, min(1.0, level))
        for d in self.vibrating_devices():
            scalars = [{"Index": i, "Scalar": round(level * steps) / steps, "ActuatorType": "Vibrate"} for i, steps in d.vibrators]
            await self._call("ScalarCmd", {"DeviceIndex": d.index, "Scalars": scalars})

    async def stop_pattern(self) -> None:
        a = self.active
        self.active = None
        if a and not a.task.done():
            a.task.cancel()
            try:
                await a.task
            except (asyncio.CancelledError, Exception):
                pass
        if self.connected:
            try:
                await self._call("StopAllDevices", {})
            except Exception as e:
                log.info("toy: StopAllDevices: %s", e)

    async def play(self, pattern: str, intensity: int, duration_s: float) -> dict:
        """Start (or replace) a pattern. Returns the clamped values actually used."""
        if pattern not in PATTERNS:
            raise ValueError(f"unknown pattern '{pattern}'")
        s = get_settings()
        intensity = int(max(0, min(100, intensity)))
        cap = int(round(s.toy_max_intensity * 100))
        intensity = min(intensity, cap)
        duration_s = float(max(1.0, min(duration_s, s.toy_max_duration_s)))
        if not self.connected:
            raise ConnectionError(self.error or "intiface not connected")
        if not self.vibrating_devices():
            raise LookupError("no vibrating device connected")

        await self.stop_pattern()
        loop = asyncio.get_running_loop()
        if intensity == 0:
            return {"pattern": pattern, "intensity": 0, "duration_s": 0}
        fn = make_pattern(pattern, seed=random.randrange(1 << 30))
        task = loop.create_task(self._run(fn, intensity / 100.0, duration_s), name="toy-pattern")
        self.active = Active(pattern, intensity, duration_s, loop.time(), task)
        return {"pattern": pattern, "intensity": intensity, "duration_s": round(duration_s)}

    async def _run(self, fn: Callable[[float], float], scale: float, duration_s: float) -> None:
        loop = asyncio.get_running_loop()
        dt = 1.0 / (self._rate_hz or get_settings().toy_rate_hz)
        steps = min((st for d in self.vibrating_devices() for _, st in d.vibrators), default=20)
        t0 = loop.time()
        last = None
        try:
            while True:
                t = loop.time() - t0
                if t >= duration_s:
                    break
                q = round(scale * fn(t) * steps)
                if q != last:
                    await self.set_level(q / steps)
                    last = q
                await asyncio.sleep(dt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log.warning("toy: pattern aborted: %s", self.error)
        finally:
            try:
                if self.connected:
                    await asyncio.shield(self.set_level(0.0))
            except Exception:
                pass
            if self.active and self.active.task is asyncio.current_task():
                self.active = None


bridge = ToyBridge()


def start() -> None:
    bridge.start()


async def stop() -> None:
    await bridge.stop()
