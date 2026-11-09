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
    assert set(p) == {"agent", "title", "kind", "notation", "dice", "total", "attack", "damage", "extra", "sheet"}
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


# ---- /ficha_ligar (ficha_link_consume) ------------------------------------------------------------------------
SHEET = "0f8e2b7a-1c3d-4e5f-9a8b-7c6d5e4f3a21"
SHEET2 = "11111111-2222-4333-8444-555555555555"


def _perms(ok=True):
    return types.SimpleNamespace(view_channel=True, send_messages=ok, send_messages_in_threads=ok, embed_links=True)


def _link_channel(cid=777, guild_id=1, member_ok=True, bot_ok=True, ctype=discord.ChannelType.text):
    class Ch:
        id, type, guild, mention = cid, ctype, types.SimpleNamespace(id=guild_id, name="AKRASIA"), f"<#{cid}>"

        def permissions_for(self, who):
            return _perms(bot_ok if who == "me" else member_ok)
    return Ch()


class _LinkState:
    """Isola códigos, ligações, limites e o guardar do config num teste."""

    def __enter__(self):
        self.saved = (B._config.pop("ficha_roll", None), B._save_config, dict(B._ficha_link_codes))
        B._save_config = lambda: None
        B._ficha_link_codes.clear()
        B._ficha_link_fail_limiter._hits.clear()
        return self

    def __exit__(self, *exc):
        cfg, B._save_config, codes = self.saved
        B._config.pop("ficha_roll", None)
        if cfg is not None:
            B._config["ficha_roll"] = cfg
        B._ficha_link_codes.clear()
        B._ficha_link_codes.update(codes)
        B._ficha_link_fail_limiter._hits.clear()


def test_link_code_shape_replacement_and_expiry():
    with _LinkState():
        user = types.SimpleNamespace(id=555, mention="<@555>")
        c1 = B.ficha_link_new_code("u1", SHEET, "Rowan")
        c2 = B.ficha_link_new_code("u1", SHEET, "Rowan")
        other = B.ficha_link_new_code("u1", SHEET2, None)
        assert c1 not in B._ficha_link_codes and {c2, other} <= set(B._ficha_link_codes), "código novo substitui o antigo"
        assert B.ficha_link_pending("u1", SHEET) and not B.ficha_link_pending("u2", SHEET)
        ok, text = B.ficha_link_consume(c1, _link_channel(), user, "me")
        assert not ok and text == B.FICHA_LINK_TEXT["unknown_code"]
        # expirado: recusado e apagado
        B._ficha_link_codes[c2]["expires"] = B.time.time() - 1
        assert not B.ficha_link_pending("u1", SHEET)
        ok, text = B.ficha_link_consume(c2, _link_channel(), user, "me")
        assert not ok and text == B.FICHA_LINK_TEXT["unknown_code"] and c2 not in B._ficha_link_codes
        # expirados saem ao criar o próximo
        B._ficha_link_codes[other]["expires"] = 0
        B.ficha_link_new_code("u9", SHEET, None)
        assert other not in B._ficha_link_codes
        # sem nome → texto genérico
        c3 = B.ficha_link_new_code("u1", SHEET2, None)
        ok, text = B.ficha_link_consume(c3, _link_channel(), user, "me")
        assert ok and text.startswith(f"🔗 **{B.FICHA_LINK_TEXT['linked_unnamed']}** agora rola aqui."), text
        # codes são únicos entre os pendentes
        codes = {B.ficha_link_new_code(f"u{i}", SHEET, None) for i in range(300)}
        assert len(codes) == 300


def test_link_pending_codes_capped_per_account():
    with _LinkState():
        sheets = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(B.FICHA_LINK_MAX_PENDING_PER_UID + 1)]
        codes = [B.ficha_link_new_code("u1", s, None) for s in sheets]
        assert all(codes[:-1]) and codes[-1] is None, "uma conta não enche o teto global"
        assert B.ficha_link_new_code("u2", sheets[0], None), "as outras contas continuam"
        assert B.ficha_link_new_code("u1", sheets[0], None), "trocar o código de uma ficha que já tem um continua valendo"


def test_link_consume_seeds_member_cache():
    with _LinkState():
        B._ficha_member_cache.clear()
        user = types.SimpleNamespace(id=556, mention="<@556>")
        ch = _link_channel()
        code = B.ficha_link_new_code("u1", SHEET, "Rowan")
        ok, _ = B.ficha_link_consume(code, ch, user, "me")
        assert ok and B._ficha_member_cache[(ch.guild.id, "556")][0] is user, "quem ligou conta como membro já"
        B._ficha_member_cache.clear()


