"""Testes das visitas (visita.py). Sem Discord, sem rede: `python3 tests/test_visita.py`."""
import asyncio
import copy
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import som  # noqa: E402
import visita  # noqa: E402

LIS = ZoneInfo("Europe/Lisbon")
CUES = {
    "room": {"kind": "sfx", "file": "room", "loop": True},
    "song": {"kind": "music", "file": "song"},
    "once": {"kind": "sfx", "file": "once"},  # sem loop
}
CONF = {"enabled": True, "channel_id": "111", "start": "22:30", "leave_between": ["22:45", "23:00"],
        "tz": "Europe/Lisbon", "cue": "room"}


def ts(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=LIS).timestamp()


def tone(seconds):
    x = (0.2 * np.sin(np.arange(int(seconds * som.SR)) / 10) * 32767).astype(np.int16)
    return np.stack([x, x], 1)


class FakeStation:
    """O que a Visita usa da Station, sem Discord. `cfg` faz de config.json."""

    def __init__(self, cfg=None):
        self.cfg = cfg if cfg is not None else {"visita": dict(CONF)}
        self.saved = []
        self.mixer = som.Mixer(CUES, clip_loader=lambda cid: som.Clip(data=tone(2)))
        self.joins, self.leaves, self.played = [], 0, []
        self.connected_elsewhere = None

    def _save_cfg(self):
        self.saved.append(copy.deepcopy(self.cfg))

    def busy(self):
        if self.cfg.get("channel"):
            return "canal " + self.cfg["channel"]
        return self.connected_elsewhere

    async def join(self, channel_id):
        self.joins.append(channel_id)
        self.cfg["channel"] = str(channel_id)

    async def leave(self):
        self.leaves += 1
        self.cfg.pop("channel", None)

    async def play_cue(self, cue):
        self.played.append(cue)
        self.mixer.play(cue)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


async def nosleep(_):
    return None


def make(t, st=None, posted=None):
    st = st or FakeStation()
    clock = Clock(t)

    async def poster(url, payload):
        posted.append((url, payload))

    v = visita.Visita(st, now=clock, rng=random.Random(7), sleep=nosleep, poster=poster)
    return v, st, clock


def run(coro):
    return asyncio.run(coro)


# ---- horários -----------------------------------------------------------------
def test_plan_lisbon_winter_and_summer():
    c = visita.validate_config(CONF, CUES)[0]
    w = visita.plan(ts(2026, 1, 15, 12, 0), c)  # inverno: Lisboa = UTC
    assert datetime.fromtimestamp(w["start"], timezone.utc).strftime("%H:%M") == "22:30"
    s = visita.plan(ts(2026, 7, 15, 12, 0), c)  # verão: Lisboa = UTC+1
    assert datetime.fromtimestamp(s["start"], timezone.utc).strftime("%H:%M") == "21:30"
    assert w["date"] == "2026-01-15" and s["date"] == "2026-07-15"


def test_plan_on_dst_change_days():
    c = visita.validate_config(CONF, CUES)[0]
    for day, utc in (((2026, 3, 29), "21:30"), ((2026, 10, 25), "22:30"), ((2026, 3, 28), "22:30")):
        p = visita.plan(ts(*day, 9, 0), c)
        assert p["date"] == "%04d-%02d-%02d" % day
        assert datetime.fromtimestamp(p["start"], timezone.utc).strftime("%H:%M") == utc, (day, p)
        assert p["lo"] - p["start"] == 15 * 60 and p["hi"] - p["lo"] == 15 * 60
    # a manhã da mudança (antes das 01:00 UTC) ainda é o mesmo dia local
    p = visita.plan(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp(), c)
    assert p["date"] == "2026-10-25"


