"""Testes do /consulta (consulta.py + o comando no bot.py). Sem Discord: serviço aiohttp local e interação falsa.
Correr:  python tests/test_consulta.py"""
import asyncio
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="porygon-test-")
os.environ["CONSULTA_URL"] = "https://consulta.test/c"  # o comando usa o servidor de teste local em cada teste
os.environ["CONSULTA_TOKEN"] = "tok-test"
sys.path.insert(0, ROOT)

import bot as B  # noqa: E402
import consulta as C  # noqa: E402
import discord  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402


# ---- fakes -------------------------------------------------------------------
class FakeUser:
    def __init__(self, uid=111, name="Teste"):
        self.id, self.display_name = uid, name


class FakeResponse:
    def __init__(self, log):
        self.log = log

    async def send_message(self, content=None, **kw):
        self.log.append(("send_message", content, kw))

    async def defer(self, **kw):
        self.log.append(("defer", None, kw))


class FakeFollowup:
    def __init__(self, log):
        self.log = log

    async def send(self, content=None, **kw):
        self.log.append(("followup", content, kw))


class FakeInteraction:
    def __init__(self, uid=111, name="Teste", guild_id=222, channel_id=333):
        self.log = []
        self.user = FakeUser(uid, name)
        self.guild_id, self.channel_id = guild_id, channel_id
        self.response = FakeResponse(self.log)
        self.followup = FakeFollowup(self.log)


async def service(handler):
    app = web.Application()
    app.router.add_post("/c", handler)
    server = TestServer(app)
    await server.start_server()
    return server


def run_cmd(handler, inter=None, pedido="1234", limiter_reset=True):
    """Corre o callback do comando contra um serviço com `handler`. Devolve (interação, pedidos recebidos)."""
    inter = inter or FakeInteraction()
    seen = []

    async def wrapped(request):
        seen.append({"auth": request.headers.get("Authorization"), "json": await request.json()})
        return await handler(request)

    async def go():
        server = await service(wrapped)
        saved = B.CONSULTA
        B.CONSULTA = (str(server.make_url("/c")), "tok-test")
        if limiter_reset:
            B._consulta_limiter = B.SlidingWindowLimiter(*C.RATE)
        try:
            await B.consulta_slash.callback(inter, pedido) if hasattr(B.consulta_slash, "callback") \
                else await B.consulta_slash(inter, pedido)
        finally:
            B.CONSULTA = saved
            await server.close()

    asyncio.run(go())
    return inter, seen


def final(inter):
    return inter.log[-1]


# ---- núcleo --------------------------------------------------------------------
def test_config_from_env_needs_both():
    assert C.config_from_env({}) is None
    assert C.config_from_env({"CONSULTA_URL": "https://x/y"}) is None
    assert C.config_from_env({"CONSULTA_TOKEN": "t"}) is None
    assert C.config_from_env({"CONSULTA_URL": "ftp://x", "CONSULTA_TOKEN": "t"}) is None
    assert C.config_from_env({"CONSULTA_URL": "http://x/y", "CONSULTA_TOKEN": "t"}) is None  # só https
    assert C.config_from_env({"CONSULTA_URL": "https:///y", "CONSULTA_TOKEN": "t"}) is None
    assert C.config_from_env({"CONSULTA_URL": " https://x/y ", "CONSULTA_TOKEN": "t"}) == ("https://x/y", "t")


def test_build_payload():
    p = C.build_payload(1, "Nome", None, 9, "  1234  ")
    assert p == {"user_id": "1", "display_name": "Nome", "guild_id": None, "channel_id": "9", "pedido": "1234"}
    assert len(C.build_payload(1, "x", 2, 3, "9" * 500)["pedido"]) == C.PEDIDO_MAX


