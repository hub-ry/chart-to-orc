"""Small per-case sandbox; no provider config, network, broker or Docker access."""
from __future__ import annotations

import importlib.util
import json
import os
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Config

ROOT = Path(__file__).resolve().parents[1]


def forbidden(*args, **kwargs):
    raise AssertionError("offline sandbox blocked external I/O")


async def forbidden_async(*args, **kwargs):
    forbidden()


def captured_environment(command):
    """Read only temporary fixture files named by captured Docker argv."""
    environment = {}
    for index, argument in enumerate(command[:-1]):
        if argument == "--env-file":
            environment.update(line.split("=", 1)
                               for line in Path(command[index + 1]).read_text().splitlines())
        elif argument == "-e":
            name, _, value = command[index + 1].partition("=")
            environment[name] = value
    return environment


class Broker:
    def __init__(self):
        self.messages = []

    async def publish(self, subject, data):
        self.messages.append((subject, json.loads(data)))

    async def close(self):
        pass


class Context:
    def __init__(self, path: Path):
        self.path = path
        self.commands = []
        self.stack = ExitStack()
        self._main = None

    def __enter__(self):
        self.stack.__enter__()
        # Clear inherited credentials without ever loading the user's env file.
        self.stack.enter_context(patch.dict(os.environ, {"PATH": os.defpath, "LANG": "C.UTF-8"}, clear=True))
        self.patch("app.config._load_env_file", forbidden)
        self.patch("app.config.Config.load", lambda *args, **kwargs: self.config())
        self.patch("socket.socket.connect", forbidden)
        self.patch("socket.socket.connect_ex", forbidden)
        self.patch("socket.create_connection", forbidden)
        self.patch("socket.getaddrinfo", forbidden)
        self.patch("nats.connect", forbidden_async)
        self.patch("app.provision._run", forbidden)
        self.patch("app.provision.free_port", lambda: 12345)
        self.patch("app.fabric._connections", {})
        self.patch("app.fabric._observers", {})
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def patch(self, target, value):
        return self.stack.enter_context(patch(target, value))

    @staticmethod
    def require(condition, reason):
        if not condition:
            raise AssertionError(reason)

    def config(self, **overrides):
        cfg = Config(
            data_dir=self.path,
            llm_base_url="https://text.invalid/v1", llm_api_key="fake-provider-key",
            llm_model="fixed-text", vision_base_url="https://vision.invalid/v1",
            vision_api_key="fake-vision-key", vision_model="fixed-vision",
            agent_image="eval-agent", nats_image="eval-nats", api_token="fake-api-token",
        )
        return replace(cfg, **overrides)

    def _load(self, name, path):
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def agent(self, name="a"):
        agent = self._load(f"eval_agent_{name}", "agentd/agentd.py")
        workspace = self.path / name
        workspace.mkdir(exist_ok=True)
        agent.WORKSPACE = str(workspace)
        agent.AGENT_ID = name
        agent.AGENT_NAME = name
        agent.GRAPH = "g"
        agent.TOOLS_LIST = ["workspace"]
        agent.NC = Broker()
        return agent

    def main(self):
        if self._main is None:
            self._main = self._load("eval_main", "app/main.py")
        return self._main

    def provision(self, graph, cfg=None):
        from app import provision

        cfg = cfg or self.config()

        def capture(*args, **kwargs):
            self.commands.append(args)
            return "existing-image" if args[:3] == ("docker", "image", "inspect") else ""

        with patch.object(provision, "_run", capture):
            return provision.provision(graph, cfg.data_dir, cfg.agent_image,
                                       cfg.nats_image, ROOT / "agentd", cfg)
