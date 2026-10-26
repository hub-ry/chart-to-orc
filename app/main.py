"""chart-to-orc web app. Draw → compile → accept → prompt blind agents."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import fabric
from app.config import Config
from app.llm import LLM
from app.parser import ParseError, parse_drawing, parse_spec
from app.provision import ProvisionError, provision, teardown
from app.schema import Graph

ROOT = Path(__file__).resolve().parent.parent
cfg = Config.load()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # resume observing every live graph after a restart
    if cfg.data_dir.is_dir():
        for d in cfg.data_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                status = json.loads((d / "status.json").read_text())
                if status.get("state") == "live":
                    await fabric.start_observer(d, d.name)
            except (OSError, json.JSONDecodeError, RuntimeError):
                continue
    yield


app = FastAPI(title="chart-to-orc", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

MAX_DRAWING_BYTES = 10 * 1024 * 1024
MEDIA_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def graph_dir(name: str) -> Path:
    return cfg.data_dir / name


def load_status(name: str) -> dict:
    p = graph_dir(name) / "status.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def load_graph(name: str) -> dict:
    p = graph_dir(name) / "graph.json"
    if not p.is_file():
        raise HTTPException(404, "no such graph")
    return json.loads(p.read_text())


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
    (d / "graph.json").write_text(graph.model_dump_json(indent=2, by_alias=True))
    if drawing is not None:
        ext = MEDIA_EXT.get(media, ".png")
        (d / f"drawing{ext}").write_bytes(drawing)
    (d / "status.json").write_text(
        json.dumps({"state": "pending", "created": time.time()}, indent=2)
    )
    return {**graph.model_dump(by_alias=True), "status": "pending"}


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
    graph = load_graph(name)
    return {**graph, "status": load_status(name).get("state", "pending")}


@app.post("/api/graphs/{name}/accept")
async def accept_graph(name: str) -> dict:
    graph = Graph.model_validate(load_graph(name))
    await fabric.stop_observer(name)  # converge: restart cleanly if already live
    llm_env = {
        "base_url": cfg.llm_base_url,
        "api_key": cfg.llm_api_key,
        "model": cfg.llm_model,
    }
    try:
        out = await asyncio.to_thread(
            provision, graph, cfg.data_dir, cfg.agent_image, cfg.nats_image,
            ROOT / "agentd", llm_env,
        )
    except ProvisionError as e:
        raise HTTPException(500, str(e))
    d = graph_dir(name)
    try:
        await fabric.start_observer(d, name)
    except RuntimeError as e:
        raise HTTPException(503, f"provisioned, but observer cannot reach the broker yet: {e}")
    return {"status": "live", **out}


@app.get("/api/graphs/{name}/agents/{aid}")
def agent_file(name: str, aid: str) -> Response:
    load_graph(name)
    p = graph_dir(name) / "agents" / f"{aid}.md"
    if not p.is_file():
        raise HTTPException(404, "agent has no markdown file (is the graph live?)")
    return Response(p.read_text(), media_type="text/markdown")


@app.post("/api/graphs/{name}/agents/{aid}/prompt")
async def prompt_agent(name: str, aid: str, body: dict) -> dict:
    graph = load_graph(name)
    if aid not in {a["id"] for a in graph["agents"]}:
        raise HTTPException(404, "no such agent")
    if load_status(name).get("state") != "live":
        raise HTTPException(409, "graph is not live")
    text = str(body.get("prompt", "")).strip()
    if not text:
        raise HTTPException(400, "prompt is empty")
    d = graph_dir(name)
    try:
        nc = await fabric.get_conn(d, name)
    except RuntimeError:
        raise HTTPException(503, "graph has no observer credentials (provisioning incomplete?)")
    msg = {
        "v": 1,
        "msg_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "src": "user",
        "ch": f"in.{aid}",
        "kind": "text",
        "body": {"text": text},
        "prev": None,
    }
    await nc.publish(f"orc.{name}.in.{aid}", json.dumps(msg).encode())
    p = d / "agents" / f"{aid}.md"
    try:
        with p.open("a") as f:
            f.write(f"### {msg['ts']} you\n{text}\n\n")
    except OSError:
        pass
    return {"ok": True, "msg_id": msg["msg_id"]}


@app.get("/api/graphs/{name}/traffic")
def traffic(name: str, after: int = 0) -> dict:
    p = graph_dir(name) / "traffic.jsonl"
    if not p.is_file():
        return {"lines": [], "next": 0}
    lines = p.read_text().splitlines()
    new = lines[after:]
    parsed = []
    for line in new:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"lines": parsed, "next": len(lines)}


@app.delete("/api/graphs/{name}")
async def delete_graph(name: str) -> dict:
    graph = load_graph(name)
    if load_status(name).get("state") == "live":
        await fabric.stop_observer(name)
        await asyncio.to_thread(teardown, Graph.model_validate(graph), cfg.data_dir)
    else:
        d = graph_dir(name)
        if d.is_dir():
            shutil.rmtree(d)
    return {"deleted": name}
