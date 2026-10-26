"""chart-to-orc web app. Draw → compile → provision → prompt blind agents."""

from __future__ import annotations

import base64
import json
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Config
from app.llm import LLM
from app.parser import ParseError, parse_drawing, parse_spec

ROOT = Path(__file__).resolve().parent.parent
cfg = Config.load()

app = FastAPI(title="chart-to-orc")
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

MAX_DRAWING_BYTES = 10 * 1024 * 1024
MEDIA_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def graph_dir(name: str) -> Path:
    return cfg.data_dir / name


def load_status(name: str) -> dict:
    p = graph_dir(name) / "status.json"
    return json.loads(p.read_text()) if p.is_file() else {}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/health")
def health() -> dict:
    chat = LLM(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model)
    vision = LLM(cfg.vision_base_url, cfg.vision_api_key, cfg.vision_model)
    return {
        "ok": True,
        "llm_ready": chat.ready,
        "vision_ready": vision.ready,
        "manus_ready": bool(cfg.manus_api_key),
    }


@app.post("/api/graphs")
async def create_graph(
    image: UploadFile | None = File(None), spec: str = Form("")
) -> dict:
    if image is not None and image.filename:
        raw = await image.read()
        if len(raw) > MAX_DRAWING_BYTES:
            raise HTTPException(413, "drawing too large (max 10 MiB)")
        media = (image.content_type or "image/png").split(";")[0]
        vision = LLM(cfg.vision_base_url, cfg.vision_api_key, cfg.vision_model)
        try:
            graph = parse_drawing(vision, base64.b64encode(raw).decode(), media)
        except ParseError as e:
            raise HTTPException(422, str(e))
        drawing = raw
    elif spec.strip():
        try:
            graph = parse_spec(spec)
        except ParseError as e:
            raise HTTPException(422, str(e))
        drawing = None
    else:
        raise HTTPException(400, "upload a drawing or paste a spec")

    d = graph_dir(graph.name)
    if d.exists():
        raise HTTPException(409, f"graph {graph.name!r} already exists")
    d.mkdir(parents=True)
    (d / "graph.json").write_text(graph.model_dump_json(indent=2))
    if drawing is not None:
        ext = MEDIA_EXT.get(media, ".png")
        (d / f"drawing{ext}").write_bytes(drawing)
    (d / "status.json").write_text(
        json.dumps({"state": "pending", "created": time.time()}, indent=2)
    )
    return {**graph.model_dump(), "status": "pending"}


@app.get("/api/graphs")
def list_graphs() -> dict:
    if not cfg.data_dir.is_dir():
        return {"graphs": []}
    out = []
    for d in sorted(cfg.data_dir.iterdir()):
        if not d.is_dir():
            continue
        try:
            graph = json.loads((d / "graph.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append(
            {
                "name": graph.get("name", d.name),
                "agents": len(graph.get("agents", [])),
                "channels": len(graph.get("channels", [])),
                "status": load_status(d.name).get("state", "pending"),
            }
        )
    return {"graphs": out}


@app.get("/api/graphs/{name}")
def get_graph(name: str) -> dict:
    p = graph_dir(name) / "graph.json"
    if not p.is_file():
        raise HTTPException(404, "no such graph")
    graph = json.loads(p.read_text())
    return {**graph, "status": load_status(name).get("state", "pending")}


@app.delete("/api/graphs/{name}")
def delete_graph(name: str) -> dict:
    d = graph_dir(name)
    if not d.is_dir():
        raise HTTPException(404, "no such graph")
    if load_status(name).get("state") == "live":
        raise HTTPException(409, "graph is live; teardown not implemented yet")
    shutil.rmtree(d)
    return {"deleted": name}
