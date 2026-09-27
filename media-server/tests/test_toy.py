"""Toy bridge: pattern shapes, Buttplug handshake/ScalarCmd against a fake Intiface, and the /api/toy route."""

import asyncio
import json

from fastapi.testclient import TestClient

from app import toy


def test_patterns_bounded_and_repeating():
    for name in toy.PATTERNS:
        f = toy.make_pattern(name, seed=7)
        vals = [f(t / 10) for t in range(0, 1800)]  # 0..180 s
        assert all(0.0 <= v <= 1.0 for v in vals), name
        assert max(vals) > 0.5, name
    periods = {"pulse": 1.0, "wave": 4.0, "ramp": 6.0, "heartbeat": 1.2, "tease": 24.0}
    for name, p in periods.items():
        f = toy.make_pattern(name, 0)
        assert all(abs(f(t) - f(t + p)) < 1e-9 for t in (0.1, 0.7, 2.3, 5.9)), name
    r1, r2 = toy.make_pattern("random", 1), toy.make_pattern("random", 2)
    assert [round(r1(t), 3) for t in range(10)] != [round(r2(t), 3) for t in range(10)]
    assert abs(r1(0.75) - (r1(0) + r1(1.5)) / 2) < 1e-9  # linear interpolation between targets


GUSH = {
    "DeviceName": "Lovense Gush 2",
    "DeviceIndex": 0,
    "DeviceMessages": {
        "ScalarCmd": [{"StepCount": 20, "FeatureDescriptor": "Vibrator", "ActuatorType": "Vibrate"}],
        "StopDeviceCmd": {},
    },
}


class FakeIntiface:
    """Minimal Buttplug v3 server: handshake, device list, records every ScalarCmd level."""

    def __init__(self):
        self.levels: list[float] = []
        self.stopped = 0

    async def handler(self, ws):
        async for raw in ws:
            for msg in json.loads(raw):
                ((name, body),) = msg.items()
                mid = body["Id"]
                if name == "RequestServerInfo":
                    reply = {"ServerInfo": {"Id": mid, "ServerName": "fake", "MessageVersion": 3, "MaxPingTime": 0}}
                elif name == "RequestDeviceList":
                    reply = {"DeviceList": {"Id": mid, "Devices": [GUSH]}}
                elif name == "ScalarCmd":
                    self.levels.append(body["Scalars"][0]["Scalar"])
                    reply = {"Ok": {"Id": mid}}
                elif name == "StopAllDevices":
                    self.stopped += 1
                    reply = {"Ok": {"Id": mid}}
                else:
                    reply = {"Ok": {"Id": mid}}
                await ws.send(json.dumps([reply]))


def _admin_cookie():
    from app.routes.admin import _session_token

    return _session_token()


async def _wait(cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting")


def test_bridge_runs_pattern_and_stops(env):
    from websockets.asyncio.server import serve

    async def main():
        fake = FakeIntiface()
        async with serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            b = toy.ToyBridge(url=f"ws://127.0.0.1:{port}", rate_hz=40)
            b.start()
            await _wait(lambda: b.connected and b.vibrating_devices())
            assert b.status()["devices"] == ["Lovense Gush 2"]

            used = await b.play("pulse", 100, 1.0)
            assert used == {"pattern": "pulse", "intensity": 100, "duration_s": 1}
            assert b.status()["active"]["pattern"] == "pulse"
            await _wait(lambda: b.active is None, timeout=3)
            assert fake.levels[0] == 1.0 and fake.levels[-1] == 0.0
            assert 0.0 in fake.levels[:-1]  # the off half of the pulse was sent
            assert len(fake.levels) <= 4  # deduped: only sends when the quantised level changes

            fake.levels.clear()
            await b.play("steady", 55, 60)
            await _wait(lambda: fake.levels)
            assert fake.levels == [0.55]
            await b.stop_pattern()
            assert b.active is None and fake.stopped >= 1 and fake.levels[-1] == 0.0

            # Server-side clamps: intensity to TOY_MAX_INTENSITY, duration to TOY_MAX_DURATION_S.
            used = await b.play("wave", 100, 9999)
            assert used["duration_s"] == 180 and used["intensity"] == 100
            await b.stop()
            assert not b.connected

    asyncio.run(main())


def test_bridge_reconnects_when_server_goes_away(env):
    from websockets.asyncio.server import serve

    async def main():
        fake = FakeIntiface()
        server = await serve(fake.handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        b = toy.ToyBridge(url=f"ws://127.0.0.1:{port}")
        b.start()
        await _wait(lambda: b.connected)
        await b.play("steady", 50, 60)
        server.close()
        await server.wait_closed()
        await _wait(lambda: not b.connected)
        assert b.active is None or b.active.task.done()  # the pattern died with the connection
        assert b.status()["devices"] == []
        await b.stop()

    asyncio.run(main())


def test_api_toy_unconfigured_and_stub(env, monkeypatch):
    from app import config
    from app.main import app

    hdr = {"Authorization": "Bearer tool-key"}
    with TestClient(app) as c:
        assert c.post("/api/toy", json={"pattern": "wave"}).status_code == 401
        r = c.post("/api/toy", json={"pattern": "wave"}, headers=hdr)
        assert r.json()["status"] == "unavailable"

        monkeypatch.setenv("INTIFACE_URL", "ws://127.0.0.1:1")
        config.get_settings.cache_clear()
        assert c.post("/api/toy", json={"pattern": "nope"}, headers=hdr).status_code == 422
        assert c.post("/api/toy", json={"pattern": "wave"}, headers=hdr).json()["status"] == "unavailable"  # not connected

        calls = []

        async def fake_play(pattern, intensity, duration_s):
            calls.append((pattern, intensity, duration_s))
            return {"pattern": pattern, "intensity": intensity, "duration_s": duration_s}

        monkeypatch.setattr(toy.bridge, "connected", True)
        monkeypatch.setattr(toy.bridge, "vibrating_devices", lambda: [toy.Device(0, "Gush", [(0, 20)])])
        monkeypatch.setattr(toy.bridge, "play", fake_play)
        r = c.post("/api/toy", json={"pattern": "pulse", "intensity": 35, "duration_s": 45}, headers=hdr)
        assert r.json()["status"] == "playing" and calls == [("pulse", 35, 45)]
        assert c.get("/api/toy", headers=hdr).json()["devices"] == ["Gush"]

        c.cookies.set("rpm_session", _admin_cookie())
        assert "Gush" in c.get("/toy").text