def test_parse_reply_valid_and_invalid():
    assert C.parse_reply({"content": "olá"}) == ("olá", None)
    assert C.parse_reply({"content": "a", "link": {"label": "L", "url": "https://e/x"}}) == \
        ("a", {"label": "L", "url": "https://e/x"})
    bad = [None, [], {}, {"content": ""}, {"content": "  "}, {"content": 5}, {"content": "x" * 2001},
           {"content": "a", "link": "https://e"}, {"content": "a", "link": {"label": "", "url": "https://e"}},
           {"content": "a", "link": {"label": "L" * 81, "url": "https://e"}},
           {"content": "a", "link": {"label": "L", "url": "javascript:alert(1)"}},
           {"content": "a", "link": {"label": "L"}},
           {"content": "a", "link": {"label": "L", "url": "http://e/x"}}]
    for b in bad:
        try:
            C.parse_reply(b)
        except C.ConsultaError:
            continue
        raise AssertionError(f"devia falhar: {b!r}")
    assert C.parse_reply({"content": "x" * 2000})[0] == "x" * 2000
    # com host: só links desse host
    ok = {"content": "a", "link": {"label": "L", "url": "https://Servico.Example/x"}}
    assert C.parse_reply(ok, host="servico.example")[1]["url"] == "https://Servico.Example/x"
    for url in ("https://outro.example/x", "https://servico.example.evil/x", "https://evil@outro.example/"):
        try:
            C.parse_reply({"content": "a", "link": {"label": "L", "url": url}}, host="servico.example")
        except C.ConsultaError:
            continue
        raise AssertionError(url)


# ---- comando --------------------------------------------------------------------
def test_registered_when_configured():
    names = [c.name for c in B.bot.tree.get_commands()]
    assert "consulta" in names
    cmd = B.bot.tree.get_command("consulta")
    assert cmd.description == "Consultar um pedido"
    assert [p.name for p in cmd.parameters] == ["pedido"]
    assert cmd.parameters[0].required


