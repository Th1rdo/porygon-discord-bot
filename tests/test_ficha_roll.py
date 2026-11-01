"""Testes das partes puras de POST /webhook/ficha-roll (validação, texto do embed, rate limiter, auth helpers)
e do handler HTTP com o Supabase e o Discord substituídos por falsos. Nunca toca no Discord nem no Supabase.

Correr:  .venv/bin/python -m pytest tests/      (se houver pytest)
   ou:   .venv/bin/python tests/test_ficha_roll.py
"""
import asyncio
import base64
import json
import os
import sys
import tempfile

os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="porygon-test-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as B  # noqa: E402  (o guard __main__ impede o bot de arrancar)
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request  # noqa: E402
import types  # noqa: E402
import discord  # noqa: E402


def expect_error(payload, fragment):
    try:
        B.ficha_roll_validate(payload)
    except B.FichaRollError as e:
        assert fragment in str(e), (fragment, str(e))
        return
    raise AssertionError(f"expected FichaRollError with {fragment!r} for {payload!r}")


BASE = {"agent": "Joui", "title": "Luta", "kind": "pericia", "notation": "2d20+5", "dice": "[18, 7]", "total": 23}


# ---- validation ---------------------------------------------------------------------------------------------
def test_validate_minimal_ok():
    p = B.ficha_roll_validate(BASE)
    assert p["agent"] == "Joui" and p["kind"] == "pericia" and p["total"] == 23
    assert p["attack"] is None and p["damage"] is None and p["extra"] is None


def test_validate_rejects_bad_types_and_kinds():
    expect_error([], "JSON object")
    expect_error({**BASE, "kind": "dano"}, "kind must be")
    expect_error({**BASE, "agent": None}, "agent is required")
    expect_error({**BASE, "agent": "   "}, "agent must not be empty")
    expect_error({**BASE, "title": 5}, "title must be a string")
    expect_error({**BASE, "total": True}, "total must be a number")
    expect_error({**BASE, "total": "23"}, "total must be a number")
    expect_error({**BASE, "total": float("nan")}, "out of range")
    expect_error({**BASE, "total": 10**9}, "out of range")
    expect_error({**BASE, "total": None}, "one of total, attack or damage")
    expect_error({**BASE, "attack": []}, "attack must be an object")
    expect_error({**BASE, "attack": {"critical": 0}}, "attack.total is required")
    expect_error({**BASE, "attack": {"total": 3, "critical": 2}}, "attack.critical")
    expect_error({**BASE, "attack": {"total": 3, "critical": True}}, "attack.critical")
    expect_error({**BASE, "damage": {"breakdown": "x"}}, "damage.total is required")


def test_validate_normalizes_text_and_numbers():
    long = "a" * 500
    p = B.ficha_roll_validate({**BASE, "title": "linha\n# um\t\x00dois", "extra": long, "total": 7.0,
                               "unknown": "ignored"})
    assert p["title"] == "linha # um dois"
    assert len(p["extra"]) == B.FICHA_ROLL_TEXT_MAX and p["extra"].endswith("…")
    assert p["total"] == 7 and isinstance(p["total"], int)
    assert "unknown" not in p


def test_validate_attack_and_damage():
    p = B.ficha_roll_validate({"agent": "A", "title": "Faca", "kind": "ataque",
                               "attack": {"total": 25, "critical": 1, "info": "margem 19"},
                               "damage": {"total": 14, "breakdown": "1d4+2 [3] ×2"}})
    assert p["attack"] == {"total": 25, "critical": 1, "info": "margem 19"}
    assert p["damage"] == {"total": 14, "breakdown": "1d4+2 [3] ×2"}
    p = B.ficha_roll_validate({"agent": "A", "title": "x", "kind": "ataque", "attack": {"total": 1, "critical": None}})
    assert p["attack"]["critical"] == 0


def test_json_rejects_nan_constants():
    try:
        json.loads('{"total": NaN}', parse_constant=B._reject_json_constant)
    except ValueError:
        return
    raise AssertionError("NaN accepted")