def test_transient_discord_errors_are_not_refusals():
    async def run():
        orig = B._ficha_resolve_quiet
        async def flaky(_id):
            raise B.FichaDiscordTransient(503)
        B._ficha_resolve_quiet = flaky
        try:
            info = await B._ficha_link_info({"channel": "123", "guild": "9", "by": "1"})
            assert info["usable"] is True and info["name"] == "123", info
            assert await B._ficha_default_info() is None
        finally:
            B._ficha_resolve_quiet = orig
    asyncio.run(run())


def test_link_consume_channel_problems_keep_the_code():
    with _LinkState():
        user = types.SimpleNamespace(id=555, mention="<@555>")
        code = B.ficha_link_new_code("u1", SHEET, "@everyone _Rowan_")
        T = B.FICHA_LINK_TEXT
        cases = [
            (_link_channel(member_ok=False), T["user_cannot_post"].format(mention="<#777>")),
            (_link_channel(bot_ok=False), T["bot_cannot_post"].format(mention="<#777>")),
            (_link_channel(ctype=discord.ChannelType.private_thread), T["private_thread"]),
            (_link_channel(ctype=discord.ChannelType.voice), T["not_text"]),
            (None, T["not_text"]),
        ]
        for ch, expected in cases:
            ok, text = B.ficha_link_consume(code, ch, user, "me")
            assert not ok and text == expected, (text, expected)
            assert code in B._ficha_link_codes, "canal que não serve não gasta o código"
        B.set_feature(1, "ficha_roll", False)
        try:
            ok, text = B.ficha_link_consume(code, _link_channel(), user, "me")
            assert not ok and text == T["disabled"] and "/config" in text
        finally:
            B.set_feature(1, "ficha_roll", True)
        old = B.FICHA_GUILD_IDS
        try:
            B.FICHA_GUILD_IDS = {9}
            ok, text = B.ficha_link_consume(code, _link_channel(), user, "me")
            assert not ok and text == T["guild_not_allowed"]
        finally:
            B.FICHA_GUILD_IDS = old
        # o /ficha_canal_padrao continua com as mensagens dele
        assert B._ficha_canal_problem(_link_channel(member_ok=False), user, "me") == \
            B.FICHA_CANAL_TEXT["user_cannot_post"].format(mention="<#777>")
        # nada disto contou como código errado
        ok, text = B.ficha_link_consume(code, _link_channel(), user, "me")
        assert ok, text
        assert "@\u200beveryone" in text and "\\_Rowan\\_" in text, text
        # ligar de novo noutro canal: diz qual era o anterior
        code = B.ficha_link_new_code("u1", SHEET, "Rowan")
        ok, text = B.ficha_link_consume(code, _link_channel(888), user, "me")
        assert ok and "agora rola aqui. (antes: <#777>)\n" in text, text
        assert B.ficha_link_binding("u1", SHEET)["channel"] == "888"


def test_link_consume_wrong_codes_rate_limited_per_user():
    with _LinkState():
        user = types.SimpleNamespace(id=555, mention="<@555>")
        code = B.ficha_link_new_code("u1", SHEET, "Rowan")
        for _ in range(B.FICHA_LINK_FAIL_RATE[0]):
            assert B.ficha_link_consume("ZZZZZZ", _link_channel(), user, "me")[1] == B.FICHA_LINK_TEXT["unknown_code"]
        ok, text = B.ficha_link_consume(code, _link_channel(), user, "me")
        assert not ok and text == B.FICHA_LINK_TEXT["rate_limited"] and code in B._ficha_link_codes
        other = types.SimpleNamespace(id=556, mention="<@556>")
        ok, _ = B.ficha_link_consume(code, _link_channel(), other, "me")
        assert ok, "o limite é por pessoa"
        assert B.ficha_link_binding("u1", SHEET)["by"] == "556"


def test_link_per_uid_cap():
    with _LinkState():
        user = types.SimpleNamespace(id=555, mention="<@555>")
        B._ficha_channels()["sheets"]["u1"] = {f"s{i}": {"channel": "1", "guild": "1", "by": "5", "at": 0}
                                               for i in range(B.FICHA_LINK_MAX_PER_UID - 1)}
        B._ficha_channels()["sheets"]["u1"][SHEET] = {"channel": "1", "guild": "1", "by": "5", "at": 0}
        code = B.ficha_link_new_code("u1", SHEET2, "Rowan")
        ok, text = B.ficha_link_consume(code, _link_channel(), user, "me")
        assert not ok and str(B.FICHA_LINK_MAX_PER_UID) in text and code in B._ficha_link_codes
        # ligar de novo uma ficha que já está na conta continua a dar
        again = B.ficha_link_new_code("u1", SHEET, "Rowan")
        ok, text = B.ficha_link_consume(again, _link_channel(), user, "me")
        assert ok and "(antes: <#1>)" in text, text
        B.ficha_link_unlink("u1", SHEET)
        ok, _ = B.ficha_link_consume(code, _link_channel(), user, "me")
        assert ok and len(B._ficha_channels()["sheets"]["u1"]) == B.FICHA_LINK_MAX_PER_UID