def test_leave_time_is_inside_window_and_visit_starts_at_start():
    v, st, clock = make(ts(2026, 5, 4, 22, 29, 50))
    run(v.tick())
    assert v.visit is None and st.joins == []
    clock.t = ts(2026, 5, 4, 22, 30, 5)
    run(v.tick())
    assert st.joins == [111] and st.played == ["room"]
    assert ts(2026, 5, 4, 22, 45) <= v.visit["leave_at"] <= ts(2026, 5, 4, 23, 0)
    assert "room" in st.mixer.active_cues()


def test_leave_fades_and_leaves_and_never_twice_a_day():
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1))
    run(v.tick())
    clock.t = v.visit["leave_at"] - 1
    run(v.tick())
    assert st.leaves == 0
    clock.t += 2
    run(v.tick())
    assert st.leaves == 1 and v.visit is None and "channel" not in st.cfg
    assert st.mixer.active_cues() == set()  # nada fica preso no mixer para a próxima vez
    clock.t = ts(2026, 5, 4, 22, 31)  # ainda o mesmo dia, dentro de [start, lo)
    run(v.tick())
    assert st.joins == [111]
    clock.t = ts(2026, 5, 5, 22, 30, 10)  # o dia seguinte
    run(v.tick())
    assert st.joins == [111, 111]


def test_days_filter_and_disabled():
    st = FakeStation({"visita": {**CONF, "days": ["sat", 6]}})
    st.cfg["visita"] = visita.validate_config(st.cfg["visita"], CUES)[0]
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1), st)  # segunda
    run(v.tick())
    assert st.joins == []
    clock.t = ts(2026, 5, 9, 22, 30, 1)  # sábado
    run(v.tick())
    assert st.joins == [111]
    st2 = FakeStation({"visita": {**CONF, "enabled": False}})
    v2, st2, _ = make(ts(2026, 5, 4, 22, 30, 1), st2)
    run(v2.tick())
    assert st2.joins == []


def test_late_boot_after_window_opens_is_missed_not_started():
    v, st, clock = make(ts(2026, 5, 4, 22, 50))
    run(v.tick())
    assert st.joins == [] and v.state["last_outcome"] == "missed"


# ---- som em uso ---------------------------------------------------------------
def test_busy_station_skips_the_day():
    st = FakeStation()
    st.cfg["channel"] = "999"  # uma sessão está a usar o som
    st.mixer.play("song")
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1), st)
    run(v.tick())
    assert st.joins == [] and st.played == [] and v.visit is None
    assert v.state["last_outcome"] == "busy" and v.state["last_date"] == "2026-05-04"
    assert "song" in st.mixer.active_cues()  # a sessão não foi tocada
    st.cfg.pop("channel")
    clock.t = ts(2026, 5, 4, 22, 35)
    run(v.tick())  # a sessão acabou mas o dia já foi decidido
    assert st.joins == []


def test_busy_when_voice_client_connected_without_channel_cfg():
    st = FakeStation()
    st.connected_elsewhere = "ligado a Mesa"
    v, st, _ = make(ts(2026, 5, 4, 22, 30, 1), st)
    run(v.tick())
    assert st.joins == [] and v.state["last_outcome"] == "busy"


def test_stale_mixer_voices_do_not_leak_into_visit():
    st = FakeStation()
    st.mixer.play("song")  # sobra de uma sessão antiga, já sem canal
    v, st, _ = make(ts(2026, 5, 4, 22, 30, 1), st)
    run(v.tick())
    assert st.mixer.active_cues() == {"room"}


# ---- tomar conta do som --------------------------------------------------------
def test_takeover_by_join_elsewhere_ends_quietly():
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1))
    run(v.tick())
    st.cfg["channel"] = "222"  # o Tiago carregou em "entrar" noutro canal
    st.mixer.play("song")
    clock.t += 30
    run(v.tick())
    assert v.visit is None and st.leaves == 0 and st.cfg["channel"] == "222"
    assert v.state["last_end"]["outcome"].startswith("takeover")
    assert "song" in st.mixer.active_cues()
    clock.t = ts(2026, 5, 4, 23, 30)
    run(v.tick())
    assert st.leaves == 0 and st.cfg["channel"] == "222"  # nunca sai de um canal que não é seu