# ---- embed text -----------------------------------------------------------------------------------------------
def test_embed_basic():
    e = B.ficha_roll_embed_parts(B.ficha_roll_validate(BASE))
    assert e["title"] == "🎲 Joui — Luta"
    assert e["description"] == "`2d20+5` → [18, 7]\n**Total: 23**"


def test_embed_escapes_user_markdown():
    p = B.ficha_roll_validate({**BASE, "agent": "**Bold** @everyone", "title": "_x_",
                               "dice": "||spoiler||", "notation": "1d`20`", "extra": "[link](http://evil)"})
    e = B.ficha_roll_embed_parts(p)
    assert "\\*\\*Bold\\*\\*" in e["title"] and "\\_x\\_" in e["title"]
    assert "\\|\\|spoiler\\|\\|" in e["description"]
    assert "`1dˋ20ˋ`" in e["description"]  # backticks cannot break out of the code span
    assert "\\[link]" in e["description"]


def test_embed_attack_crit_and_fumble():
    base = {"agent": "A", "title": "Faca", "kind": "ataque", "damage": {"total": 9, "breakdown": "1d4+5 [4]"}}
    crit = B.ficha_roll_embed_parts(B.ficha_roll_validate({**base, "attack": {"total": 25, "critical": 1}}))
    assert "**Ataque: 25** 💥 **Crítico!**" in crit["description"]
    assert "**Dano: 9** — 1d4+5 \\[4]" in crit["description"] or "**Dano: 9** — 1d4+5 [4]" in crit["description"]
    assert crit["color"] == 0x2ECC71
    fumble = B.ficha_roll_embed_parts(B.ficha_roll_validate({**base, "attack": {"total": 2, "critical": -1, "info": "nat 1"}}))
    assert "💀 **Falha crítica**" in fumble["description"] and "*nat 1*" in fumble["description"]
    assert fumble["color"] == 0xE74C3C
    normal = B.ficha_roll_embed_parts(B.ficha_roll_validate({**base, "attack": {"total": 12}}))
    assert "Crítico" not in normal["description"] and "Falha" not in normal["description"]


def test_embed_title_capped_and_floats():
    p = B.ficha_roll_validate({**BASE, "agent": "*" * 200, "title": "_" * 200, "total": 2.5})
    e = B.ficha_roll_embed_parts(p)
    assert len(e["title"]) <= 256
    assert "**Total: 2.5**" in e["description"]
    emb = B.ficha_roll_embed(p)
    assert emb.footer.text == "ordemcodex.xyz"


# ---- kind "op2" -----------------------------------------------------------------------------------------------
OP2 = {"kind": "op2", "agent": "Rowan", "title": "Pontaria",
       "dice": [{"role": "pericia", "sides": 10, "value": 7}, {"role": "atributo", "sides": 8, "value": 3}],
       "total": 10, "ra": 7, "rb": 3, "outcome": "normal"}


def op2_parts(**over):
    return B.ficha_roll_embed_parts(B.ficha_roll_validate({**OP2, **over}))


def die(role="atributo", sides=8, value=3, **over):
    return {"role": role, "sides": sides, "value": value, **over}


def test_op2_validate_minimal_ok():
    p = B.ficha_roll_validate({**OP2, "unknown": "ignored"})
    assert p["kind"] == "op2" and p["agent"] == "Rowan" and "unknown" not in p
    assert p["dice"][0] == {"role": "pericia", "sides": 10, "value": 7, "cancelled": False, "dropped": False, "label": None}
    assert p["perfil"] is None
    assert (p["total"], p["ra"], p["rb"], p["outcome"]) == (10, 7, 3, "normal")
    assert "dt" not in p and "dt" not in B.ficha_roll_validate({**OP2, "dt": 7})
    assert p["damage"] is None and p["extra"] is None and p["followUp"] is None