def test_link_validate_and_binding_sanity():
    p = B.ficha_link_validate({"action": "code", "sheet": SHEET.upper(), "name": "x" * 200, "extra": 1})
    assert p["sheet"] == SHEET and len(p["name"]) == B.FICHA_LINK_NAME_MAX and p["name"].endswith("…")
    with _LinkState():
        B._ficha_channels()["sheets"]["u1"] = {SHEET: {"channel": "abc", "by": "5"}, SHEET2: "junk"}
        assert B.ficha_link_binding("u1", SHEET) is None and B.ficha_link_binding("u1", SHEET2) is None
        B._ficha_channels()["sheets"]["u2"] = ["junk"]
        assert B.ficha_link_binding("u2", SHEET) is None
        B.ficha_set_default_channel(888)
        assert B.ficha_target_channel_id() == (888, "default")
        B.ficha_set_default_channel(None)
        assert B.ficha_target_channel_id() == (B.FICHA_ROLL_CHANNEL_ID, "env")
    assert "/ficha_ligar <codigo>" in B.AJUDA_TEXT and "/ficha_canal [" not in B.AJUDA_TEXT
    assert "ficha_canal_limpar" not in B.AJUDA_TEXT


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


def test_channel_checks_are_real():
    P = lambda **k: types.SimpleNamespace(view_channel=True, send_messages=True, send_messages_in_threads=True,
                                          embed_links=True, **{**{}, **k})
    class Ch:
        type = discord.ChannelType.text
        def __init__(self, member_p, bot_p):
            self.p = {"m": member_p, "b": bot_p}
        def permissions_for(self, who):
            return self.p[who]
    assert B._ficha_channel_ok(Ch(P(), P()), "m", "b")
    assert not B._ficha_channel_ok(Ch(types.SimpleNamespace(view_channel=True, send_messages=False,
                                                          send_messages_in_threads=False, embed_links=True), P()), "m", "b")
    assert not B._ficha_channel_ok(Ch(P(), types.SimpleNamespace(view_channel=True, send_messages=True,
                                                               send_messages_in_threads=True, embed_links=False)), "m", "b")
    priv = Ch(P(), P()); priv.type = discord.ChannelType.private_thread
    assert not B._ficha_channel_ok(priv, "m", "b")
    assert not B._ficha_channel_ok(None, "m", "b") and not B._ficha_channel_ok(Ch(P(), P()), None, "b")
    g = types.SimpleNamespace(id=5)
    old = B.FICHA_GUILD_IDS
    try:
        B.FICHA_GUILD_IDS = {9}
        assert not B._ficha_guild_ok(g)
        B.FICHA_GUILD_IDS = set()
        assert B._ficha_guild_ok(g)
    finally:
        B.FICHA_GUILD_IDS = old


