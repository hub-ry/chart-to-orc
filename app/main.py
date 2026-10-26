"""chart-to-orc web app. Draw → compile → accept → prompt blind agents."""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import json
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app import fabric
from app.config import Config
from app.llm import LLM
from app.parser import ParseError, parse_drawing, parse_spec
from app.provision import ProvisionError, provision, teardown
from app.schema import Graph, SLUG
from app.proxy import running_proxy

ROOT = Path(__file__).resolve().parent.parent
cfg = Config.load()
_lifecycle_lock = asyncio.Lock()
logger = logging.getLogger(__name__)


async def cleanup_expired_graphs(config: Config) -> None:
    """Expire live resources and recover provisioning abandoned by a crash."""
    if not config.data_dir.is_dir():
        return
    async with _lifecycle_lock:
        for d in config.data_dir.iterdir():
            if not d.is_dir() or not SLUG.fullmatch(d.name):
                continue
            try:
                status_path = d / "status.json"
                status = json.loads(status_path.read_text())
                abandoned = (status.get("state") == "provisioning"
                             and time.time() - status_path.stat().st_mtime > 300)
                expired = (status.get("state") == "live"
                           and config.graph_ttl_seconds > 0
                           and time.time() - status["provisioned_at"] > config.graph_ttl_seconds)
                if not (abandoned or expired):
                    continue
                graph = Graph.model_validate_json((d / "graph.json").read_text())
                await fabric.stop_observer(d.name)
                await asyncio.to_thread(teardown, graph, config.data_dir, rm_data=False)
                if abandoned:
                    status.update(state="failed", error="provisioning interrupted")
                else:
                    status.update(state="expired", expired_at=time.time())
                (d / "status.json").write_text(json.dumps(status, indent=2))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                logger.exception("Graph cleanup failed for %s", d.name)


async def _graph_gc() -> None:
    while True:
        await cleanup_expired_graphs(cfg)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with running_proxy(cfg):
        # resume observing every live graph after a restart
        if cfg.data_dir.is_dir():
            for d in cfg.data_dir.iterdir():
                if not d.is_dir():
                    continue
                try:
                    status = json.loads((d / "status.json").read_text())
                    if status.get("state") == "live":
                        graph = Graph.model_validate(json.loads((d / "graph.json").read_text()))
                        await fabric.start_observer(d, d.name, {c.id: c.from_ for c in graph.channels})
                except (OSError, json.JSONDecodeError, RuntimeError, ValidationError):
                    continue
        gc = asyncio.create_task(_graph_gc())
        try:
            yield
        finally:
            gc.cancel()
            with suppress(asyncio.CancelledError):
                await gc


app = FastAPI(title="chart-to-orc", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.middleware("http")
async def authenticate_api(request: Request, call_next):
    if request.url.path == "/api" or request.url.path.startswith("/api/"):
        if cfg.api_token:
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            authorized = scheme.lower() == "bearer" and hmac.compare_digest(
                token.encode(), cfg.api_token.encode()
            )
        else:
            authorized = cfg.allow_no_auth
        if not authorized:
            return JSONResponse(
                {"detail": "API authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)

MAX_DRAWING_BYTES = 10 * 1024 * 1024
TRAFFIC_READ_BYTES = 1024 * 1024
MEDIA_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def validate_slug(value: str) -> str:
    if not SLUG.fullmatch(value):
        raise HTTPException(400, "invalid slug")
    return value


def graph_dir(name: str) -> Path:
    return cfg.data_dir / validate_slug(name)


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
    async with _lifecycle_lock:
        graph = Graph.model_validate(load_graph(name))
        if load_status(name).get("state") != "live":
            live = sum(
                load_status(d.name).get("state") == "live"
                for d in cfg.data_dir.iterdir() if d.is_dir() and SLUG.fullmatch(d.name)
            )
            if live >= cfg.max_live_graphs:
                raise HTTPException(429, "live graph limit reached")
        await fabric.stop_observer(name)  # converge: restart cleanly if already live
        try:
            out = await asyncio.to_thread(
                provision, graph, cfg.data_dir, cfg.agent_image, cfg.nats_image,
                ROOT / "agentd", cfg,
            )
        except ProvisionError as e:
            raise HTTPException(500, str(e))
        d = graph_dir(name)
        try:
            await fabric.start_observer(d, name, {c.id: c.from_ for c in graph.channels})
        except RuntimeError as e:
            await fabric.stop_observer(name)
            await asyncio.to_thread(teardown, graph, cfg.data_dir, rm_data=False)
            (d / "status.json").write_text(json.dumps({
                "state": "failed", "error": "observer connection failed",
            }))
            raise HTTPException(503, "observer cannot reach the broker; graph resources removed")
        return {"status": "live", **out}


@app.get("/api/graphs/{name}/agents/{aid}")
def agent_file(name: str, aid: str) -> Response:
    validate_slug(aid)
    load_graph(name)
    p = graph_dir(name) / "agents" / f"{aid}.md"
    if not p.is_file():
        raise HTTPException(404, "agent has no markdown file (is the graph live?)")
    return Response(p.read_text(), media_type="text/markdown")


@app.post("/api/graphs/{name}/agents/{aid}/prompt")
async def prompt_agent(name: str, aid: str, body: dict) -> dict:
    validate_slug(aid)
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
        "hops": 0,
    }
    msg["conv"] = msg["msg_id"]
    await nc.publish(f"orc.{name}.in.{aid}", json.dumps(msg).encode())
    p = d / "agents" / f"{aid}.md"
    try:
        fabric.append_bounded(p, f"### {msg['ts']} you\n{text}\n\n")
    except OSError:
        pass
    return {"ok": True, "msg_id": msg["msg_id"]}


@app.get("/api/graphs/{name}/result")
async def graph_result(name: str, timeout: float = 30, conv: str | None = None):
    load_graph(name)
    if not 0 <= timeout <= 300:
        raise HTTPException(400, "timeout must be between 0 and 300 seconds")
    try:
        result = await fabric.wait_result(name, conv, timeout)
    except asyncio.TimeoutError:
        return JSONResponse({"status": "timeout"}, status_code=408)
    return {"status": "result", "result": result}


@app.get("/api/graphs/{name}/traffic")
def traffic(name: str, after: int = 0) -> dict:
    p = graph_dir(name) / "traffic.jsonl"
    if not p.is_file():
        return {"lines": [], "next": 0}
    # Cursors are byte offsets. Read only a bounded tail, skipping any cut row.
    with p.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        cursor = after if 0 <= after <= size else 0
        start = max(cursor, size - TRAFFIC_READ_BYTES)
        f.seek(start)
        raw = f.read(min(TRAFFIC_READ_BYTES, size - start))
    end = raw.rfind(b"\n") + 1
    next_cursor = start + end
    lines = raw[:end].splitlines()
    if start > cursor:
        lines = lines[1:]
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return {"lines": parsed, "next": next_cursor}


@app.delete("/api/graphs/{name}")
async def delete_graph(name: str) -> dict:
    async with _lifecycle_lock:
        graph = load_graph(name)
        await fabric.stop_observer(name)
        await asyncio.to_thread(teardown, Graph.model_validate(graph), cfg.data_dir)
        return {"deleted": name}