def test_op2_validate_accepts_every_size_role_and_outcome():
    for sides in (4, 6, 8, 10, 12, 20):
        for value in (1, sides):
            assert B.ficha_roll_validate({**OP2, "dice": [die(sides=sides, value=value)]})["dice"][0]["value"] == value
    for role in ("atributo", "pericia", "bonus", "pavor"):
        assert B.ficha_roll_validate({**OP2, "dice": [die(role=role)]})["dice"][0]["role"] == role
    for outcome in ("normal", "critico", "falha-critica"):
        assert B.ficha_roll_validate({**OP2, "outcome": outcome})["outcome"] == outcome
    assert len(B.ficha_roll_validate({**OP2, "dice": [die()] * 5})["dice"]) == 5
    d = B.ficha_roll_validate({**OP2, "dice": [die(sides=8.0, value=3.0, cancelled=True, dropped=None)]})["dice"][0]
    assert d["sides"] == 8 and isinstance(d["sides"], int) and d["value"] == 3 and isinstance(d["value"], int)
    assert d["cancelled"] is True and d["dropped"] is False


def test_op2_validate_rejects_bad_payloads():
    expect_error({**OP2, "agent": None}, "agent is required")
    expect_error({**OP2, "title": 5}, "title must be a string")
    expect_error({**OP2, "dice": "[7, 3]"}, "dice must be a non-empty array")
    expect_error({**OP2, "dice": []}, "dice must be a non-empty array")
    expect_error({**OP2, "dice": None}, "dice must be a non-empty array")
    expect_error({**OP2, "dice": [die()] * 21}, "at most 20")
    expect_error({**OP2, "dice": [7]}, "dice[0] must be an object")
    expect_error({**OP2, "dice": [die(role="sorte")]}, "dice[0].role")
    expect_error({**OP2, "dice": [die(role=["pavor"])]}, "dice[0].role")
    expect_error({**OP2, "dice": [die(sides=7)]}, "dice[0].sides must be one of")
    expect_error({**OP2, "dice": [die(sides=True)]}, "dice[0].sides must be a number")
    expect_error({**OP2, "dice": [die(sides=None)]}, "dice[0].sides is required")
    expect_error({**OP2, "dice": [die(sides=8, value=9)]}, "dice[0].value must be a whole number from 1 to 8")
    expect_error({**OP2, "dice": [die(value=0)]}, "dice[0].value")
    expect_error({**OP2, "dice": [die(value=2.5)]}, "dice[0].value")
    expect_error({**OP2, "dice": [die(value="3")]}, "dice[0].value must be a number")
    expect_error({**OP2, "dice": [die(), die(cancelled=1)]}, "dice[1].cancelled must be a boolean")
    expect_error({**OP2, "dice": [die(dropped="sim")]}, "dice[0].dropped must be a boolean")
    for field in ("total", "ra", "rb"):
        expect_error({k: v for k, v in OP2.items() if k != field}, f"{field} is required")
        expect_error({**OP2, field: True}, f"{field} must be a number")
        expect_error({**OP2, field: 10**9}, "out of range")
    expect_error({k: v for k, v in OP2.items() if k != "outcome"}, "outcome must be")
    for old in ("sucesso", "falha", "quase"):
        expect_error({**OP2, "outcome": old}, "outcome must be")
    expect_error({**OP2, "outcome": ["falha"]}, "outcome must be")
    expect_error({**OP2, "damage": []}, "damage must be an object")
    expect_error({**OP2, "damage": {"breakdown": "x"}}, "damage.total is required")
    expect_error({**OP2, "extra": 5}, "extra must be a string")
    expect_error({**OP2, "followUp": True}, "followUp must be a string")
    expect_error({**OP2, "dice": [die(label=5)]}, "dice[0].label must be a string")
    expect_error({**OP2, "perfil": "ladino"}, "perfil must be one of")
    expect_error({**OP2, "perfil": ["analista"]}, "perfil must be one of")
    expect_error({**OP2, "perfil": 1}, "perfil must be one of")


def test_other_kinds_keep_their_shape_next_to_op2():
    # dice continua a ser texto nos kinds antigos, e o array do op2 não entra por lá
    expect_error({**BASE, "dice": [die()]}, "dice must be a string")
    p = B.ficha_roll_validate(BASE)
    assert set(p) == {"agent", "title", "kind", "notation", "dice", "total", "attack", "damage", "extra"}
    assert B.ficha_roll_embed(p).footer.text == "ordemcodex.xyz"