async def _run_handler_tests():
    calls = {"n": 0}

    async def fake_check(token):
        calls["n"] += 1
        if token.split(".")[-1].startswith("bad"):
            raise B.FichaAuthError(401, "unauthorized")
        if token.endswith(".notinvited"):
            raise B.FichaAuthError(403, "not_allowed")
        return "user-" + token[-4:]

    orig = (B._ficha_check_token, B._resolve_channel, B._ficha_can_post, B._save_config, B._ficha_resolve_quiet)
    B._ficha_check_token = fake_check
    fake = _FakeChannel()
    resolves = {"n": 0}
    player = _FakeChannel()
    player.id, player.name, player.guild = 777, "mesa", types.SimpleNamespace(id=1, name="AKRASIA")
    can_post = {"555": True}  # quem ligou (id do Discord) → ainda pode escrever no canal 777?

    async def fake_resolve(cid):
        resolves["n"] += 1
        if cid == 4242:
            return None  # canal apagado / inacessível
        return player if cid == 777 else fake

    async def fake_can_post(channel, did):
        return getattr(channel, "id", None) == 777 and can_post.get(did, False)
    B._ficha_can_post = fake_can_post
    B._save_config = lambda: None
    B._ficha_link_codes.clear()
    saved_cfg = B._config.pop("ficha_roll", None)

    B._resolve_channel = fake_resolve
    B._ficha_resolve_quiet = fake_resolve
    B._ficha_roll_channels.clear()
    B.ready_event.set()

    def reset():
        B._ficha_auth_cache.clear()
        B._ficha_auth_neg_cache.clear()
        for lim in (B._ficha_roll_limiter, B._ficha_auth_limiter, B._ficha_auth_global_limiter,
                    B._ficha_link_limiter, B._ficha_link_fail_limiter):
            lim._hits.clear()
    reset()

    app = web.Application(middlewares=[B._cors_middleware])
    app.add_routes([web.post(B.FICHA_ROLL_PATH, B._handle_ficha_roll),
                    web.post(B.FICHA_LINK_PATH, B._handle_ficha_link),
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
        assert "default" not in B._ficha_roll_channels
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
        # --- ficha ligada a um canal (/ficha_ligar com código) --------------------------------------------------
        reset()
        LINK = B.FICHA_LINK_PATH
        uid = "user-good"
        sheet_up = SHEET.upper()
        # auth e validação
        r = await client.post(LINK, json={"action": "status", "sheet": SHEET})
        assert r.status == 401 and (await r.json())["error"] == "unauthorized"
        r = await client.post(LINK, data=b'{"action":"code","sheet":"' + b"x" * 1100 + b'"}', headers=H())
        assert r.status == 413
        for bad in (b"not json", b'{"action": NaN}', b"[]", b'{"action":"code"}', b'{"action":"x","sheet":"%s"}' % SHEET.encode(),
                    b'{"action":"code","sheet":"12345"}', b'{"action":"code","sheet":5}',
                    b'{"action":"code","sheet":"%s","name":5}' % SHEET.encode()):
            r = await client.post(LINK, data=bad, headers=H())
            assert r.status == 400 and (await r.json())["error"] == "invalid_payload", bad
        r = await client.post(LINK, json={"action": "status", "sheet": SHEET}, headers=H("a.b.notinvited"))
        assert r.status == 403 and (await r.json())["error"] == "not_allowed"
        r = await client.options(LINK, headers={"Origin": "https://ordemcodex.xyz", "Access-Control-Request-Method": "POST"})
        assert r.headers.get("Access-Control-Allow-Origin") == "https://ordemcodex.xyz"
        r = await client.options(LINK, headers={"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in r.headers

        # nada ligado: status vazio com o padrão
        r = await client.post(LINK, json={"action": "status", "sheet": SHEET}, headers=H())
        body = await r.json()
        assert r.status == 200 and body["linked"] is None and body["pending"] is False, body
        assert body["default"]["id"] == str(fake.id)

        # código → pendente (sheet em maiúsculas guardado em minúsculas)
        r = await client.post(LINK, json={"action": "code", "sheet": sheet_up, "name": "  Rowan\n**Ka**  "}, headers=H())
        body = await r.json()
        assert r.status == 200 and body["ok"] and body["expires_in"] == 600, body
        code = body["code"]
        assert len(code) == 6 and set(code) <= set(B.FICHA_LINK_ALPHABET)
        assert B._ficha_link_codes[code] == {**B._ficha_link_codes[code], "uid": uid, "sheet": SHEET, "name": "Rowan **Ka**"}
        r = await client.post(LINK, json={"action": "status", "sheet": SHEET}, headers=H())
        assert (await r.json())["pending"] is True
        # rolagem com a ficha ainda não ligada → padrão
        n_def = len(fake.sent)
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "sheet": SHEET}, headers=H())
        assert r.status == 200 and (await r.json())["channel_id"] == str(fake.id) and len(fake.sent) == n_def + 1

        # /ficha_ligar no canal da mesa (lógica do comando, sem Interaction)
        ch, user, me = _link_channel(777), types.SimpleNamespace(id=555, mention="<@555>"), "me"
        ok, text = B.ficha_link_consume(f" {code[:3].lower()}-{code[3:]} ", ch, user, me)
        assert ok and text.startswith("🔗 **Rowan \\*\\*Ka\\*\\*** agora rola aqui.\n"), text
        assert "<@555>" in text and "antes" not in text
        assert code not in B._ficha_link_codes
        assert B._config["ficha_roll"]["sheets"][uid][SHEET] == {**B._config["ficha_roll"]["sheets"][uid][SHEET],
                                                                 "channel": "777", "guild": "1", "by": "555"}
        ok, _ = B.ficha_link_consume(code, ch, user, me)
        assert not ok, "código só vale uma vez"
        r = await client.post(LINK, json={"action": "status", "sheet": SHEET}, headers=H())
        body = await r.json()
        assert body["linked"] == {"id": "777", "name": "mesa", "guild": "AKRASIA", "usable": True}, body
        assert body["pending"] is False

        # rolagem com sheet → canal ligado (Ordem 1 e Ordem 2); `channel` antigo é ignorado
        n_p = len(player.sent)
        for roll in ({**OP2, "sheet": sheet_up}, {**BASE, "sheet": SHEET, "channel": "999"}):
            reset()
            r = await client.post(B.FICHA_ROLL_PATH, json=roll, headers=H())
            assert r.status == 200 and (await r.json())["channel_id"] == "777", roll
        assert len(player.sent) == n_p + 2
        assert not player.sent[-1]["allowed_mentions"].everyone
        # sem sheet → padrão, mesmo com `channel`
        reset()
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "channel": "777"}, headers=H())
        assert r.status == 200 and (await r.json())["channel_id"] == str(fake.id)
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "channel": "12ab"}, headers=H())
        assert r.status == 200, "channel inválido já não dá erro (é ignorado)"
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "sheet": "12ab"}, headers=H())
        assert r.status == 400 and "sheet" in (await r.json())["detail"]
        # outra ficha da mesma conta, e a mesma ficha noutra conta → padrão
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "sheet": SHEET2}, headers=H())
        assert (await r.json())["channel_id"] == str(fake.id)
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "sheet": SHEET}, headers=H("a.b.othr"))
        assert (await r.json())["channel_id"] == str(fake.id)

        # quem ligou perdeu a permissão → 403, nada é postado; status diz usable false
        reset()
        can_post["555"] = False
        n_all = len(player.sent) + len(fake.sent)
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "sheet": SHEET}, headers=H())
        assert r.status == 403 and (await r.json())["error"] == "channel_not_allowed"
        assert len(player.sent) + len(fake.sent) == n_all, "nada postado quando recusado"
        r = await client.post(LINK, json={"action": "status", "sheet": SHEET}, headers=H())
        assert (await r.json())["linked"]["usable"] is False
        can_post["555"] = True
        # o Discord recusa o envio → 502, sem cair para o padrão
        async def player_forbidden(**kw):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "no")
        player.send = player_forbidden
        n_def = len(fake.sent)
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "sheet": SHEET}, headers=H())
        assert r.status == 502 and (await r.json())["error"] == "discord_error" and len(fake.sent) == n_def
        del player.send

        # canal inacessível: status mostra os ids, usable false
        B._config["ficha_roll"]["sheets"][uid][SHEET2] = {"channel": "4242", "guild": "31", "by": "555", "at": 1}
        r = await client.post(LINK, json={"action": "status", "sheet": SHEET2}, headers=H())
        assert (await r.json())["linked"] == {"id": "4242", "name": "4242", "guild": "31", "usable": False}

        # desligar → padrão; o código pendente também vai
        r = await client.post(LINK, json={"action": "code", "sheet": SHEET}, headers=H())
        assert (await r.json())["code"] in B._ficha_link_codes
        r = await client.post(LINK, json={"action": "unlink", "sheet": SHEET}, headers=H())
        body = await r.json()
        assert body == {"ok": True, "linked": None, "pending": False, "default": body["default"]} and body["default"]
        assert SHEET not in B._config["ficha_roll"]["sheets"][uid]
        assert not any(v["sheet"] == SHEET for v in B._ficha_link_codes.values())
        reset()
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "sheet": SHEET}, headers=H())
        assert r.status == 200 and (await r.json())["channel_id"] == str(fake.id)

        # limite por conta: 30 pedidos / 60 s
        reset()
        for i in range(B.FICHA_LINK_RATE[0]):
            r = await client.post(LINK, json={"action": "status", "sheet": SHEET}, headers=H())
            assert r.status == 200, i
        r = await client.post(LINK, json={"action": "status", "sheet": SHEET}, headers=H())
        assert r.status == 429 and (await r.json())["error"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1

        # teto global de códigos → 503 busy
        reset()
        old_max = B.FICHA_LINK_MAX_PENDING
        try:
            B.FICHA_LINK_MAX_PENDING = len(B._ficha_link_codes)
            r = await client.post(LINK, json={"action": "code", "sheet": SHEET2}, headers=H())
            assert r.status == 503 and (await r.json())["error"] == "busy"
        finally:
            B.FICHA_LINK_MAX_PENDING = old_max
    finally:
        await client.close()
        (B._ficha_check_token, B._resolve_channel, B._ficha_can_post, B._save_config, B._ficha_resolve_quiet) = orig
        B._ficha_link_codes.clear()
        B._config.pop("ficha_roll", None)
        if saved_cfg is not None:
            B._config["ficha_roll"] = saved_cfg
        B._ficha_roll_channels.clear()


def test_handler_end_to_end_with_fakes():
    asyncio.run(_run_handler_tests())


# ---- /dds: Authorization: DDS <palavra-chave> -----------------------------------------------------------------
DDS_KEY = "chave-de-teste-nao-real"  # inventada: as palavras verdadeiras nunca entram no repositório


def test_dds_keyword_parsing():
    assert B.dds_keyword(f"DDS {DDS_KEY}") == DDS_KEY
    assert B.dds_keyword(f"DDS   {DDS_KEY}  ") == DDS_KEY
    assert B.dds_keyword("DDS duas palavras") == "duas palavras"
    assert B.dds_keyword("DDS " + "k" * B.DDS_KEY_MAX) == "k" * B.DDS_KEY_MAX
    for bad in (None, "", "DDS ", "DDS    ", "DDS " + "k" * (B.DDS_KEY_MAX + 1), "DDS a\x7fb", "DDS a\x00b",
                "dds abc", "DDSabc", "Bearer abc", f"Bearer {DDS_KEY}"):
        assert B.dds_keyword(bad) is None, bad
    # como o aiohttp entrega um header vindo do browser: UTF-8 válido, ou um byte latin-1 ("ã" = 0xE3)
    assert B.dds_keyword("DDS " + "solidão".encode("utf-8").decode("utf-8", "surrogateescape")) == "solidão"
    assert B.dds_keyword("DDS " + "solidão".encode("latin-1").decode("utf-8", "surrogateescape")) == "solidão"
    # os dois esquemas não se confundem
    assert B.bearer_token(f"DDS {DDS_KEY}") is None and B.bearer_token("DDS a.b.c") is None


class _FakeHttpResp:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status, self.body, self.bad_json = status, body, bad_json

    async def json(self, content_type=None):
        if self.bad_json:
            raise ValueError("not json")
        return self.body


class _FakeHttpCtx:
    def __init__(self, outcome):
        self.outcome = outcome

    async def __aenter__(self):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, *exc):
        return False