def test_not_registered_without_env():
    code = ("import os,sys,tempfile; os.environ['DISCORD_TOKEN']='x'; os.environ['DATA_DIR']=tempfile.mkdtemp();"
            "[os.environ.pop(k,None) for k in ('CONSULTA_URL','CONSULTA_TOKEN')]; sys.path.insert(0, %r);"
            "import bot; print(sorted(c.name for c in bot.bot.tree.get_commands()))" % ROOT)
    for extra in ({}, {"CONSULTA_URL": "https://x/c"}, {"CONSULTA_TOKEN": "t"}):
        env = {k: v for k, v in os.environ.items() if k not in ("CONSULTA_URL", "CONSULTA_TOKEN")}
        env.update(extra)
        set_code = "".join(f"os.environ[{k!r}]={v!r};" for k, v in extra.items())
        out = subprocess.run([sys.executable, "-c", code.replace("import bot;", set_code + "import bot;")],
                             env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr[-2000:]
        names = eval(out.stdout.strip().splitlines()[-1])
        assert "consulta" not in names and "roll" in names, names


def test_forwards_payload_and_bearer_and_renders_link_button():
    async def ok(request):
        return web.json_response({"content": "**Pedido 1** — ok.",
                                  "link": {"label": "Abrir", "url": "https://127.0.0.1/x"}})

    inter, seen = run_cmd(ok, FakeInteraction(uid=42, name="Ana", guild_id=7, channel_id=8), pedido=" 1234 ")
    assert seen == [{"auth": "Bearer tok-test", "json": {"user_id": "42", "display_name": "Ana", "guild_id": "7",
                                                         "channel_id": "8", "pedido": "1234"}}]
    assert inter.log[0][0] == "defer" and inter.log[0][2]["ephemeral"] is True
    kind, content, kw = final(inter)
    assert kind == "followup" and content == "**Pedido 1** — ok."
    assert kw["ephemeral"] is True
    am = kw["allowed_mentions"]
    assert am.everyone is False and am.users is False and am.roles is False
    (button,) = kw["view"].children
    assert isinstance(button, discord.ui.Button) and button.style == discord.ButtonStyle.link
    assert button.label == "Abrir" and button.url == "https://127.0.0.1/x"


def test_reply_without_link_has_no_view():
    async def ok(request):
        return web.json_response({"content": "Pedido arquivado."})

    inter, _ = run_cmd(ok)
    kind, content, kw = final(inter)
    assert content == "Pedido arquivado." and "view" not in kw and kw["ephemeral"] is True


def test_errors_give_neutral_ephemeral_message():
    async def s500(request):
        return web.json_response({"content": "x"}, status=500)

    async def s401(request):
        return web.Response(status=401)

    async def notjson(request):
        return web.Response(text="<html>")

    async def badshape(request):
        return web.json_response({"content": "x" * 3000})

    async def foreign_link(request):
        return web.json_response({"content": "x", "link": {"label": "L", "url": "https://outro.example/x"}})

    async def redirect(request):
        raise web.HTTPFound("https://example.com/")

    for h in (s500, s401, notjson, badshape, redirect, foreign_link):
        inter, seen = run_cmd(h)
        assert len(seen) == 1
        kind, content, kw = final(inter)
        assert kind == "followup" and content == "Serviço indisponível. Tente mais tarde.", (h.__name__, content)
        assert kw["ephemeral"] is True and "view" not in kw


def test_discord_refusing_the_reply_gives_neutral_text():
    async def ok(request):
        return web.json_response({"content": "ok", "link": {"label": "L", "url": "https://127.0.0.1/x"}})

    inter = FakeInteraction()
    calls = []

    async def flaky_send(content=None, **kw):
        calls.append((content, kw))
        if len(calls) == 1:
            raise RuntimeError("400 Bad Request (invalid button)")
        inter.log.append(("followup", content, kw))

    inter.followup.send = flaky_send
    run_cmd(ok, inter)
    assert len(calls) == 2 and calls[0][0] == "ok"
    kind, content, kw = final(inter)
    assert content == "Serviço indisponível. Tente mais tarde." and kw["ephemeral"] is True and "view" not in kw


def test_unreachable_and_timeout():
    inter = FakeInteraction()

    async def go():
        saved = B.CONSULTA
        B.CONSULTA = ("http://127.0.0.1:9/c", "tok-test")  # porta fechada
        try:
            await B.consulta_slash.callback(inter, "1") if hasattr(B.consulta_slash, "callback") \
                else await B.consulta_slash(inter, "1")
        finally:
            B.CONSULTA = saved

    asyncio.run(go())
    assert final(inter)[1] == "Serviço indisponível. Tente mais tarde."

    async def slow(request):
        await asyncio.sleep(1.0)
        return web.json_response({"content": "tarde"})

    async def go2():
        server = await service(slow)
        try:
            try:
                await C.forward(str(server.make_url("/c")), "t", {}, timeout=0.2)
            except asyncio.TimeoutError:
                return "timeout"
        finally:
            await server.close()

    assert asyncio.run(go2()) == "timeout"
    assert C.TIMEOUT == 8.0


def test_rate_limit_five_per_minute_per_user():
    async def ok(request):
        return web.json_response({"content": "ok"})

    B._consulta_limiter = B.SlidingWindowLimiter(*C.RATE)
    results = []
    for i in range(7):
        inter, seen = run_cmd(ok, FakeInteraction(uid=5), limiter_reset=False)
        results.append((final(inter)[1], len(seen)))
    assert [r[0] for r in results[:5]] == ["ok"] * 5
    assert results[5] == (C.TEXT["rate_limited"], 0) and results[6] == (C.TEXT["rate_limited"], 0)
    other, seen = run_cmd(ok, FakeInteraction(uid=6), limiter_reset=False)
    assert final(other)[1] == "ok" and final(other)[0] == "followup"
    assert C.RATE == (5, 60.0)


def test_token_never_logged():
    import logging

    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = H()
    logging.getLogger().addHandler(h)
    try:
        async def s500(request):
            return web.Response(status=500)

        run_cmd(s500)
    finally:
        logging.getLogger().removeHandler(h)
    assert records and not any("tok-test" in r for r in records)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok ", name)
    print(f"{len(tests)} tests passed")