ANIMAIS = {"kind": "op2", "agent": "Rowan", "title": "Animais", "total": 6, "ra": 3, "rb": 3, "outcome": "normal",
           "dice": [die("atributo", 6, 3, label="Emoção"), die("pericia", 6, 3, label="Animais")]}


def op2_lines(**over):
    return B.ficha_roll_embed_parts(B.ficha_roll_validate({**ANIMAIS, **over}))["description"].split("\n")


def test_op2_embed_exact_shape_matches_ordem_1():
    p = B.ficha_roll_validate(ANIMAIS)
    e = B.ficha_roll_embed_parts(p)
    assert e["title"] == "🎲 Rowan - Animais"
    assert e["description"] == "`d6+d6` → [3, 3]\n**Total: 6**\nRA 3 · RB 3"
    emb = B.ficha_roll_embed(p)
    assert not emb.footer.text and emb.description == e["description"]
    assert e["color"] == 0x2B2D31


def test_op2_embed_has_no_verdict_or_dt():
    text = "\n".join(op2_lines())
    for verdict in ("Sucesso", "Falha", "DT", "✅", "❌"):
        assert verdict not in text


def test_op2_embed_crit_and_fumble_marks_and_colours():
    assert op2_lines(outcome="critico")[1] == "**Total: 6** 💥 **Crítico!**"
    assert op2_lines(outcome="falha-critica", total=0)[1] == "**Total: 0** 💀 **Falha crítica**"
    colour = lambda **o: B.ficha_roll_embed_parts(B.ficha_roll_validate({**ANIMAIS, **o}))["color"]
    assert colour(outcome="critico") == 0x2ECC71 and colour(outcome="falha-critica") == 0xE74C3C
    assert colour(perfil="executor") == 0x2B2D31


def test_op2_embed_bonus_fear_and_struck_dice():
    lines = op2_lines(dice=[die("pavor", 8, 5), die("bonus", 6, 4), die("atributo", 8, 3, cancelled=True),
                            die("bonus", 6, 1, dropped=True), die("pericia", 10, 7)])
    assert lines[0] == "`d10+d8+d6+d6` → [7, ~~3~~, 4, ~~1~~] · pavor `d8` → 5"
    assert op2_lines(dice=[die("pavor", 6, 1)])[0] == "pavor `d6` → 1"
    assert op2_lines(dice=[die("bonus", 6, 2)])[0] == "`d6` → [2]"


def test_op2_embed_attack_shape_and_followup_and_extra():
    dice = [die("pericia", 10, 7, label="Pontaria"), die("atributo", 8, 3, label="Físico")]
    lines = op2_lines(title="Arco curto", dice=dice, total=10, ra=7, rb=3, damage={"total": 8, "breakdown": "RA 7 + 1"})
    assert lines == ["**Ataque: 10**", "Pontaria + Físico · `d10+d8` → [7, 3] · RA 7 · RB 3", "**Dano: 8** — RA 7 + 1"]
    assert op2_lines(damage={"total": 8}, outcome="critico")[0] == "**Ataque: 6** 💥 **Crítico!**"
    assert op2_lines(damage={"total": 8})[-1] == "**Dano: 8**"
    assert op2_lines(extra="O dado de pavor acabou")[-1] == "O dado de pavor acabou"
    follow = op2_lines(followUp="Análise: d8 3→4", total=7, rb=4)
    assert follow[0] == "*Análise: d8 3→4*" and follow[2] == "**Total: 7**" and follow[3] == "RA 3 · RB 4"


def test_op2_free_dice_have_no_ra_rb_and_allow_large_pools():
    dice = [die("bonus", 6, 2)] * 12 + [die("bonus", 20, 7)]
    p = {**ANIMAIS, "title": "Dados", "dice": dice, "total": 31}
    p.pop("ra"), p.pop("rb")
    lines = B.ficha_roll_embed_parts(B.ficha_roll_validate(p))["description"].split("\n")
    assert lines[1] == "**Total: 31**" and len(lines) == 2
    assert lines[0].startswith("`d6+d6+") and lines[0].endswith("2, 7]")
    # teste normal continua exigindo RA e RB
    bad = dict(ANIMAIS); bad.pop("ra")
    try:
        B.ficha_roll_validate(bad)
    except B.FichaRollError:
        pass
    else:
        raise AssertionError("ra deveria ser obrigatório num teste")