class _FakeHttpSession:
    """Faz de aiohttp.ClientSession para o rpc dds_enter: responde conforme a palavra-chave."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeHttpCtx(self.answers((json or {}).get("p_key")))


def test_dds_check_calls_the_rpc():
    async def run():
        orig = B._ficha_http_session
        sess = _FakeHttpSession(lambda key: {
            DDS_KEY: _FakeHttpResp(200, "gabi"),
            "errada": _FakeHttpResp(200, None),
            "http500": _FakeHttpResp(500, {"message": "boom"}),
            "http401": _FakeHttpResp(401, {"message": "bad apikey"}),
            "estranha": _FakeHttpResp(200, "GABI; drop"),
            "numero": _FakeHttpResp(200, 7),
            "naojson": _FakeHttpResp(200, bad_json=True),
            "rede": B.aiohttp.ClientConnectionError("down"),
            "tempo": asyncio.TimeoutError(),
        }[key])
        B._ficha_http_session = lambda: sess
        try:
            assert await B._ficha_dds_check(DDS_KEY) == "gabi"
            call = sess.calls[-1]
            assert call["url"] == f"{B.FICHA_SUPABASE_URL}/rest/v1/rpc/dds_enter"
            assert call["json"] == {"p_key": DDS_KEY}
            assert call["headers"]["apikey"] == B.FICHA_SUPABASE_KEY
            assert call["headers"]["Authorization"] == f"Bearer {B.FICHA_SUPABASE_KEY}"
            assert call["headers"]["Content-Profile"] == "ficha_paranormal"
            expected = {"errada": (401, "unauthorized")}
            for key in ("errada", "http500", "http401", "estranha", "numero", "naojson", "rede", "tempo"):
                try:
                    await B._ficha_dds_check(key)
                except B.FichaAuthError as e:
                    assert (e.status, e.code) == expected.get(key, (503, "auth_unavailable")), (key, e.status, e.code)
                else:
                    raise AssertionError(f"{key} should fail")
        finally:
            B._ficha_http_session = orig
    asyncio.run(run())


class _LogCapture(B.logging.Handler):
    def __init__(self):
        super().__init__(level=B.logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args) + " " + str(record.exc_info))


async def _run_dds_handler_tests():
    calls = {"n": 0}

    async def fake_dds_check(keyword):
        calls["n"] += 1
        if keyword in (DDS_KEY, "outra-chave-da-gabi"):
            return "gabi"
        if keyword == "chave-do-leo":
            return "leo"
        if keyword == "supabase-em-baixo":
            raise B.FichaAuthError(503, "auth_unavailable")
        raise B.FichaAuthError(401, "unauthorized")

    async def fake_check(token):
        return "user-" + token[-4:]

    default_ch, dds_ch = _FakeChannel(), _FakeChannel()
    dds_ch.id, dds_ch.guild = 2222, types.SimpleNamespace(id=31)
    channels = {default_ch.id: default_ch, 2222: dds_ch}
    resolves = []

    async def fake_resolve(cid):
        resolves.append(cid)
        return channels.get(cid)

    orig = (B._ficha_dds_check, B._ficha_check_token, B._resolve_channel, B.DDS_ROLL_CHANNEL_ID, B._save_config)
    B._ficha_dds_check, B._ficha_check_token, B._resolve_channel = fake_dds_check, fake_check, fake_resolve
    B.DDS_ROLL_CHANNEL_ID = 2222
    B._save_config = lambda: None
    B._ficha_roll_channels.clear()
    saved_cfg = B._config.pop("ficha_roll", None)
    B.ready_event.set()

    def reset():
        B._ficha_auth_cache.clear()
        B._ficha_auth_neg_cache.clear()
        for lim in (B._ficha_roll_limiter, B._ficha_auth_limiter, B._ficha_auth_global_limiter):
            lim._hits.clear()
    reset()

    logs = _LogCapture()
    root = B.logging.getLogger()
    old_level = root.level
    root.addHandler(logs)
    root.setLevel(B.logging.DEBUG)
    app = web.Application(middlewares=[B._cors_middleware])
    app.add_routes([web.post(B.FICHA_ROLL_PATH, B._handle_ficha_roll),
                    web.route("OPTIONS", "/{tail:.*}", B._handle_options)])
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        D = lambda key=DDS_KEY, **h: {"Authorization": f"DDS {key}", "Origin": "https://ordemcodex.xyz", **h}

        # preflight do browser com o header Authorization (o /dds manda DDS <palavra>)
        r = await client.options(B.FICHA_ROLL_PATH, headers={"Origin": "https://ordemcodex.xyz",
                                                             "Access-Control-Request-Method": "POST",
                                                             "Access-Control-Request-Headers": "authorization,content-type"})
        assert r.status == 204 and r.headers["Access-Control-Allow-Origin"] == "https://ordemcodex.xyz"
        assert "Authorization" in r.headers["Access-Control-Allow-Headers"]

        # palavra mal formada → 401 sem perguntar ao Supabase
        for bad in ("", "   ", "k" * (B.DDS_KEY_MAX + 1)):
            r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D(bad))
            assert r.status == 401 and (await r.json())["error"] == "unauthorized", bad
        assert calls["n"] == 0
        # payload inválido → 400 antes de verificar a palavra
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "dice": [die(value=99)]}, headers=D())
        assert r.status == 400 and calls["n"] == 0

        # palavra certa → canal do /dds (não o padrão), mesma resposta das outras rolagens; `sheet` é ignorado
        B._ficha_channels()["sheets"]["dds:gabi"] = {SHEET: {"channel": "777", "guild": "1", "by": "555", "at": 1}}
        r = await client.post(B.FICHA_ROLL_PATH, json={**OP2, "sheet": SHEET}, headers=D())
        body = await r.json()
        assert r.status == 200 and body == {"ok": True, "message_id": "42", "channel_id": "2222"}, body
        assert r.headers["Access-Control-Allow-Origin"] == "https://ordemcodex.xyz"
        assert len(dds_ch.sent) == 1 and not default_ch.sent and resolves == [2222]
        sent = dds_ch.sent[-1]
        assert sent["embed"].title == "🎲 Rowan - Pontaria"
        assert not sent["allowed_mentions"].everyone and not sent["allowed_mentions"].users
        # qualquer kind válido também passa (op2 é o esperado)
        r = await client.post(B.FICHA_ROLL_PATH, json=BASE, headers=D())
        assert r.status == 200 and (await r.json())["channel_id"] == "2222"
        assert calls["n"] == 1, "palavra confirmada fica em cache (5 min)"
        assert resolves == [2222], "canal do /dds resolvido uma vez e guardado"
        key = "dds:" + B.hashlib.sha256(DDS_KEY.encode()).hexdigest()
        assert B._ficha_auth_cache[key][0] == "gabi"
        assert DDS_KEY not in repr(B._ficha_auth_cache), "cache só guarda o hash"

        # limite por personagem: 8 / 10 s, partilhado por duas palavras da mesma personagem
        reset()
        n = len(dds_ch.sent)
        for i in range(B.FICHA_ROLL_RATE[0]):
            r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D(DDS_KEY if i % 2 else "outra-chave-da-gabi"))
            assert r.status == 200, i
        r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D())
        assert r.status == 429 and int(r.headers["Retry-After"]) >= 1
        assert (await r.json())["error"] == "rate_limited"
        assert len(dds_ch.sent) == n + B.FICHA_ROLL_RATE[0]
        r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D("chave-do-leo"))
        assert r.status == 200, "outra personagem tem o seu limite"

        # palavra errada → 401 e cache negativa (60 s): o Supabase só é chamado uma vez
        reset()
        n0 = calls["n"]
        for _ in range(4):
            r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D("palavra-errada"))
            assert r.status == 401 and (await r.json())["error"] == "unauthorized"
        assert calls["n"] == n0 + 1
        assert ("dds:" + B.hashlib.sha256(b"palavra-errada").hexdigest()) in B._ficha_auth_neg_cache

        # Supabase em baixo → 503 auth_unavailable, sem cache negativa
        reset()
        for _ in range(2):
            r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D("supabase-em-baixo"))
            assert r.status == 503 and (await r.json())["error"] == "auth_unavailable"
        assert not B._ficha_auth_neg_cache

        # verificações novas: mesmos limites por IP (30/min) que os tokens
        reset()
        for i in range(B.FICHA_ROLL_AUTH_RATE[0]):
            r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D(f"errada-{i}", **{"X-Real-IP": "1.2.3.4"}))
            assert r.status == 401
        n0 = calls["n"]
        r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D("errada-x", **{"X-Real-IP": "1.2.3.4"}))
        assert r.status == 429 and "Retry-After" in r.headers and calls["n"] == n0
        # e o mesmo balde que os tokens Bearer (o limite é do IP, não do esquema)
        r = await client.post(B.FICHA_ROLL_PATH, json=OP2,
                              headers={"Authorization": "Bearer a.b.tokn", "X-Real-IP": "1.2.3.4"})
        assert r.status == 429

        # canal do /dds: desligado, inacessível, recusa
        reset()
        B.set_feature(31, "ficha_roll", False)
        r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D())
        assert r.status == 503 and (await r.json())["error"] == "disabled"
        B.set_feature(31, "ficha_roll", True)

        async def send_forbidden(**kw):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        dds_ch.send = send_forbidden
        r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D())
        assert r.status == 502 and (await r.json())["error"] == "discord_error"
        assert "dds" not in B._ficha_roll_channels
        del dds_ch.send
        del channels[2222]
        r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D())
        assert r.status == 503 and (await r.json())["error"] == "channel_unavailable"
        channels[2222] = dds_ch
        r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers=D())
        assert r.status == 200 and (await r.json())["channel_id"] == "2222"

        # Bearer continua igual: padrão, e o canal do /dds não é tocado
        reset()
        n_dds = len(dds_ch.sent)
        r = await client.post(B.FICHA_ROLL_PATH, json=OP2, headers={"Authorization": "Bearer a.b.good"})
        assert r.status == 200 and (await r.json())["channel_id"] == str(default_ch.id)
        assert len(default_ch.sent) == 1 and len(dds_ch.sent) == n_dds

        # a palavra-chave nunca aparece nos logs; a personagem sim
        everything = "\n".join(logs.lines)
        for secret in (DDS_KEY, "outra-chave-da-gabi", "chave-do-leo", "palavra-errada", "supabase-em-baixo"):
            assert secret not in everything, secret
        assert "dds character=gabi" in everything and "dds character=leo" in everything
    finally:
        await client.close()
        root.removeHandler(logs)
        root.setLevel(old_level)
        (B._ficha_dds_check, B._ficha_check_token, B._resolve_channel, B.DDS_ROLL_CHANNEL_ID, B._save_config) = orig
        B._config.pop("ficha_roll", None)
        if saved_cfg is not None:
            B._config["ficha_roll"] = saved_cfg
        B._ficha_roll_channels.clear()
        B.set_feature(31, "ficha_roll", True)


def test_dds_handler_end_to_end_with_fakes():
    asyncio.run(_run_dds_handler_tests())


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok ", name)
    print(f"{len(tests)} tests passed")
