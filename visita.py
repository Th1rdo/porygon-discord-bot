"""Visitas: o Ajudante entra sozinho num canal de voz a uma hora marcada, toca uma deixa e sai.

Fica por cima da Station (som.py) e usa só o que ela já sabe fazer (entrar, tocar, `fade_all`, sair).
A configuração vive no `config.json` da Station, em `"visita"`; o estado (dia já decidido, visita em
curso) em `"visita_state"`, para sobreviver a reinícios.

Regras:
- À hora `start`, num dia ainda não decidido (e em `days`, se houver), se a Station já estiver num
  canal o som está em uso (uma sessão): o dia é saltado e fica registado porquê. Senão entra em
  `channel_id`, toca `cue` e sorteia a hora de saída dentro de `leave_between`.
- À hora de saída: `fade_all` e sai.
- Se alguém pegar no som durante a visita (entrar noutro canal, tocar outra deixa, sair, parar a
  deixa), a visita acaba em silêncio: não sai do canal, não mexe em volumes nem no que está a tocar.
  Só a própria deixa da visita é retirada, para não ficar a tocar por baixo de quem pegou no som.
- Um dia nunca tem duas visitas. Se o bot reiniciar a meio, retoma até à hora de saída.
- Quem entrar no canal durante a visita gera um aviso (uma vez por pessoa e visita) no webhook do
  Discord em `VISITA_NOTIFY_WEBHOOK`, se existir.

O núcleo (`plan`, `validate_config`, `notify_payload`, `Visita`) não depende do discord.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from datetime import date as Date
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("som.visita")

DEFAULTS = {
    "enabled": False,
    "channel_id": None,
    "start": "22:30",
    "leave_between": ["22:45", "23:00"],
    "tz": "Europe/Lisbon",
    "cue": None,
    "days": None,  # None = todos; senão lista de 0..6 (segunda = 0)
}
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


# ---- horários -----------------------------------------------------------------
def parse_hhmm(s) -> tuple[int, int]:
    m = _HHMM.match(str(s if s is not None else "").strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValueError(f"hora inválida: {s!r} (usa HH:MM)")
    return int(m[1]), int(m[2])


def at_local(day: Date, hhmm: str, tz: ZoneInfo) -> float:
    """Instante (epoch) de `hhmm` no dia local `day`, no fuso `tz` (com hora de verão)."""
    h, m = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz).timestamp()


def plan(now: float, conf: dict) -> dict:
    """Os instantes de hoje (dia local de `now`): entrada, início e fim da janela de saída."""
    tz = ZoneInfo(conf["tz"])
    day = datetime.fromtimestamp(now, tz).date()
    lo, hi = conf["leave_between"]
    days = conf.get("days")
    return {"date": day.isoformat(), "day_ok": days is None or day.weekday() in days,
            "start": at_local(day, conf["start"], tz), "lo": at_local(day, lo, tz), "hi": at_local(day, hi, tz)}


def _iso(t: float | None, tz: str) -> str | None:
    if t is None:
        return None
    try:
        return datetime.fromtimestamp(t, ZoneInfo(tz)).isoformat(timespec="seconds")
    except Exception:
        return None


# ---- configuração -------------------------------------------------------------
def validate_config(d, cues: dict) -> tuple[dict | None, list[str]]:
    """Valida e completa com os valores por omissão. Devolve (config, erros)."""
    if not isinstance(d, dict):
        return None, ["o corpo tem de ser um objeto JSON"]
    errs = []
    unknown = sorted(set(d) - set(DEFAULTS))
    if unknown:
        errs.append("chaves desconhecidas: " + ", ".join(unknown))
    c = {**DEFAULTS, **{k: v for k, v in d.items() if k in DEFAULTS}}
    c["leave_between"] = list(c["leave_between"]) if isinstance(c["leave_between"], (list, tuple)) else c["leave_between"]

    if not isinstance(c["enabled"], bool):
        errs.append("enabled tem de ser true ou false")
    if c["channel_id"] is not None:
        ch = str(c["channel_id"]).strip()
        if not ch.isdigit():
            errs.append("channel_id tem de ser o id numérico do canal de voz")
        c["channel_id"] = ch or None
    try:
        ZoneInfo(str(c["tz"]))
    except Exception:
        errs.append(f"fuso desconhecido: {c['tz']!r}")
    try:
        lb = c["leave_between"]
        if not isinstance(lb, list) or len(lb) != 2:
            raise ValueError("leave_between tem de ser [\"HH:MM\", \"HH:MM\"]")
        s, lo, hi = (h * 60 + m for h, m in map(parse_hhmm, [c["start"], lb[0], lb[1]]))
        if not s < lo <= hi:
            raise ValueError("tem de ser start < leave_between[0] <= leave_between[1], no mesmo dia")
    except ValueError as e:
        errs.append(str(e))
    if c["cue"] is not None and c["cue"] not in cues:
        errs.append(f"deixa desconhecida: {c['cue']!r}")
    if c["days"] is not None:
        days = c["days"] if isinstance(c["days"], list) else [c["days"]]
        norm = set()
        for x in days:
            if isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 6:
                norm.add(x)
            elif isinstance(x, str) and x.strip().lower()[:3] in DAY_NAMES:
                norm.add(DAY_NAMES.index(x.strip().lower()[:3]))
            else:
                errs.append(f"dia inválido: {x!r} (0-6 ou mon..sun)")
        c["days"] = sorted(norm)
    if c["enabled"] is True and (not c["channel_id"] or not c["cue"]):
        errs.append("para ligar são precisos channel_id e cue")
    return (None, errs) if errs else (c, [])


# ---- aviso de quem entrou ----------------------------------------------------
_MD = re.compile(r"([\\*_~`|>#\-\[\]()<:])")


def escape_md(s, limit: int = 100) -> str:
    """Texto seguro para uma mensagem do Discord: uma linha, markdown escapado, menções partidas."""
    s = " ".join(str(s).split())[:limit]
    return _MD.sub(r"\\\1", s).replace("@", "@​")


def notify_payload(name: str, channel: str, hhmm: str) -> dict:
    return {"content": f"👂 {escape_md(name)} entrou em {escape_md(channel)} durante a visita ({hhmm})",
            "allowed_mentions": {"parse": []}}


async def post_json(url: str, payload: dict):
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.post(url, json=payload) as r:
            if r.status >= 300:
                log.warning("visita: o webhook respondeu %s", r.status)


# ---- o agendador ---------------------------------------------------------------
class Visita:
    """`station` precisa de: cfg (dict), _save_cfg(), mixer, busy() -> str|None,
    async join(channel_id), async leave(), async play_cue(cue)."""

    TICK = 20.0
    LEAVE_FADE = 4.0
    TAKEOVER_FADE = 3.0

    def __init__(self, station, now=time.time, rng: random.Random | None = None, sleep=asyncio.sleep, poster=None):
        self.st = station
        self.now = now
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.poster = poster or post_json
        self._ours = False  # a deixa da visita foi posta a tocar por este processo
        self._lock = asyncio.Lock()

    # -- estado persistente
    @property
    def conf(self) -> dict:
        return {**DEFAULTS, **(self.st.cfg.get("visita") or {})}

    @property
    def state(self) -> dict:
        s = self.st.cfg.get("visita_state")
        if not isinstance(s, dict):
            s = self.st.cfg["visita_state"] = {}
        for k in ("last_date", "last_outcome", "visit", "last_end"):
            s.setdefault(k, None)
        return s

    @property
    def visit(self) -> dict | None:
        return self.state.get("visit")

    def _save(self):
        self.st._save_cfg()

    def set_config(self, d) -> list[str]:
        c, errs = validate_config(d, self.st.mixer.cues)
        if errs:
            return errs
        self.st.cfg["visita"] = c
        self._save()
        return []

    # -- ciclo
    async def loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("visita: tick")
            await self.sleep(self.TICK)

    async def tick(self):
        async with self._lock:
            now = self.now()
            if self.visit:
                return await self._tick_visit(now)
            c = self.conf
            if not c["enabled"] or not c["channel_id"] or not c["cue"]:
                return
            p = plan(now, c)
            s = self.state
            if not p["day_ok"] or s["last_date"] == p["date"] or now < p["start"]:
                return
            if now >= p["lo"]:
                self._decide(p["date"], "missed")
                log.info("visita: %s sem visita (o bot só ficou pronto depois de %s)", p["date"], c["leave_between"][0])
                return
            why = self.st.busy()
            if why:
                self._decide(p["date"], "busy")
                log.info("visita: %s sem visita, o som está em uso (%s)", p["date"], why)
                return
            await self._start(c["channel_id"], c["cue"], self.rng.uniform(p["lo"], p["hi"]), test=False, day=p["date"])

    def _decide(self, day: str, outcome: str):
        s = self.state
        s["last_date"], s["last_outcome"] = day, outcome
        self._save()

    async def _start(self, channel: str, cue: str, leave_at: float, test: bool, day: str | None = None):
        s = self.state
        prev = (s["last_date"], s["last_outcome"])
        # sem canal nada está a soar; restos de uma sessão antiga no mixer não podem entrar na visita
        self.st.mixer.reset()
        if day:
            s["last_date"], s["last_outcome"] = day, "visited"
        s["visit"] = {"channel": str(channel), "cue": cue, "started_at": self.now(), "leave_at": leave_at,
                      "test": test, "notified": []}
        self._save()  # antes de entrar: se o processo morrer aqui, o dia já está gasto
        self._ours = False
        try:
            await self.st.join(int(channel))
        except Exception as e:
            if self.st.cfg.get("channel") != str(channel):
                log.exception("visita: não consegui entrar no canal %s", channel)
                s["visit"] = None
                s["last_date"], s["last_outcome"] = prev
                self._save()
                return f"{type(e).__name__}: {e}"
            log.warning("visita: erro ao entrar (%s), mas o canal ficou marcado; o watchdog trata", e)
        await self._ensure_cue(s["visit"])
        log.info("visita: %s no canal %s até %s", "teste" if test else "início", channel,
                 _iso(leave_at, self.conf["tz"]))
        return None

    def _cue_loops(self, cue: str) -> bool:
        c = self.st.mixer.cues.get(cue, {})
        return bool(c.get("loop")) or any(x.get("loop") for x in c.get("sections") or [])

    def _taken_over(self, v: dict) -> str | None:
        ch = self.st.cfg.get("channel")
        if ch != v["channel"]:
            return "moved" if ch else "left"
        others = self.st.mixer.active_cues() - {v["cue"]}
        if others:
            return "played " + ",".join(sorted(others))
        if self._ours and v["cue"] not in self.st.mixer.active_cues() and self._cue_loops(v["cue"]):
            return "stopped"
        return None

    async def _tick_visit(self, now: float):
        v = self.visit
        why = self._taken_over(v)
        if why:
            return self._end_quietly(v, why)
        if now >= v["leave_at"]:
            return await self._leave(v, "done")
        await self._ensure_cue(v)

    async def _ensure_cue(self, v: dict):
        """Reinício (mixer novo) ou deixa sem loop que acabou: volta a tocar."""
        if v["cue"] in self.st.mixer.active_cues():
            return
        if getattr(self.st, "loading", False):  # a Station ainda está a descodificar tudo: fica para o próximo tick
            return
        try:
            await self.st.play_cue(v["cue"])
            self._ours = True
        except KeyError:
            log.warning("visita: a deixa %s já não existe", v["cue"])
            await self._leave(v, "cue_missing")

    def _end_quietly(self, v: dict, why: str):
        m = self.st.mixer
        if self.st.cfg.get("channel"):
            m.stop(v["cue"], self.TAKEOVER_FADE)  # só a deixa da visita; o resto é de quem pegou no som
        else:
            m.drop(v["cue"])  # sem canal o mixer não anda: tirar já, senão volta no próximo "entrar"
        log.info("visita: acabou porque alguém pegou no som (%s)", why)
        self._finish("takeover: " + why, v)

    async def _leave(self, v: dict, outcome: str):
        # muito depois da hora (o bot esteve em baixo): sair já, antes que o watchdog volte a entrar
        if self.now() - v["leave_at"] < 3 * self.TICK:
            self.st.mixer.fade_all(self.LEAVE_FADE)
            await self.sleep(self.LEAVE_FADE + 0.5)
        why = self._taken_over(v) if outcome == "done" else None
        if why and why != "stopped":  # alguém pegou no som durante o fade: fica com ele
            log.info("visita: alguém pegou no som durante a saída (%s)", why)
            return self._finish("takeover: " + why, v)
        await self.st.leave()
        self.st.mixer.drop(v["cue"])
        log.info("visita: saiu (%s)", outcome)
        self._finish(outcome, v)

    def _finish(self, outcome: str, v: dict):
        s = self.state
        s["visit"] = None
        s["last_end"] = {"outcome": outcome, "at": self.now(), "test": bool(v.get("test")),
                         "listeners": len(v.get("notified") or [])}
        self._ours = False
        self._save()

    # -- visita de teste
    async def start_now(self, minutes) -> tuple[int, str, str] | None:
        """Começa já uma visita de `minutes` minutos (1..30). Não gasta o dia. None = começou."""
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            return 400, "bad_minutes", "minutes tem de ser um número de 1 a 30"
        if not 1 <= minutes <= 30:
            return 400, "bad_minutes", "minutes tem de ser um número de 1 a 30"
        async with self._lock:
            if self.visit:
                return 409, "already_in_visit", "já há uma visita a decorrer"
            c = self.conf
            if not c["channel_id"] or not c["cue"]:
                return 400, "not_configured", "faltam channel_id e cue em PUT /som/visita"
            if c["cue"] not in self.st.mixer.cues:
                return 400, "unknown_cue", c["cue"]
            why = self.st.busy()
            if why:
                return 409, "busy", why
            err = await self._start(c["channel_id"], c["cue"], self.now() + minutes * 60, test=True)
            return (500, "join_failed", err) if err else None

    # -- quem entra
    async def on_voice_state(self, member, before, after):
        v = self.visit
        if not v or getattr(member, "bot", False):
            return
        ach, bch = getattr(after, "channel", None), getattr(before, "channel", None)
        if ach is None or str(ach.id) != v["channel"] or (bch is not None and bch.id == ach.id):
            return
        if member.id in v["notified"]:
            return
        v["notified"].append(member.id)
        self._save()
        url = os.getenv("VISITA_NOTIFY_WEBHOOK", "").strip()
        if not url:
            return
        if not url.startswith("https://"):
            log.warning("visita: VISITA_NOTIFY_WEBHOOK tem de ser https")
            return
        hhmm = datetime.fromtimestamp(self.now(), ZoneInfo(self.conf["tz"])).strftime("%H:%M")
        try:
            await self.poster(url, notify_payload(getattr(member, "display_name", "?"), getattr(ach, "name", "?"), hhmm))
        except Exception as e:
            log.warning("visita: aviso falhou: %s: %s", type(e).__name__, e)

    # -- para GET /som/visita
    def describe(self) -> dict:
        c, s, v = self.conf, self.state, self.visit
        out = {"in_visit": bool(v), "last_date": s["last_date"], "last_outcome": s["last_outcome"],
               "last_end": s["last_end"]}
        try:
            p = plan(self.now(), c)
            decided = s["last_date"] == p["date"]
            out.update({"today": p["date"], "today_scheduled": p["day_ok"],
                        "visited_today": decided and s["last_outcome"] == "visited",
                        "today_outcome": s["last_outcome"] if decided else None,
                        "start_at": _iso(p["start"], c["tz"]),
                        "leave_window": [_iso(p["lo"], c["tz"]), _iso(p["hi"], c["tz"])]})
        except Exception as e:
            out["error"] = str(e)
        if v:
            out.update({"channel": v["channel"], "cue": v["cue"], "test": v["test"],
                        "started_at": _iso(v["started_at"], c["tz"]), "leave_at": _iso(v["leave_at"], c["tz"]),
                        "listeners": len(v["notified"])})
        return {"config": c, "state": out}
