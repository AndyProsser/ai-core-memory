import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub import search_sync
from acm_hub.access import principal_for_user
from acm_hub.app import create_app
from acm_hub.config import Settings
from acm_hub.focus import build_focus
from acm_hub.models import Project, User
from acm_hub.plugins import registry, remote
from acm_hub.records import RecordIn, write_record

from .conftest import setup_admin
from .remote_support import SECRET, add_instance

EXAMPLE = Path(__file__).parents[1] / "examples" / "embedding-index" / "app.py"


def load_example(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("INDEX_PLUGIN_SECRET", SECRET)
    monkeypatch.setenv("INDEX_DB", str(tmp_path / "index.sqlite3"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location(f"embedding_example_{abs(hash(str(env)))}", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            return server, t
        time.sleep(0.05)
    raise RuntimeError("service didn't start")


# --- the default (hash) embedder -----------------------------------------------------------------------------------------------


def test_hash_embedder_ranks_by_shared_words_and_isolates_instances(monkeypatch, tmp_path):
    ex = load_example(monkeypatch, tmp_path)
    a, b = {"id": "inst-a"}, {"id": "inst-b"}
    docs = [
        {
            "id": "r1",
            "name": "webhook-retries",
            "description": "Retry webhooks with idempotency keys",
            "topics": ["payments"],
        },
        {"id": "r2", "name": "blog-theme", "description": "The blog uses a dark theme", "topics": []},
    ]
    assert ex.index(a, docs)["indexed"] == 2
    hits = ex.search(a, "retry webhook deliveries safely", 5)["results"]
    assert hits and hits[0]["id"] == "r1" and all(h["id"] != "r2" for h in hits)
    assert (
        ex.search(b, "retry webhook deliveries safely", 5)["results"] == []
    )  # another instance never sees these vectors
    ex.remove(a, ["r1"])
    assert ex.search(a, "retry webhook", 5)["results"] == []
    ex.index(a, docs)
    ex.reset(a)
    assert ex.search(a, "retry webhook", 5)["results"] == []


def test_the_example_search_service_speaks_the_signed_protocol(monkeypatch, tmp_path):
    import httpx

    from acm_hub.plugins import remote_sdk as sdk

    from .remote_support import asgi_transport

    ex = load_example(monkeypatch, tmp_path)
    t = asgi_transport(ex.app)
    body = json.dumps({"instance": {"id": "i"}, "query": "x", "limit": 5}).encode()
    ts = str(int(time.time()))
    ok = t.handle_request(
        httpx.Request(
            "POST",
            "http://svc/acm/v1/search",
            content=body,
            headers={"X-ACM-Timestamp": ts, "X-ACM-Signature": sdk.sign(SECRET, ts, body)},
        )
    )
    assert ok.status_code == 200
    bad = t.handle_request(
        httpx.Request(
            "POST",
            "http://svc/acm/v1/search",
            content=body,
            headers={"X-ACM-Timestamp": ts, "X-ACM-Signature": "sha256=00"},
        )
    )
    assert bad.status_code == 401  # an unsigned/forged call can't read or poison the index


# --- model-backed embedders, against local stand-ins -------------------------------------------------------------------------------


def fake_embedding_server(kind: str):
    """A stand-in for Ollama (/api/embeddings) or an OpenAI-compatible server (/v1/embeddings), with a tiny 'meaning'
    space: car/automobile/vehicle share a dimension, so they match even though they share no words."""
    seen: list[dict] = []
    groups = [{"car", "automobile", "vehicle", "truck"}, {"invoice", "billing", "payment"}, {"cat", "kitten"}]

    def vec(text: str) -> list[float]:
        words = set(text.lower().replace(".", " ").replace(",", " ").split())
        return [float(len(words & g)) for g in groups] + [0.01]

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append({"path": self.path, "auth": self.headers.get("authorization"), **payload})
            if kind == "ollama" and self.path == "/api/embeddings":
                out = {"embedding": vec(payload["prompt"])}
            elif kind == "openai" and self.path == "/v1/embeddings":
                out = {"data": [{"embedding": vec(payload["input"])}]}
            else:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port, seen


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_model_backends_find_matches_that_share_no_words(monkeypatch, tmp_path, kind):
    srv, port, seen = fake_embedding_server(kind)
    env = {"EMBEDDER": kind, "EMBED_MODEL": "m-test"}
    if kind == "ollama":
        env["OLLAMA_URL"] = f"http://127.0.0.1:{port}"
    else:
        env |= {"EMBED_URL": f"http://127.0.0.1:{port}", "EMBED_API_KEY": "embed-key-123"}
    ex = load_example(monkeypatch, tmp_path, **env)
    try:
        inst = {"id": "i"}
        ex.index(
            inst,
            [
                {
                    "id": "r-auto",
                    "name": "automobile-maintenance",
                    "description": "Service the vehicle yearly",
                    "topics": [],
                },
                {
                    "id": "r-cat",
                    "name": "feeding",
                    "description": "Feed the kitten twice a day",
                    "topics": [],
                },
            ],
        )
        hits = ex.search(inst, "my car broke down", 5)["results"]
        assert [h["id"] for h in hits] == ["r-auto"]  # no shared words; matched by (stand-in) meaning
        assert all(c["model"] == "m-test" for c in seen)
        if kind == "openai":
            assert seen[0]["auth"] == "Bearer embed-key-123"
    finally:
        srv.shutdown()


# --- the whole chain over real sockets ----------------------------------------------------------------------------------------------


def test_semantic_search_end_to_end_over_real_http(settings, monkeypatch, tmp_path):
    """hub -> real HTTP -> example service -> real HTTP -> (stand-in) embedding model, then back into the focus pack."""
    emb, emb_port, _ = fake_embedding_server("ollama")
    ex = load_example(monkeypatch, tmp_path, EMBEDDER="ollama", OLLAMA_URL=f"http://127.0.0.1:{emb_port}")
    svc_port = free_port()
    server, t = serve(ex.app, svc_port)
    try:
        monkeypatch.setenv(
            "MEMORY_HUB_REMOTE_PLUGINS",
            json.dumps(
                [
                    {
                        "key": "embeddings",
                        "url": f"http://127.0.0.1:{svc_port}",
                        "secret_env": "INDEX_PLUGIN_SECRET",
                    }
                ]
            ),
        )
        root = create_app(Settings())
        with TestClient(root) as client:
            setup_admin(client, root.fastapi)
            remote.refresh_due(force=True)
            assert registry.get("embeddings") is not None and registry.get("embeddings").info.kind == "search"
            app = root.fastapi
            with Session(app.state.engine) as s:
                owner = s.exec(select(User)).one()
                s.add(Project(slug="alpha", owner_user_id=owner.id, visibility="private"))
                s.commit()
                p = principal_for_user(s, owner)
                for name, desc in (
                    ("automobile-maintenance", "Service the vehicle every year"),
                    ("kitten-feeding", "Feed the kitten twice a day"),
                ):
                    write_record(
                        s,
                        p,
                        RecordIn(
                            name=name,
                            description=desc,
                            body="details",
                            type="project",
                            scope="project",
                            project="alpha",
                        ),
                        change_source="ui",
                    )
                s.commit()

                def pack(use):
                    return build_focus(s, p, "my car broke down", project="alpha", use_semantic=use)

                assert (
                    pack(True).associated == []
                )  # no instance yet, and plain text search has nothing for "car"
            iid = add_instance(
                app,
                plugin_key="embeddings",
                scopes=["project"],
                egress="metadata",
                config={},
                events=[],
                pull_interval_minutes=15,
            )
            res = search_sync.run_sync(app.state.engine, iid)
            assert res.error is None and res.indexed == 2
            with Session(app.state.engine) as s:
                p = principal_for_user(s, s.exec(select(User)).one())
                got = build_focus(s, p, "my car broke down", project="alpha", use_semantic=True)
                names = {i.record.name: i for i in got.associated}
                assert set(names) == {"automobile-maintenance"}
                assert any("semantic match" in w for w in names["automobile-maintenance"].why)
                assert (
                    build_focus(s, p, "my car broke down", project="alpha", use_semantic=False).associated
                    == []
                )  # FTS alone: nothing
    finally:
        server.should_exit = True
        t.join(5)
        emb.shutdown()