def test_takeover_by_playing_another_cue_in_same_channel():
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1))
    run(v.tick())
    st.mixer.play("song")
    clock.t += 20
    run(v.tick())
    assert v.visit is None and st.leaves == 0 and st.cfg["channel"] == "111"
    vol_before = dict(st.mixer.bus_db)
    clock.t = ts(2026, 5, 4, 23, 30)
    run(v.tick())
    assert st.leaves == 0 and st.mixer.bus_db == vol_before
    # a música do Tiago continua; só a deixa da visita sai
    music = [x for x in st.mixer._voices() if x.cue == "song"][0]
    assert music.env.b == 1.0


def test_takeover_by_leave_drops_visit_voice():
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1))
    run(v.tick())
    st.cfg.pop("channel")  # "sair" pelo telemóvel
    clock.t += 20
    run(v.tick())
    assert v.visit is None and st.leaves == 0
    assert st.mixer.active_cues() == set()  # não volta a soar no próximo "entrar"


def test_manual_stop_of_looping_cue_counts_as_takeover():
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1))
    run(v.tick())
    st.mixer.reset()  # como se o "silêncio" tivesse acabado de esvaziar o mixer
    clock.t += 20
    run(v.tick())
    assert v.visit is None and st.played == ["room"]


def test_resume_waits_while_station_is_loading():
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1))
    run(v.tick())
    st2 = FakeStation(copy.deepcopy(st.cfg))
    st2.loading = True
    v2, st2, clock2 = make(ts(2026, 5, 4, 22, 35), st2)
    run(v2.tick())
    assert st2.played == [] and v2.visit is not None
    st2.loading = False
    clock2.t += 20
    run(v2.tick())
    assert st2.played == ["room"]


def test_non_looping_cue_is_replayed_when_it_ends():
    st = FakeStation({"visita": {**CONF, "cue": "once"}})
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1), st)
    run(v.tick())
    st.mixer.reset()
    clock.t += 20
    run(v.tick())
    assert v.visit is not None and st.played == ["once", "once"]


def test_takeover_during_leave_fade_keeps_the_channel():
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1))
    run(v.tick())

    async def tiago_plays(_):
        st.mixer.play("song")  # carregou num botão enquanto a visita saía

    v.sleep = tiago_plays
    clock.t = v.visit["leave_at"] + 1
    run(v.tick())
    assert st.leaves == 0 and st.cfg["channel"] == "111" and v.visit is None
    assert "song" in st.mixer.active_cues()


# ---- reinício ------------------------------------------------------------------
def test_restart_mid_visit_resumes_until_leave_time():
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1))
    run(v.tick())
    leave_at = v.visit["leave_at"]
    disk = copy.deepcopy(st.saved[-1])  # o que ficou no config.json
    disk["channel"] = st.cfg["channel"]
    # o processo morre; um novo arranca com o mixer vazio
    st2 = FakeStation(disk)
    v2, st2, clock2 = make(ts(2026, 5, 4, 22, 40), st2)
    run(v2.tick())
    assert st2.joins == [] and st2.played == ["room"] and v2.visit["leave_at"] == leave_at
    clock2.t = leave_at + 1
    run(v2.tick())
    assert st2.leaves == 1 and v2.visit is None
    clock2.t = ts(2026, 5, 4, 22, 41)
    run(v2.tick())
    assert st2.joins == []  # o mesmo dia não volta a ter visita


def test_restart_after_leave_time_leaves_at_once():
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1))
    run(v.tick())
    disk = copy.deepcopy(st.cfg)
    st2 = FakeStation(disk)
    v2, st2, _ = make(ts(2026, 5, 5, 1, 0), st2)
    slept = []

    async def sleep(x):
        slept.append(x)

    v2.sleep = sleep
    run(v2.tick())
    assert slept == []  # sem fade de 4 s: o watchdog não chega a voltar a entrar
    assert st2.leaves == 1 and st2.played == [] and v2.visit is None and "channel" not in st2.cfg


