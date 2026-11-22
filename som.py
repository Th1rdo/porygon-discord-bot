"""Som: o Ajudante toca música, fitas e efeitos num canal de voz, comandado do telemóvel.

Mistura em numpy (48 kHz estéreo), codifica Opus com o PyAV (traz o seu FFmpeg com libopus, por
isso o Railway não precisa de pacotes de sistema) e entrega pacotes de 20 ms ao discord.py.

Três barramentos:
- **música**: uma faixa de cada vez. Secções (intro, loop, fim) com costura cruzada no loop; trocar de
  faixa segue a regra da Trilha do Tiago (a velha sai em `fade` s, a nova entra quando a saída vai a 75%).
  `break` deixa o loop acabar e seguir para o resto do ficheiro; `next` salta já para a secção seguinte.
- **fitas**: uma de cada vez (uma nova corta a anterior); baixam a música (`duck_db`) enquanto tocam.
- **efeitos**: disparam por cima de tudo, vários ao mesmo tempo.

O motor (`Mixer`) é puro e testável sem Discord: `Mixer.render(seg)` devolve o que soaria.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from pathlib import Path

import numpy as np

SR = 48000
BLOCK = 960  # 20 ms, o pacote do Discord

log = logging.getLogger("som")


# ---- utilidades -------------------------------------------------------------
def db(x: float) -> float:
    return 10.0 ** (x / 20.0)


def decode_to_pcm(src: Path, dst: Path) -> int:
    """Descodifica qualquer ficheiro de áudio para s16le 48 kHz estéreo em `dst`. Devolve n.º de frames."""
    import av  # só quando é preciso: os testes do motor não dependem disto
    tmp = dst.with_suffix(".tmp")
    n = 0
    with av.open(str(src)) as c, open(tmp, "wb") as out:
        stream = c.streams.audio[0]
        rs = av.AudioResampler(format="s16", layout="stereo", rate=SR)
        for frame in c.decode(stream):
            for f in rs.resample(frame):
                a = f.to_ndarray()  # (1, n*2) packed s16
                out.write(a.tobytes()); n += a.size // 2
        for f in rs.resample(None):
            a = f.to_ndarray(); out.write(a.tobytes()); n += a.size // 2
    os.replace(tmp, dst)
    return n


class Clip:
    """Um ficheiro descodificado, lido do disco (memmap) para não encher a memória."""

    def __init__(self, pcm_path: Path | None = None, data: np.ndarray | None = None):
        if data is not None:
            self.pcm = data.astype(np.int16, copy=False).reshape(-1, 2)
        else:
            raw = np.memmap(pcm_path, dtype=np.int16, mode="r")
            self.pcm = raw[: raw.size // 2 * 2].reshape(-1, 2)
        self.n = self.pcm.shape[0]

    def get(self, a: int, m: int) -> np.ndarray:
        """m frames a partir de a, em float32; zeros fora do ficheiro."""
        out = np.zeros((m, 2), dtype=np.float32)
        lo, hi = max(a, 0), min(a + m, self.n)
        if hi > lo:
            out[lo - a: hi - a] = self.pcm[lo:hi] / 32768.0
        return out


# ---- envelopes ---------------------------------------------------------------
class Env:
    """Ganho que vai de `a` a `b` em `n` frames, com curva de igual potência (sin para subir, cos para descer)."""

    def __init__(self, level: float = 1.0):
        self.a = self.b = level
        self.n = 0
        self.k = 0

    @property
    def level(self) -> float:
        return self._at(self.k)

    def _at(self, k):
        if self.n <= 0:
            return np.full(np.shape(k), self.b, dtype=np.float32) if np.ndim(k) else self.b
        t = np.clip(np.asarray(k, dtype=np.float32) / self.n, 0.0, 1.0)
        if self.b >= self.a:
            c = np.sin(t * math.pi / 2)
        else:
            c = 1.0 - np.cos(t * math.pi / 2)
        v = self.a + (self.b - self.a) * c
        return v if np.ndim(k) else float(v)

    def to(self, level: float, seconds: float):
        cur = self.level
        self.a, self.b = cur, level
        self.n = max(int(seconds * SR), 0)
        self.k = 0

    def block(self, m: int) -> np.ndarray:
        g = self._at(np.arange(self.k, self.k + m))
        self.k += m
        if self.n and self.k >= self.n:
            self.a, self.n, self.k = self.b, 0, 0
        return g.astype(np.float32)

    @property
    def done_at_zero(self) -> bool:
        return self.n == 0 and self.b <= 0.0


# ---- vozes -------------------------------------------------------------------
class Voice:
    """Uma reprodução de um clip. Sabe de secções, loops com costura e atraso inicial."""

    def __init__(self, cue_id: str, clip: Clip, gain: float, sections: list[dict] | None,
                 start: int = 0, delay: int = 0, fade_in: float = 0.0):
        self.cue = cue_id
        self.clip = clip
        self.gain = gain
        self.sections = sorted(sections or [], key=lambda s: s["start"])
        self.pos = start
        self.delay = delay
        self.env = Env(0.0 if fade_in > 0 else 1.0)
        if fade_in > 0:
            self.env.to(1.0, fade_in)
        self.broken: set[int] = set()  # secções cujo loop já foi interrompido
        self.passes: dict[int, int] = {}
        self.finished = False

    # secção onde está `pos`
    def section_index(self) -> int | None:
        for i, s in enumerate(self.sections):
            if self._f(s["start"]) <= self.pos < self._f(s["end"]):
                return i
        return None

    @staticmethod
    def _f(seconds: float) -> int:
        return int(round(seconds * SR))

    def _looping(self, i: int | None) -> bool:
        if i is None:
            return False
        s = self.sections[i]
        if not s.get("loop") or i in self.broken:
            return False
        reps = int(s.get("repeats") or 0)
        return reps == 0 or self.passes.get(i, 0) < reps - 1

    def read(self, m: int) -> np.ndarray:
        out = np.zeros((m, 2), dtype=np.float32)
        w = 0
        while w < m and not self.finished:
            i = self.section_index()
            if self._looping(i):
                s = self.sections[i]
                a, e = self._f(s["start"]), self._f(s["end"])
                seam = min(self._f(float(s.get("seam") or 0.0)), (e - a) // 2)
                z = e - seam  # início da costura
                if self.pos < z:
                    k = min(m - w, z - self.pos)
                    out[w:w + k] = self.clip.get(self.pos, k)
                    self.pos += k; w += k
                    if seam == 0 and self.pos >= e:
                        self.pos = a; self.passes[i] = self.passes.get(i, 0) + 1
                    continue
                # na costura: o fim da passagem a sair, o início da próxima a entrar
                j = self.pos - z
                k = min(m - w, seam - j)
                t = (np.arange(j, j + k, dtype=np.float32) + 0.5) / seam
                old = self.clip.get(self.pos, k)
                new = self.clip.get(a + j, k)
                out[w:w + k] = old * np.cos(t * math.pi / 2)[:, None] + new * np.sin(t * math.pi / 2)[:, None]
                self.pos += k; w += k
                if self.pos >= e:
                    self.pos = a + seam
                    self.passes[i] = self.passes.get(i, 0) + 1
            else:
                end = self.clip.n
                if i is not None:  # parar no fim da secção para voltar a decidir (a seguinte pode ter loop)
                    end = min(end, self._f(self.sections[i]["end"]))
                else:
                    nxt = [self._f(s["start"]) for s in self.sections if self._f(s["start"]) > self.pos]
                    if nxt:
                        end = min(end, min(nxt))
                if self.pos >= self.clip.n:
                    self.finished = True
                    break
                k = min(m - w, max(end - self.pos, 1))
                out[w:w + k] = self.clip.get(self.pos, k)
                self.pos += k; w += k
        return out

    def render(self, m: int) -> np.ndarray:
        # o atraso (a nova faixa à espera, regra da Trilha) não gasta o envelope
        d = min(self.delay, m)
        self.delay -= d
        x = np.zeros((m, 2), dtype=np.float32)
        if d < m:
            g = self.env.block(m - d) * self.gain
            x[d:] = self.read(m - d) * g[:, None]
        if self.env.done_at_zero:
            self.finished = True
        return x


# ---- mesa de mistura ---------------------------------------------------------
class Mixer:
    """Estado do som. Todos os comandos e `render` passam pelo mesmo lock (o discord.py lê noutra thread)."""

    def __init__(self, cues: dict | None = None, clip_loader=None):
        self.lock = threading.RLock()
        self.cues: dict = cues or {}
        self.clip_loader = clip_loader  # cue_id -> Clip
        self.clips: dict[str, Clip] = {}
        self.music: Voice | None = None
        self.music_out: list[Voice] = []
        self.tape: Voice | None = None
        self.tapes_out: list[Voice] = []
        self.sfx: list[Voice] = []
        self.bus = {"master": Env(db(-3)), "music": Env(1.0), "tape": Env(1.0), "sfx": Env(1.0)}
        self.bus_db = {"master": -3.0, "music": 0.0, "tape": 0.0, "sfx": 0.0}
        self.duck = Env(1.0)
        self.duck_db = -9.0
        self.frames = 0
        self.last_peak = 0.0

    # -- clips
    def clip(self, cue_id: str) -> Clip:
        c = self.cues[cue_id]
        key = c.get("file", cue_id)
        if key not in self.clips:
            self.clips[key] = self.clip_loader(cue_id)
        return self.clips[key]

    def preload(self) -> list[str]:
        """Descodifica tudo de antemão (corre numa thread à parte). Devolve as deixas que falharam."""
        bad = []
        for cid in list(self.cues):
            try:
                self.clip(cid)
            except Exception:
                log.exception("som: não consegui carregar %s", cid)
                bad.append(cid)
        return bad

    def _gain(self, cue_id: str) -> float:
        return db(float(self.cues[cue_id].get("gain_db", 0.0)))

    # -- comandos
    def play(self, cue_id: str, fade: float | None = None) -> dict:
        if cue_id not in self.cues:
            raise KeyError(cue_id)
        kind = self.cues[cue_id].get("kind", "sfx")
        self.clip(cue_id)  # descodificar fora do lock: o som não pode parar à espera disto
        with self.lock:
            if kind == "music":
                return self._play_music(cue_id, fade)
            if kind == "tape":
                return self._play_tape(cue_id, fade)
            return self._play_sfx(cue_id, fade)

    def _play_music(self, cue_id, fade):
        c = self.cues[cue_id]
        sections = c.get("sections") or [{"name": "Inteira", "start": 0.0, "end": self.clip(cue_id).n / SR, "loop": bool(c.get("loop"))}]
        if self.music is not None and not self.music.finished:
            out = float(c.get("crossfade", 5.5) if fade is None else fade)
            old = self.music
            old.env.to(0.0, out)
            self.music_out.append(old)
            delay = int(out * 0.75 * SR)
            fin = out
        else:
            delay = 0
            fin = float(c.get("fade_in", 1.5) if fade is None else fade)
        self.music = Voice(cue_id, self.clip(cue_id), self._gain(cue_id), sections, 0, delay, fin)
        return {"music": cue_id, "fade_in": fin, "starts_in": round(delay / SR, 2)}

    def _play_tape(self, cue_id, fade):
        c = self.cues[cue_id]
        if self.tape is not None and not self.tape.finished:
            self.tape.env.to(0.0, 0.3); self.tapes_out.append(self.tape)
        sections = [{"name": "Loop", "start": 0.0, "end": self.clip(cue_id).n / SR, "loop": True, "seam": float(c.get("seam", 0.8))}] if c.get("loop") else []
        self.tape = Voice(cue_id, self.clip(cue_id), self._gain(cue_id), sections, 0, 0, float(fade or 0.02))
        if c.get("duck", True):
            self.duck.to(db(self.duck_db), 0.8)
        return {"tape": cue_id, "duck_db": self.duck_db if c.get("duck", True) else 0}

    def _play_sfx(self, cue_id, fade):
        c = self.cues[cue_id]
        sections = [{"name": "Loop", "start": 0.0, "end": self.clip(cue_id).n / SR, "loop": True, "seam": float(c.get("seam", 0.3))}] if c.get("loop") else []
        if len(self.sfx) >= 12:
            self.sfx[0].env.to(0.0, 0.1)
        self.sfx.append(Voice(cue_id, self.clip(cue_id), self._gain(cue_id), sections, 0, 0, float(fade or 0.005)))
        return {"sfx": cue_id}

    def stop(self, target: str = "music", fade: float | None = None) -> dict:
        """target: 'music', 'tape', 'sfx', ou o id de uma deixa."""
        with self.lock:
            hit = []
            for v in self._voices():
                kind = self.cues.get(v.cue, {}).get("kind", "sfx")
                if target in (kind, v.cue):
                    f = fade if fade is not None else {"music": 4.0, "tape": 0.5}.get(kind, 0.3)
                    v.env.to(0.0, f); hit.append(v.cue)
            return {"stopped": hit}

    def break_loop(self) -> dict:
        with self.lock:
            v = self.music
            if v is None or v.finished:
                return {"break": None}
            i = v.section_index()
            if i is not None:
                v.broken.add(i)
            return {"break": v.cue, "section": None if i is None else v.sections[i].get("name")}

    def next_section(self, fade: float = 1.0) -> dict:
        with self.lock:
            v = self.music
            if v is None or v.finished:
                return {"next": None}
            i = v.section_index()
            nxt = (i + 1) if i is not None else None
            v.env.to(0.0, fade); self.music_out.append(v)
            if nxt is None or nxt >= len(v.sections):
                self.music = None
                return {"next": "fim"}
            nv = Voice(v.cue, v.clip, v.gain, v.sections, v._f(v.sections[nxt]["start"]), 0, fade)
            self.music = nv
            return {"next": v.sections[nxt].get("name")}

    def fade_all(self, fade: float = 3.0) -> dict:
        with self.lock:
            for v in self._voices():
                v.env.to(0.0, fade)
            return {"fade_all": fade}

    def silence(self) -> dict:
        return self.fade_all(0.2)

    def set_volume(self, bus: str, value_db: float, seconds: float = 0.5) -> dict:
        with self.lock:
            if bus not in self.bus:
                raise KeyError(bus)
            value_db = max(-60.0, min(12.0, float(value_db)))
            self.bus_db[bus] = value_db
            self.bus[bus].to(db(value_db), seconds)
            return {"bus": bus, "db": value_db}

    def nudge(self, bus: str, step_db: float) -> dict:
        return self.set_volume(bus, self.bus_db.get(bus, 0.0) + step_db)

    def active_cues(self) -> set[str]:
        with self.lock:
            return {v.cue for v in self._voices()}

    def drop(self, cue_id: str) -> None:
        """Tira já (sem fade) as vozes de uma deixa. Só para quando o leitor está parado."""
        with self.lock:
            for v in self._voices():
                if v.cue == cue_id:
                    v.finished = True
            self.music_out = [v for v in self.music_out if not v.finished]
            self.tapes_out = [v for v in self.tapes_out if not v.finished]
            self.sfx = [v for v in self.sfx if not v.finished]
            if self.music is not None and self.music.finished:
                self.music = None
            if self.tape is not None and self.tape.finished:
                self.tape = None

    def reset(self) -> None:
        """Esvazia todas as vozes (os volumes ficam). Só para quando o leitor está parado."""
        with self.lock:
            self.music, self.tape = None, None
            self.music_out, self.tapes_out, self.sfx = [], [], []
            self.duck = Env(1.0)

    def _voices(self) -> list[Voice]:
        vs = [v for v in [self.music, self.tape] if v is not None] + self.music_out + self.tapes_out + self.sfx
        return [v for v in vs if not v.finished]

    # -- som
    def render(self, m: int = BLOCK) -> np.ndarray:
        with self.lock:
            mus = np.zeros((m, 2), np.float32)
            for v in [self.music] + self.music_out:
                if v is not None and not v.finished:
                    mus += v.render(m)
            tap = np.zeros((m, 2), np.float32)
            for v in [self.tape] + self.tapes_out:
                if v is not None and not v.finished:
                    tap += v.render(m)
            fx = np.zeros((m, 2), np.float32)
            for v in self.sfx:
                fx += v.render(m)
            # a fita acabou: a música volta
            tape_on = self.tape is not None and not self.tape.finished
            if not tape_on and self.duck.b < 1.0:
                self.duck.to(1.0, 1.5)
            mix = (mus * (self.bus["music"].block(m) * self.duck.block(m))[:, None]
                   + tap * self.bus["tape"].block(m)[:, None]
                   + fx * self.bus["sfx"].block(m)[:, None])
            mix *= self.bus["master"].block(m)[:, None]
            # limpar vozes acabadas
            self.music_out = [v for v in self.music_out if not v.finished]
            self.tapes_out = [v for v in self.tapes_out if not v.finished]
            self.sfx = [v for v in self.sfx if not v.finished]
            if self.music is not None and self.music.finished:
                self.music = None
            if self.tape is not None and self.tape.finished:
                self.tape = None
            self.frames += m
            peak = float(np.max(np.abs(mix))) if m else 0.0
            self.last_peak = max(peak, self.last_peak * 0.9)
            return np.clip(mix, -1.0, 1.0)

    def state(self) -> dict:
        with self.lock:
            def desc(v: Voice | None):
                if v is None or v.finished:
                    return None
                i = v.section_index()
                return {"cue": v.cue, "title": self.cues.get(v.cue, {}).get("title", v.cue),
                        "t": round(v.pos / SR, 1),
                        "section": None if i is None else v.sections[i].get("name"),
                        "looping": v._looping(i)}
            return {"music": desc(self.music), "tape": desc(self.tape),
                    "sfx": [v.cue for v in self.sfx if not v.finished],
                    "volume_db": dict(self.bus_db), "peak": round(self.last_peak, 3)}


# ---- saída para o Discord ----------------------------------------------------
class OpusSource:
    """AudioSource do discord.py que entrega Opus já codificado (is_opus=True)."""

    def __init__(self, mixer: Mixer, bitrate: int = 128000):
        import av
        import discord
        self._discord = discord
        self.mixer = mixer
        self.enc = av.CodecContext.create("libopus", "w")
        self.enc.sample_rate = SR
        self.enc.layout = "stereo"
        self.enc.format = "s16"
        self.enc.bit_rate = bitrate
        self.enc.options = {"application": "audio", "frame_duration": "20"}
        self.enc.open()
        self.q: list[bytes] = []
        self.pts = 0

    def is_opus(self) -> bool:
        return True

    def cleanup(self):
        pass

    def read(self) -> bytes:
        import av
        while not self.q:
            x = self.mixer.render(BLOCK)
            pcm = (x * 32767.0).astype("<i2").reshape(1, -1)
            fr = av.AudioFrame.from_ndarray(pcm, format="s16", layout="stereo")
            fr.sample_rate = SR
            fr.pts = self.pts; self.pts += BLOCK
            for p in self.enc.encode(fr):
                self.q.append(bytes(p))
        return self.q.pop(0)


def make_source_class(discord):
    class _Src(OpusSource, discord.AudioSource):
        pass
    return _Src


# ---- biblioteca no volume ----------------------------------------------------
class Library:
    """/data/som/: cues.json, files/<ficheiro>, cache/<ficheiro>.pcm"""

    def __init__(self, root: Path):
        self.root = root
        (root / "files").mkdir(parents=True, exist_ok=True)
        (root / "cache").mkdir(parents=True, exist_ok=True)

    def load_cues(self) -> dict:
        p = self.root / "cues.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def save_cues(self, cues: dict):
        p = self.root / "cues.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cues, ensure_ascii=False, indent=1))
        os.replace(tmp, p)

    def clip_for(self, cues: dict, cue_id: str) -> Clip:
        f = cues[cue_id]["file"]
        src = self.root / "files" / f
        pcm = self.root / "cache" / (f + ".pcm")
        if not pcm.exists() or pcm.stat().st_mtime < src.stat().st_mtime:
            t = time.time()
            decode_to_pcm(src, pcm)
            log.info("som: descodificado %s em %.1f s", f, time.time() - t)
        return Clip(pcm)


# ---- o Ajudante: canal de voz + HTTP ------------------------------------------
class Station:
    """Liga o Mixer ao bot: entra no canal de voz, mantém-se lá, e responde aos botões do telemóvel.

    Rotas (todas menos a página pedem `Authorization: Bearer <SOM_TOKEN>`):
      GET  /som              a mesa de botões (página para o ecrã principal do iPhone)
      GET  /som/state        o que está a tocar, volumes, canal, deixas
      POST /som/cmd          {"action": play|stop|break|next|fade_all|silence|volume|nudge|join|leave, ...}
      PUT  /som/files/{nome} carregar um ficheiro de som (corpo cru)
      PUT  /som/cues         substituir a lista de deixas (JSON)
      GET  /som/visita       configuração e estado da visita agendada (visita.py)
      PUT  /som/visita       validar e substituir a configuração da visita
      POST /som/visita/agora {"minutes": n}: visita de teste já, por n minutos (1-30)
    """

    def __init__(self, bot, root: Path, token: str | None):
        import discord
        self.discord = discord
        self.bot = bot
        self.token = token or ""
        self.lib = Library(root)
        cues = self.lib.load_cues()
        self.mixer = Mixer(cues, clip_loader=lambda cid: self.lib.clip_for(self.mixer.cues, cid))
        self.Source = make_source_class(discord)
        self.cfg_path = root / "config.json"
        self.cfg = json.loads(self.cfg_path.read_text()) if self.cfg_path.exists() else {}
        for bus, v in self.cfg.get("volume_db", {}).items():
            if bus in self.mixer.bus:
                self.mixer.set_volume(bus, v, 0)
        self.loading = False
        self.last_error = None
        self.html = (Path(__file__).parent / "som_deck.html").read_text(encoding="utf-8")
        from visita import Visita
        self.visita = Visita(self)

    # -- configuração persistente (canal escolhido, volumes)
    def _save_cfg(self):
        tmp = self.cfg_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, ensure_ascii=False))
        os.replace(tmp, self.cfg_path)

    async def preload(self):
        import asyncio
        self.loading = True
        try:
            bad = await asyncio.get_running_loop().run_in_executor(None, self.mixer.preload)
            if bad:
                self.last_error = "falharam: " + ", ".join(bad)
        finally:
            self.loading = False

    # -- voz
    def _vc(self):
        ch = self.bot.get_channel(int(self.cfg["channel"])) if self.cfg.get("channel") else None
        return ch.guild.voice_client if ch is not None else None

    async def join(self, channel_id: int):
        ch = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        vc = ch.guild.voice_client
        if vc is not None and vc.channel and vc.channel.id != ch.id:
            await vc.move_to(ch)
        elif vc is None or not vc.is_connected():
            vc = await ch.connect(self_deaf=True, timeout=20, reconnect=True)
        self.cfg["channel"] = str(ch.id); self._save_cfg()
        self._ensure_playing(vc)
        return ch

    def _ensure_playing(self, vc):
        if vc is not None and vc.is_connected() and not vc.is_playing():
            vc.play(self.Source(self.mixer), after=lambda e: e and log.error("som: o leitor parou: %s", e))
            log.info("som: a tocar no canal %s", vc.channel)

    def busy(self) -> str | None:
        """Porque é que o som está em uso (já num canal), ou None se está livre."""
        if self.cfg.get("channel"):
            return f"já está no canal {self.cfg['channel']}"
        for vc in getattr(self.bot, "voice_clients", None) or []:
            if vc.is_connected():
                return f"ligado a {vc.channel}"
        return None

    async def play_cue(self, cue_id: str):
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(None, self.mixer.play, cue_id, None)

    async def leave(self):
        vc = self._vc()
        self.cfg.pop("channel", None); self._save_cfg()
        if vc is not None:
            await vc.disconnect(force=True)

    async def watchdog(self):
        """De 3 em 3 s: se devia estar num canal e não está (queda de rede, restart), volta a entrar."""
        import asyncio
        while True:
            await asyncio.sleep(3)
            try:
                if not self.cfg.get("channel"):
                    continue
                vc = self._vc()
                if vc is None or not vc.is_connected():
                    await self.join(int(self.cfg["channel"]))
                else:
                    self._ensure_playing(vc)
            except Exception as e:
                self.last_error = f"voz: {type(e).__name__}: {e}"
                log.exception("som: watchdog")

    # -- HTTP
    def _authorized(self, request) -> bool:
        import hmac
        h = request.headers.get("Authorization", "")
        given = h[7:].strip() if h.lower().startswith("bearer ") else ""
        return bool(self.token) and bool(given) and hmac.compare_digest(given, self.token)

    def routes(self):
        from aiohttp import web

        async def page(request):
            return web.Response(text=self.html, content_type="text/html", headers={"Cache-Control": "no-store"})

        async def state(request):
            if not self._authorized(request):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
            vc = self._vc()
            s = self.mixer.state()
            s.update({
                "ok": True,
                "voice": {"channel": self.cfg.get("channel"), "connected": bool(vc and vc.is_connected()),
                          "name": vc.channel.name if vc and vc.channel else None,
                          "playing": bool(vc and vc.is_playing())},
                "loading": self.loading, "error": self.last_error,
                "cues": [{"id": k, "title": v.get("title", k), "kind": v.get("kind", "sfx"),
                          "group": v.get("group", ""), "hint": v.get("hint", "")}
                         for k, v in self.mixer.cues.items()],
                "default_channel": self.mixer.cues and self.cfg.get("default_channel"),
            })
            return web.json_response(s)

        async def cmd(request):
            if not self._authorized(request):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
            try:
                d = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
            a = d.get("action")
            fade = d.get("fade")
            fade = None if fade is None else max(0.0, min(30.0, float(fade)))
            try:
                if a == "play":
                    r = await __import__("asyncio").get_running_loop().run_in_executor(None, self.mixer.play, str(d["cue"]), fade)
                elif a == "stop":
                    r = self.mixer.stop(str(d.get("cue") or d.get("target") or "music"), fade)
                elif a == "break":
                    r = self.mixer.break_loop()
                elif a == "next":
                    r = self.mixer.next_section(fade if fade is not None else 1.0)
                elif a == "fade_all":
                    r = self.mixer.fade_all(fade if fade is not None else 3.0)
                elif a == "silence":
                    r = self.mixer.silence()
                elif a in ("volume", "nudge"):
                    bus = str(d.get("bus", "master"))
                    r = self.mixer.set_volume(bus, float(d["db"])) if a == "volume" else self.mixer.nudge(bus, float(d.get("step", 2)))
                    self.cfg["volume_db"] = dict(self.mixer.bus_db); self._save_cfg()
                elif a == "join":
                    ch = await self.join(int(d.get("channel") or self.cfg.get("channel") or self.cfg.get("default_channel")))
                    r = {"joined": ch.name}
                elif a == "leave":
                    await self.leave(); r = {"left": True}
                else:
                    return web.json_response({"ok": False, "error": "unknown_action"}, status=400)
            except KeyError as e:
                return web.json_response({"ok": False, "error": "unknown", "detail": str(e)}, status=404)
            except Exception as e:
                log.exception("som: comando %s", a)
                return web.json_response({"ok": False, "error": type(e).__name__, "detail": str(e)[:200]}, status=500)
            return web.json_response({"ok": True, **r})

        async def put_file(request):
            if not self._authorized(request):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
            name = request.match_info["name"]
            if not name or "/" in name or name.startswith(".") or len(name) > 120:
                return web.json_response({"ok": False, "error": "bad_name"}, status=400)
            dst = self.lib.root / "files" / name
            tmp = dst.with_suffix(dst.suffix + ".part")
            n = 0
            with open(tmp, "wb") as f:
                async for chunk in request.content.iter_chunked(1 << 16):
                    n += len(chunk)
                    if n > 400 * 1024 * 1024:
                        tmp.unlink(missing_ok=True)
                        return web.json_response({"ok": False, "error": "too_large"}, status=413)
                    f.write(chunk)
            os.replace(tmp, dst)
            for k in [k for k in self.mixer.clips if k == name]:
                self.mixer.clips.pop(k, None)
            return web.json_response({"ok": True, "name": name, "bytes": n})

        async def put_cues(request):
            if not self._authorized(request):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
            d = await request.json()
            cues = d.get("cues", d)
            if "default_channel" in d:
                self.cfg["default_channel"] = str(d["default_channel"]); self._save_cfg()
            missing = [c["file"] for c in cues.values() if not (self.lib.root / "files" / c["file"]).exists()]
            if missing:
                return web.json_response({"ok": False, "error": "missing_files", "files": missing}, status=400)
            self.lib.save_cues(cues)
            with self.mixer.lock:
                self.mixer.cues = cues
            __import__("asyncio").get_running_loop().create_task(self.preload())
            return web.json_response({"ok": True, "cues": len(cues)})

        async def visita_get(request):
            if not self._authorized(request):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
            return web.json_response({"ok": True, **self.visita.describe()})

        async def visita_put(request):
            if not self._authorized(request):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
            try:
                d = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
            errs = self.visita.set_config(d)
            if errs:
                return web.json_response({"ok": False, "error": "invalid", "detail": errs}, status=400)
            return web.json_response({"ok": True, **self.visita.describe()})

        async def visita_now(request):
            if not self._authorized(request):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
            try:
                d = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
            r = await self.visita.start_now(d.get("minutes") if isinstance(d, dict) else None)
            if r:
                return web.json_response({"ok": False, "error": r[1], "detail": r[2]}, status=r[0])
            return web.json_response({"ok": True, **self.visita.describe()})

        return [
            web.get("/som/visita", visita_get),
            web.put("/som/visita", visita_put),
            web.post("/som/visita/agora", visita_now),
            web.get("/som", page),
            web.get("/som/state", state),
            web.post("/som/cmd", cmd),
            web.put("/som/files/{name}", put_file),
            web.put("/som/cues", put_cues),
        ]


def setup(bot) -> Station | None:
    """Chamado pelo bot.py no arranque. Sem SOM_TOKEN/WEBHOOK_*, o som fica desligado."""
    token = os.getenv("SOM_TOKEN") or os.getenv("WEBHOOK_TOKEN") or os.getenv("WEBHOOK_SECRET")
    base = Path(os.getenv("DATA_DIR", "/data"))
    root = base / "som"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        root = Path(__file__).parent / "som_data"
        root.mkdir(parents=True, exist_ok=True)
    try:
        st = Station(bot, root, token)
    except Exception:
        log.exception("som: não arrancou")
        return None
    log.info("som: %d deixas em %s", len(st.mixer.cues), root)
    return st
