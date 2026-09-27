# RP Media for Open WebUI

Lets a roleplay character send **real photos and videos** into an Open WebUI chat, drawn from a pre-tagged
per-character library instead of generated on the fly.

```
 Open WebUI (Qwen, native tool calling)
   └─ Tool: send_media(description, media_type, user_requested)
        │  POST /api/send  (server→server, internal URL, Bearer TOOL_API_KEY)
        ▼
 RP Media server (FastAPI + SQLite + sqlite-vec)  ──►  Ollama (vision tagger + embeddings)
        │  returns a signed https://media.<domain>/m/<id>/full?sig=… URL + caption
        ▼
 Tool returns an HTML embed (<img>/<video>) + "you sent a photo showing …" context for the model
 Browser loads the media from media.<domain> (reverse proxy, LAN-only access list)
```

- **Libraries**: one per character, linked to one or more Open WebUI model/preset IDs.
- **Auto-tagging**: the Ollama vision model writes a caption, rating (sfw/suggestive/explicit) and tags
  (outfit, location, activity, mood, framing, time of day, extra). For videos it looks at 4 keyframes.
- **Hybrid search**: embedding similarity on caption+tags (75%) plus tag/caption word overlap (25%). Items
  are never repeated within a chat, the library's rating cap is respected, and nothing is sent below `MIN_SCORE`.
- **Cooldown**: spontaneous sends need N user turns between them. Explicit requests always go through.
- **Signed, non-expiring URLs**: old chats keep working, but the library can't be enumerated.

## 1. Deploy the media server (Portainer)

1. Create datasets, e.g. `/mnt/tank/apps/rp-media/data` (app state) and optionally `/mnt/tank/media/rp`
   (existing media to import in place). Adjust the volume paths in `docker-compose.yml`.
2. In Portainer: **Stacks → Add stack → Web editor**, paste `docker-compose.yml`, and add the variables from
   `.env.example`. The image `ghcr.io/spooknik/openwebui-rp-tool:latest` is built by GitHub Actions on every push
   to `main`. The workflow runs the tests first, and tags `v1.2.3` also publish `:1.2.3` / `:1.2`.
   - To update: in Portainer, open the stack → **Update the stack** with *Re-pull image* enabled.
   - If the repo/package is private, add GHCR under **Registries** (`ghcr.io`, your GitHub username, and a PAT
     with `read:packages`). Or make the package public: GitHub → Packages → openwebui-rp-tool →
     Package settings → Change visibility.
3. Models on your Ollama server:
   - `VISION_MODEL`: the exact tag from `ollama list` for your Qwen VL model.
   - `EMBED_MODEL`: e.g. `ollama pull nomic-embed-text` (small and fast). `qwen3-embedding` also works;
     task prefixes are auto-detected. Changing the embedding model later triggers a full re-embed automatically.

### Reverse proxy (HTTPS subdomain, LAN only)
Open WebUI is served over HTTPS, so media has to be HTTPS too, or the browser blocks it as mixed content.
- Add a proxy host `media.<domain>` → `http://<truenas-ip>:8090` using your existing (wildcard) certificate.
- Attach an **access list that allows only your LAN subnet** (e.g. `192.168.1.0/24`). In Nginx Proxy Manager
  that's *Access Lists → allow 192.168.1.0/24, deny all*.
- Set `PUBLIC_BASE_URL=https://media.<domain>`.
- Enable large uploads if you'll upload videos through the UI. In NPM → Advanced: `client_max_body_size 2g;`.

The admin UI is at `https://media.<domain>/` (log in with `ADMIN_API_KEY`).

## 2. Build a library