def test_restart_after_visit_does_not_repeat_the_day():
    v, st, clock = make(ts(2026, 5, 4, 22, 30, 1))
    run(v.tick())
    clock.t = v.visit["leave_at"] + 1
    run(v.tick())
    st2 = FakeStation(copy.deepcopy(st.saved[-1]))
    v2, st2, _ = make(ts(2026, 5, 4, 22, 33), st2)
    run(v2.tick())
    assert st2.joins == []


# ---- visita de teste -------------------------------------------------------------
def test_start_now_runs_for_n_minutes_and_keeps_the_day():
    v, st, clock = make(ts(2026, 5, 4, 20, 0))
    assert run(v.start_now(5)) is None
    assert st.joins == [111] and v.visit["test"] and v.visit["leave_at"] == clock.t + 300
    assert run(v.start_now(5))[1] == "already_in_visit"
    clock.t += 301
    run(v.tick())
    assert st.leaves == 1 and v.visit is None
    clock.t = ts(2026, 5, 4, 22, 30, 1)
    run(v.tick())
    assert st.joins == [111, 111]  # a visita da noite não foi gasta pelo teste


def test_start_now_refuses_when_busy_or_bad_minutes():
    st = FakeStation()
    st.cfg["channel"] = "999"
    v, st, _ = make(ts(2026, 5, 4, 20, 0), st)
    r = run(v.start_now(5))
    assert r[0] == 409 and r[1] == "busy"
    assert st.joins == []
    assert run(v.start_now(0))[0] == 400 and run(v.start_now(31))[0] == 400


# ---- configuração ----------------------------------------------------------------
def test_validate_config():
    c, e = visita.validate_config({"channel_id": 123, "cue": "room"}, CUES)
    assert not e and c["channel_id"] == "123" and c["enabled"] is False and c["tz"] == "Europe/Lisbon"
    assert c["days"] is None and c["leave_between"] == ["22:45", "23:00"]
    bad = [
        {"enabled": True},  # sem canal nem deixa
        {"cue": "nope"},
        {"tz": "Mars/Base"},
        {"start": "25:00"},
        {"leave_between": ["22:20", "23:00"]},  # sai antes de entrar
        {"leave_between": ["23:00", "22:50"]},
        {"channel_id": "abc"},
        {"days": ["funday"]},
        {"enabled": "yes"},
        {"color": "blue"},
    ]
    for b in bad:
        assert visita.validate_config(b, CUES)[1], b
    c, e = visita.validate_config({"days": ["Mon", 2, "sun"]}, CUES)
    assert not e and c["days"] == [0, 2, 6]


# ---- aviso de quem entrou ----------------------------------------------------------
def member(i, name, bot=False):
    return SimpleNamespace(id=i, display_name=name, bot=bot)


def vs(ch):
    return SimpleNamespace(channel=ch)


def test_notify_once_per_member_with_mentions_off(monkey_env=None):
    import os
    os.environ["VISITA_NOTIFY_WEBHOOK"] = "https://discord.example/api/webhooks/1/x"
    try:
        posted = []
        v, st, clock = make(ts(2026, 5, 4, 22, 30, 1), posted=posted)
        room = SimpleNamespace(id=111, name="sala_2")
        other = SimpleNamespace(id=5, name="geral")
        run(v.on_voice_state(member(1, "Ana"), vs(None), vs(room)))  # ainda não há visita
        run(v.tick())
        run(v.on_voice_state(member(1, "*Ana* @everyone"), vs(None), vs(room)))
        run(v.on_voice_state(member(1, "Ana"), vs(room), vs(room)))  # silenciou-se: não conta
        run(v.on_voice_state(member(1, "Ana"), vs(room), vs(None)))
        run(v.on_voice_state(member(1, "Ana"), vs(None), vs(room)))  # voltou: já foi avisado
        run(v.on_voice_state(member(2, "Bot", bot=True), vs(None), vs(room)))
        run(v.on_voice_state(member(3, "Rui"), vs(None), vs(other)))
        run(v.on_voice_state(member(4, "Rita"), vs(other), vs(room)))
        assert len(posted) == 2, posted
        url, p = posted[0]
        assert url.startswith("https://") and p["allowed_mentions"] == {"parse": []}
        assert "\\*Ana\\*" in p["content"] and "@everyone" not in p["content"]
        assert "sala\\_2" in p["content"] and "(22:30)" in p["content"]
        assert v.visit["notified"] == [1, 4]
    finally:
        os.environ.pop("VISITA_NOTIFY_WEBHOOK", None)