def test_op2_embed_escapes_user_text():
    e = B.ficha_roll_embed_parts(B.ficha_roll_validate({
        **ANIMAIS, "agent": "**Bold** @everyone", "title": "_x_", "extra": "# título [link](http://evil)",
        "followUp": "||spoiler||", "damage": {"total": 1, "breakdown": "`code`"},
        "dice": [die("pericia", label="~~a~~\n# b")]}))
    assert e["title"].startswith("🎲 \\*\\*Bold\\*\\* @everyone - \\_x\\_")
    d = e["description"]
    assert "\\|\\|spoiler\\|\\|" in d and "\\`code\\`" in d and "\\[link]" in d and "\\~\\~a\\~\\~" in d
    # uma só linha por campo: texto do jogador nunca abre linha própria
    assert len(d.split("\n")) == 5


def test_op2_largest_payload_fits_the_body_limit_and_embed():
    p = {**OP2, "agent": "a" * 200, "title": "b" * 200, "extra": "c" * 200, "followUp": "d" * 200,
         "damage": {"total": 1000, "breakdown": "e" * 200}, "perfil": "analista",
         "dice": [die(cancelled=True, dropped=True, label="f" * 200)] * 5}
    assert len(json.dumps(p).encode()) <= B.FICHA_ROLL_MAX_BODY
    e = B.ficha_roll_embed_parts(B.ficha_roll_validate(p))
    assert len(e["description"]) <= 4096


# ---- rate limiter ---------------------------------------------------------------------------------------------
def test_limiter_sliding_window():
    lim = B.SlidingWindowLimiter(8, 10.0)
    assert all(lim.hit("u", now=100.0 + i * 0.01) == 0 for i in range(8))
    wait = lim.hit("u", now=100.5)
    assert 9.4 < wait <= 10.0
    assert lim.hit("other", now=100.5) == 0          # per key
    assert lim.hit("u", now=109.99) > 0              # still inside window of the first hit
    assert lim.hit("u", now=110.0) == 0              # first hit expired → one slot free
    assert lim.hit("u", now=110.001) > 0             # burst at the boundary doesn't get through
    assert lim.hit("u", now=200.0) == 0


def test_limiter_bounded_keys():
    lim = B.SlidingWindowLimiter(1, 1.0, max_keys=3)
    for i in range(10):
        lim.hit(f"k{i}", now=float(i * 5))  # old keys pruned
    assert len(lim._hits) <= 3


# ---- auth helpers ---------------------------------------------------------------------------------------------
def _jwt(payload: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'HS256'})}.{enc(payload)}.sig"


def test_bearer_token_and_jwt_exp():
    t = _jwt({"sub": "x", "exp": 1234567890})
    assert B.bearer_token(f"Bearer {t}") == t
    assert B.bearer_token(None) is None
    assert B.bearer_token("Basic abc") is None
    assert B.bearer_token("Bearer not a jwt") is None
    assert B.bearer_token("Bearer " + "a" * 5000 + ".b.c") is None
    assert B._jwt_exp(t) == 1234567890
    assert B._jwt_exp("garbage") is None


def test_client_key_ignores_spoofable_left_hop():
    req = lambda h: make_mocked_request("POST", B.FICHA_ROLL_PATH, headers=h)
    assert B._client_key(req({"X-Forwarded-For": "6.6.6.6, 10.0.0.1, 203.0.113.9"})) == "203.0.113.9"
    assert B._client_key(req({"X-Real-IP": "198.51.100.7", "X-Forwarded-For": "6.6.6.6"})) == "198.51.100.7"
    assert B._client_key(req({"X-Forwarded-For": " , "})) != ""


# ---- HTTP handler (Supabase + Discord faked) ------------------------------------------------------------------
class _FakeMsg:
    id = 42


class _FakeChannel:
    id = 1165819468354687066
    guild = None

    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)
        return _FakeMsg()


