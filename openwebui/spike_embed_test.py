"""
title: RP Media Spike (embed test)
description: Step-0 spike. Returns a hardcoded image + video embed to verify rendering on this Open WebUI version.
version: 0.1.0
"""

from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        image_url: str = Field(
            default="https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
            description="HTTPS image URL to embed (point this at media.<domain> to test the proxy).",
        )
        video_url: str = Field(
            default="https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4",
            description="HTTPS mp4 URL to embed.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def send_test_media(self, kind: str = "image") -> tuple:
        """
        Send a test photo or video into the chat. Use when the user asks for a test picture or test video.
        :param kind: "image" or "video"
        """
        if kind == "video":
            body = f'<video src="{self.valves.video_url}" controls playsinline loop muted autoplay></video>'
        else:
            body = f'<img src="{self.valves.image_url}" alt="test">'
        html = f"""<!doctype html><html><head><style>
html,body{{margin:0;padding:0;background:transparent}}
img,video{{display:block;max-width:100%;max-height:480px;border-radius:12px}}
</style></head><body>{body}
<script>
function h(){{parent.postMessage({{type:'iframe:height',height:document.documentElement.scrollHeight}},'*')}}
window.addEventListener('load',h);new ResizeObserver(h).observe(document.body);
document.querySelectorAll('img,video').forEach(e=>{{e.addEventListener('load',h);e.addEventListener('loadedmetadata',h)}});
</script></body></html>"""
        return (
            HTMLResponse(content=html, headers={"Content-Disposition": "inline"}),
            f"You just sent a test {kind}. Tell the user it was sent.",
        )