1. **New library**: name, character notes (short and visual, with pronouns, for example "Luna, she/her, long
   silver hair, freckles"), rating cap, cooldown, and the **Open WebUI model IDs** that should use it.
2. **Upload** by drag-and-drop, or use **Import from server folder** (indexes `/media/...` in place, read-only).
3. The queue creates thumbnails and transcodes, then tags on the GPU. **Pause** the GPU work while you're
   chatting if the tagger and the chat model don't both fit in VRAM.
4. Fix any caption/tags by hand. Edited items are marked *edited* and skipped by bulk re-tag. Use the
   ✓/⊘ toggle on a card to exclude an item from sending.
5. Use **Test search** with the kinds of descriptions the model will send, and tune `MIN_SCORE`
   (faded rows fall below the threshold).

## 3. Install the Open WebUI tool

1. **Workspace → Tools → +**, paste `openwebui/rp_media_tool.py`, and save.
2. Tool **Valves**: `api_base_url` = the address the OWUI *server* can reach (e.g. `http://192.168.1.10:8090`),
   and `api_key` = `TOOL_API_KEY`.
3. For each character preset (**Workspace → Models**):
   - enable the **RP Media** tool,
   - **Advanced Params → Function Calling → Native**,
   - append the snippet from `openwebui/character_prompt_template.md` to the system prompt,
   - copy the preset's **Model ID** into the library's linked model IDs.
4. Embeds render in a sandboxed iframe. Height auto-sizes through `postMessage`, which works with the
   default *allowSameOrigin = off*.

> **First-time check**: `openwebui/spike_embed_test.py` is a zero-dependency tool that embeds a hardcoded
> image and video. Install it, ask a model to "send a test video", and confirm three things: the embed renders,
> it plays, and it survives a page reload. Then point its valves at `https://media.<domain>/...` URLs to test
> the proxy.

## 4. Toy control (optional)

Lets a character drive a vibrator (tested target: Lovense Gush 2) through [Intiface Central](https://intiface.com/central/)
running on the PC you chat from. The media server keeps one Buttplug websocket open to Intiface and runs repeating
patterns on its own timer, because Intiface stops every device the moment a client disconnects. The Open WebUI tool
just posts "play this pattern at this strength for this long" and returns immediately.

```
 Open WebUI tool: control_toy(action, pattern, intensity, duration_s)
        │  POST /api/toy  (Bearer TOOL_API_KEY)
        ▼
 RP Media server  ── toy bridge (persistent Buttplug v3 websocket) ──►  Intiface Central on your PC  ──►  toy
```

1. **Intiface Central** (on the chatting PC): Settings → Server → enable **Listen on all interfaces**, keep port
   `12345`, then start the server and connect the toy. Add a Windows Firewall inbound rule for TCP 12345 limited to
   the TrueNAS IP so only the media server can reach it.
2. **Stack variables**: `INTIFACE_URL=ws://<pc-ip>:12345` (e.g. `ws://10.10.10.174:12345`). Optional caps:
   `TOY_MAX_DURATION_S` (default 180; the bridge stops the toy by itself after this, whatever the model asked for)
   and `TOY_MAX_INTENSITY` (0..1). Update the stack; the dashboard's **Toy** panel shows the connection, the
   devices Intiface reports, what is playing, and has **Stop**, a 5-second **Test** and **Scan** buttons.
3. **Open WebUI**: Workspace → Tools → +, paste `openwebui/rp_toy_tool.py`, set the same `api_base_url` and
   `api_key` valves as the media tool. Enable *RP Toy* on the character presets that may use it and append
   `openwebui/toy_prompt_template.md` to their system prompt.

Patterns all repeat until the time is up: `steady`, `pulse` (on/off each second), `wave` (4 s rise and fall),
`ramp` (6 s climb then drop), `heartbeat`, `tease` (18 s build, 6 s back-off) and `random` (smoothly wandering).
A new call replaces the running pattern, `action="stop"` switches it off, and the bridge sends level 0 and
`StopAllDevices` when a run ends, is replaced, or the connection drops. Intiface runs on the PC and needs the
toy paired to it, so nothing here touches Bluetooth.

## Development

```bash
cd media-server
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt pytest   # (bin/ on Linux)
.venv/Scripts/python -m pytest -q          # Ollama is faked; ffmpeg required for the video test
DATA_DIR=./data IMPORT_ROOT=./import OLLAMA_URL=http://<ollama>:11434 \
  .venv/Scripts/python -m uvicorn app.main:app --port 8090 --reload
```

Layout: `app/ingest.py` (upload/import, thumbnails, keyframes, transcode), `app/tagger.py` (job workers),
`app/ollama.py` (tagging prompt + JSON schema, embeddings), `app/search.py` (hybrid retrieval, cooldown),
`app/routes/` (tool API, signed media, admin UI).

### API (Bearer `TOOL_API_KEY`)
| Endpoint | Purpose |
|---|---|
| `POST /api/send` | `{description, model_id \| library, media_type, user_requested, chat_id, message_id, user_turn}` → `{status: sent\|cooldown\|no_match, media}` |
| `POST /api/search` | Ranked hits with score breakdown (debugging) |
| `GET /api/libraries` | Libraries, linked model IDs, ready counts |
| `GET /m/{id}/{full\|thumb\|poster}?sig=` | Signed media (no auth; Range supported) |
