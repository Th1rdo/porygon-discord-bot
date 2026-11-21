"""Testes dos anexos do /webhook/broadcast (descarregar ficheiros e enviá-los como discord.File).
Sem Discord: servidor aiohttp local e canal falso.  Correr:  python tests/test_broadcast_files.py"""
import asyncio
import os
import sys
import tempfile

os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="porygon-test-")
os.environ["BROADCAST_ALLOW_LOCAL_HTTP"] = "1"  # o servidor de teste é http://127.0.0.1
os.environ.pop("BROADCAST_FILE_HOSTS", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as B  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402
import discord  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


async def serve():
    async def img(request):
        return web.Response(body=PNG, content_type="image/png")

    async def big(request):
        return web.Response(body=b"0" * (B.BROADCAST_FILE_MAX + 1))

    async def missing(request):
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/a.png", img)
    app.router.add_get("/big", big)
    app.router.add_get("/gone", missing)
    server = TestServer(app)
    await server.start_server()
    return server


async def with_fake(fn):
    ch = FakeChannel()
    saved = B._resolve_channel

    async def fake_resolve(cid):
        return ch

    B._resolve_channel = fake_resolve
    B.ready_event.set()
    server = await serve()
    try:
        await fn(ch, str(server.make_url("")).rstrip("/"))
    finally:
        B._resolve_channel = saved
        await server.close()


def test_sends_one_discord_file_with_name():
    async def run(ch, base):
        res = await B._broadcast_to(["1", "2"], "olá", [{"url": f"{base}/a.png", "filename": "../x/nota.png"}])
        assert res == {"1": "ok", "2": "ok"}, res
        assert len(ch.sent) == 2
        for content, kw in ch.sent:
            assert content == "olá"
            assert len(kw["files"]) == 1
            f = kw["files"][0]
            assert isinstance(f, discord.File) and f.filename == "nota.png"
            assert f.fp.read() == PNG  # cada canal recebe um ficheiro novo, por ler
    asyncio.run(with_fake(run))


def test_no_files_keeps_old_behaviour():
    async def run(ch, base):
        res = await B._broadcast_to(["1"], "só texto")
        assert res == {"1": "ok"} and ch.sent == [("só texto", {})]
    asyncio.run(with_fake(run))


def test_fetch_failure_sends_nothing():
    async def run(ch, base):
        for path in ("/gone", "/big"):
            res = await B._broadcast_to(["1"], "x", [{"url": f"{base}{path}", "filename": "a.png"}])
            assert res["1"].startswith("error: attachment"), res
        assert ch.sent == []
    asyncio.run(with_fake(run))


def test_url_policy():
    ok = B.broadcast_files_validate
    assert ok(None) == []
    assert ok([{"url": "https://x.example/a.png", "filename": "a.png"}])
    assert ok([{"url": "http://127.0.0.1:9/a.png", "filename": "a.png"}])
    assert ok([{"url": "http://localhost:9/a.png", "filename": "a.png"}])
    bad = [
        [{"url": "http://example.com/a.png", "filename": "a.png"}],
        [{"url": "file:///etc/passwd", "filename": "a"}],
        [{"url": "https://x.example/a.png"}],
        [{"url": "https://x.example/a.png", "filename": ""}],
        [{"url": "https://x.example/a.png", "filename": "a.png"}] * 5,
        "nope",
    ]
    for b in bad:
        try:
            ok(b)
        except ValueError:
            continue
        raise AssertionError(f"accepted {b!r}")


class env:
    """Define/remove variáveis de ambiente durante um bloco e repõe-nas no fim."""
    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def rejects(files):
    try:
        B.broadcast_files_validate(files)
    except ValueError:
        return True
    return False


def test_local_http_needs_opt_in():
    local = [{"url": "http://127.0.0.1:9/a.png", "filename": "a.png"}]
    with env(BROADCAST_ALLOW_LOCAL_HTTP=None):
        assert rejects(local)
        assert rejects([{"url": "http://localhost:9/a.png", "filename": "a.png"}])
        assert not rejects([{"url": "https://x.example/a.png", "filename": "a.png"}])
    with env(BROADCAST_ALLOW_LOCAL_HTTP="0"):
        assert rejects(local)
    with env(BROADCAST_ALLOW_LOCAL_HTTP="1"):
        assert not rejects(local)


def test_file_hosts_allowlist():
    a = [{"url": "https://files.example.test/a.png", "filename": "a.png"}]
    b = [{"url": "https://other.example.test/a.png", "filename": "a.png"}]
    with env(BROADCAST_FILE_HOSTS=None):
        assert not rejects(a) and not rejects(b)
    with env(BROADCAST_FILE_HOSTS=" Files.Example.Test , cdn.example.test"):
        assert not rejects(a)
        assert rejects(b)
        assert rejects([{"url": "http://127.0.0.1:9/a.png", "filename": "a.png"}])  # local fora da lista
    with env(BROADCAST_FILE_HOSTS="127.0.0.1"):
        assert not rejects([{"url": "http://127.0.0.1:9/a.png", "filename": "a.png"}])


def test_handler_rejects_bad_files():
    async def run():
        class Req:
            async def json(self):
                return {"token": "t", "channel_ids": [1], "content": "x",
                        "files": [{"url": "http://example.com/a.png", "filename": "a.png"}]}
        saved = B.WEBHOOK_TOKEN
        B.WEBHOOK_TOKEN = "t"
        try:
            resp = await B._handle_broadcast(Req())
        finally:
            B.WEBHOOK_TOKEN = saved
        assert resp.status == 400, resp.status
    asyncio.run(run())


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok ", name)
    print(f"{len(tests)} tests passed")