def test_notify_skipped_without_webhook():
    import os
    os.environ.pop("VISITA_NOTIFY_WEBHOOK", None)
    posted = []
    v, st, _ = make(ts(2026, 5, 4, 22, 30, 1), posted=posted)
    run(v.tick())
    run(v.on_voice_state(member(1, "Ana"), vs(None), vs(SimpleNamespace(id=111, name="x"))))
    assert posted == []


def test_notify_payload_escapes():
    p = visita.notify_payload("a_b`c|d\n<@1>", "canal", "22:31")
    assert p["allowed_mentions"] == {"parse": []}
    assert "a\\_b\\`c\\|d" in p["content"] and "\n" not in p["content"]
    assert "<@1>" not in p["content"] and p["content"].endswith("(22:31)")


# ---- rotas HTTP (Station real, discord falso) ----------------------------------------
def test_routes_auth_get_put_and_now():
    import json
    import tempfile
    import types
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    if "discord" not in sys.modules:
        try:
            import discord  # noqa: F401
        except ImportError:
            sys.modules["discord"] = types.SimpleNamespace(AudioSource=object)
    root = Path(tempfile.mkdtemp(prefix="visita-test-"))
    (root / "files").mkdir()
    for f in ("room", "song", "once"):
        (root / "files" / f).write_bytes(b"x")
    (root / "cues.json").write_text(json.dumps(CUES))
    st = som.Station(SimpleNamespace(voice_clients=[]), root, "tok")
    joined = []

    async def join(cid):
        joined.append(cid); st.cfg["channel"] = str(cid); st._save_cfg()

    async def play_cue(cue):
        joined.append(cue)

    st.join, st.play_cue = join, play_cue
    H = {"Authorization": "Bearer tok"}

    async def go():
        app = web.Application(); app.add_routes(st.routes())
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/som/visita")).status == 401
            assert (await c.get("/som/visita", headers={"Authorization": "Bearer nope"})).status == 401
            r = await (await c.get("/som/visita", headers=H)).json()
            assert r["ok"] and r["config"]["enabled"] is False and r["state"]["in_visit"] is False
            r = await c.put("/som/visita", headers=H, json={"enabled": True, "cue": "room"})
            assert r.status == 400 and (await r.json())["error"] == "invalid"
            r = await c.post("/som/visita/agora", headers=H, json={"minutes": 3})
            assert r.status == 400 and (await r.json())["error"] == "not_configured"
            r = await c.put("/som/visita", headers=H, json={"enabled": True, "cue": "room", "channel_id": "111"})
            assert r.status == 200 and (await r.json())["config"]["channel_id"] == "111"
            assert json.loads((root / "config.json").read_text())["visita"]["cue"] == "room"
            r = await c.post("/som/visita/agora", headers=H, json={"minutes": 99})
            assert r.status == 400
            r = await c.post("/som/visita/agora", headers=H, json={"minutes": 2})
            body = await r.json()
            assert r.status == 200 and body["state"]["in_visit"] and body["state"]["test"], body
            assert joined == [111, "room"]
            r = await c.post("/som/visita/agora", headers=H, json={"minutes": 2})
            assert r.status == 409

    run(go())


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); n += 1; print("ok", name)
    print(n, "testes passaram")