async def _run_handler_tests():
    calls = {"n": 0}

    async def fake_check(token):
        calls["n"] += 1
        if token.split(".")[-1].startswith("bad"):
            raise B.FichaAuthError(401, "unauthorized")
        if token.endswith(".notinvited"):
            raise B.FichaAuthError(403, "not_allowed")
        return "user-" + token[-4:]

    orig_check, orig_resolve = B._ficha_check_token, B._resolve_channel
    B._ficha_check_token = fake_check
    fake = _FakeChannel()
    resolves = {"n": 0}

    async def fake_resolve(cid):
        resolves["n"] += 1
        return fake

    B._resolve_channel = fake_resolve
    B._ficha_roll_channel = None
    B.ready_event.set()

    def reset():
        B._ficha_auth_cache.clear()
        B._ficha_auth_neg_cache.clear()
        for lim in (B._ficha_roll_limiter, B._ficha_auth_limiter, B._ficha_auth_global_limiter):
            lim._hits.clear()
    reset()

    app = web.Application(middlewares=[B._cors_middleware])
    app.add_routes([web.post(B.FICHA_ROLL_PATH, B._handle_ficha_roll),
                    web.post("/webhook/roll", lambda r: web.json_response({"x": 1})),
                    web.route("OPTIONS", "/{tail:.*}", B._handle_options)])
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        good = "a.b.good"
        H = lambda tok=good, origin="https://ordemcodex.xyz": {"Authorization": f"Bearer {tok}", "Origin": origin}

        # CORS preflight: allowed origin echoed, others get no ACAO
        r = await client.options(B.FICHA_ROLL_PATH, headers={"Origin": "https://ordemcodex.xyz",
                                                             "Access-Control-Request-Method": "POST"})
        assert r.status == 204 and r.headers["Access-Control-Allow-Origin"] == "https://ordemcodex.xyz"
        assert "Authorization" in r.headers["Access-Control-Allow-Headers"]
        for o in ("http://localhost:5184", "https://www.ordemcodex.xyz",
                  "https://ficha-paranormal-preview-production.up.railway.app"):
            r = await client.options(B.FICHA_ROLL_PATH, headers={"Origin": o})
            assert r.headers.get("Access-Control-Allow-Origin") == o, o
        r = await client.options(B.FICHA_ROLL_PATH, headers={"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in r.headers
        # other routes unchanged
        r = await client.post("/webhook/roll", json={})
        assert r.headers["Access-Control-Allow-Origin"] == "*" and r.headers["Access-Control-Allow-Headers"] == "Content-Type"

        # 401 without / with malformed token (no Supabase call)
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE)
        assert r.status == 401 and (await r.json())["error"] == "unauthorized"
        assert calls["n"] == 0
        # 413 / 400
        r = await client.post(B.FICHA_ROLL_PATH, data=b"{" + b" " * 5000 + b"}", headers=H())
        assert r.status == 413, r.status

        async def chunked():  # no Content-Length → limit enforced while reading
            for _ in range(10):
                yield b" " * 1000
        r = await client.post(B.FICHA_ROLL_PATH, data=chunked(), headers=H())
        assert r.status == 413, r.status
        r = await client.post(B.FICHA_ROLL_PATH, data=b"not json", headers=H())
        assert r.status == 400 and (await r.json())["error"] == "invalid_json"
        r = await client.post(B.FICHA_ROLL_PATH, data=b'{"total": NaN}', headers=H())
        assert r.status == 400
        r = await client.post(B.FICHA_ROLL_PATH, json={**BASE, "kind": "x"}, headers=H())
        body = await r.json()
        assert r.status == 400 and body["error"] == "invalid_payload" and "kind" in body["detail"]
        # 401 / 403 from auth
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=H("a.b.bad"))
        assert r.status == 401
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=H("a.b.notinvited"))
        assert r.status == 403 and (await r.json())["error"] == "not_allowed"

        # success: embed sent with mentions disabled; auth cached
        n_before = calls["n"]
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=H())
        body = await r.json()
        assert r.status == 200 and body == {"ok": True, "message_id": "42", "channel_id": str(fake.id)}, body
        assert r.headers["Access-Control-Allow-Origin"] == "https://ordemcodex.xyz"
        sent = fake.sent[-1]
        am = sent["allowed_mentions"]
        assert not am.everyone and not am.users and not am.roles
        assert sent["embed"].title == "🎲 Joui — Luta"
        for _ in range(7):
            r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=H())
            assert r.status == 200
        assert calls["n"] == n_before + 1, "auth result should be cached per token"
        # 9th in 10 s → 429 with Retry-After
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=H())
        assert r.status == 429 and int(r.headers["Retry-After"]) >= 1
        body = await r.json()
        assert body["error"] == "rate_limited" and isinstance(body["retry_after"], int)
        assert body["retry_after"] == int(r.headers["Retry-After"])
        assert len(fake.sent) == 8
        assert resolves["n"] == 1, "channel resolved once, then cached"

        # 401 results are cached (60 s) per token → no second Supabase call
        reset()
        n0 = calls["n"]
        for _ in range(3):
            r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=H("a.b.bad"))
            assert r.status == 401
        assert calls["n"] == n0 + 1

        # kind op2 pelo mesmo caminho: embed próprio; payload inválido nunca chega ao canal
        reset()
        n_sent = len(fake.sent)
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "dice": [die(value=99)]}, headers=H())
        body = await r.json()
        assert r.status == 400 and body["error"] == "invalid_payload" and "dice[0].value" in body["detail"]
        assert len(fake.sent) == n_sent
        r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=H())
        assert r.status == 200 and len(fake.sent) == n_sent + 1
        assert fake.sent[-1]["embed"].title == "🎲 Rowan - Pontaria"
        assert not fake.sent[-1]["embed"].footer.text

        # M1: rotating X-Forwarded-For (spoofed IP per request) still hits the GLOBAL ceiling
        reset()
        n0 = calls["n"]
        limit = B.FICHA_ROLL_AUTH_GLOBAL_RATE[0]
        statuses = []
        for i in range(limit + 5):
            r = await client.post(B.FICHA_ROLL_PATH, json=BASE,
                                  headers={**H(f"a.b.bad{i:04d}"), "X-Forwarded-For": f"10.{i // 256}.{i % 256}.1"})
            statuses.append(r.status)
        assert statuses[:limit] == [401] * limit, statuses[:limit]
        assert set(statuses[limit:]) == {429}, statuses[limit:]
        assert calls["n"] - n0 == limit, "Supabase must not be called past the global ceiling"
        assert int(r.headers["Retry-After"]) >= 1 and isinstance((await r.json())["retry_after"], int)

        # per-IP limit (stable IP) with the same header contract
        reset()
        for i in range(B.FICHA_ROLL_AUTH_RATE[0]):
            r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers={**H(f"a.b.bad{i:04d}"), "X-Real-IP": "1.2.3.4"})
            assert r.status == 401
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers={**H("a.b.badXXXX"), "X-Real-IP": "1.2.3.4"})
        assert r.status == 429 and "Retry-After" in r.headers

        # m1: Forbidden/NotFound on send clears the channel cache → next request resolves again
        reset()
        resp403 = types.SimpleNamespace(status=403, reason="Forbidden")

        async def send_forbidden(**kw):
            raise discord.Forbidden(resp403, "Missing Permissions")
        fake.send = send_forbidden
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=H())
        assert r.status == 502 and (await r.json())["error"] == "discord_error"
        assert B._ficha_roll_channel is None
        del fake.send  # back to the class method
        before = resolves["n"]
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=H())
        assert r.status == 200 and resolves["n"] == before + 1

        # m3: ficha_roll can be disabled on its own (ficha = invites stays on)
        reset()
        fake.guild = types.SimpleNamespace(id=999)
        B.set_feature(999, "ficha_roll", False)
        assert B.feature_enabled(999, "ficha")
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=H())
        assert r.status == 503 and (await r.json())["error"] == "disabled"
        B.set_feature(999, "ficha", False)
        B.set_feature(999, "ficha_roll", True)
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=H())
        assert r.status == 200, "disabling invites must not disable rolls"
    finally:
        await client.close()
        B._ficha_check_token, B._resolve_channel = orig_check, orig_resolve
        B._ficha_roll_channel = None


def test_handler_end_to_end_with_fakes():
    asyncio.run(_run_handler_tests())


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok ", name)
    print(f"{len(tests)} tests passed")
