"""Testes do motor de som (som.py). Sem Discord, sem rede: `python tests/test_som.py`."""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import som  # noqa: E402

SR = som.SR


def tone(seconds, hz=220.0, amp=0.5):
    t = np.arange(int(seconds * SR)) / SR
    x = (amp * np.sin(2 * math.pi * hz * t) * 32767).astype(np.int16)
    return np.stack([x, x], 1)


def mixer(cues, clips):
    m = som.Mixer(cues, clip_loader=lambda cid: som.Clip(data=clips[cues[cid]["file"]]))
    m.set_volume("master", 0.0, 0)
    return m


def run(m, seconds):
    n = int(seconds * SR) // som.BLOCK
    return np.concatenate([m.render() for _ in range(n)])[:, 0]


def rms(x, a, b):
    s = x[int(a * SR):int(b * SR)]
    return float(np.sqrt(np.mean(s * s)))


def test_loop_holds_and_seam_has_no_jump():
    # 2 s de intro (220 Hz), loop de 3 s (330 Hz) com costura de 0.4 s, 1 s de fim (440 Hz)
    data = np.concatenate([tone(2, 220), tone(3, 331.7), tone(1, 440)])  # 331.7 Hz: o loop não fecha em ciclos inteiros
    cues = {"s": {"kind": "music", "file": "s", "fade_in": 0, "sections": [
        {"name": "Intro", "start": 0, "end": 2, "loop": False},
        {"name": "Loop", "start": 2, "end": 5, "loop": True, "seam": 0.4},
        {"name": "End", "start": 5, "end": 6, "loop": False}]}}
    m = mixer(cues, {"s": data})
    m.play("s", fade=0)
    x = run(m, 20)
    # passados 20 s ainda está no loop (não chegou ao fim de 440 Hz) e soa sempre
    assert m.state()["music"]["section"] == "Loop", m.state()
    # nunca cai em silêncio: no pior caso (fases opostas) a costura de igual potência perde ~7 dB
    assert min(rms(x, a, a + 0.1) for a in np.arange(0.1, 19.8, 0.1)) > 0.1
    # sem saltos: a maior diferença entre amostras vizinhas fica perto da de um seno de 330 Hz
    d = np.max(np.abs(np.diff(x[int(2.1 * SR):])))
    assert d < 0.5 * 2 * math.pi * 331.7 / SR * 1.6, d


def test_break_plays_on_to_the_end_and_stops():
    data = np.concatenate([tone(1, 220), tone(2, 330), tone(1, 440)])
    cues = {"s": {"kind": "music", "file": "s", "sections": [
        {"name": "Intro", "start": 0, "end": 1, "loop": False},
        {"name": "Loop", "start": 1, "end": 3, "loop": True, "seam": 0.2},
        {"name": "End", "start": 3, "end": 4, "loop": False}]}}
    m = mixer(cues, {"s": data})
    m.play("s", fade=0)
    run(m, 5)
    assert m.break_loop()["section"] == "Loop"
    x = run(m, 4)
    assert m.state()["music"] is None
    assert rms(x, 3.5, 4.0) < 1e-4


def test_fade_in_is_gradual():
    cues = {"s": {"kind": "music", "file": "s"}}
    m = mixer(cues, {"s": tone(10)})
    m.play("s", fade=2.0)
    x = run(m, 3)
    levels = [rms(x, a, a + 0.1) for a in (0.0, 0.5, 1.0, 1.5, 2.5)]
    assert levels == sorted(levels), levels
    assert levels[0] < 0.1 and abs(levels[-1] - 0.3536) < 0.01, levels


def test_trilha_crossfade_timing():
    cues = {"a": {"kind": "music", "file": "a"}, "b": {"kind": "music", "file": "b"}}
    m = mixer(cues, {"a": tone(30, 220), "b": tone(30, 660)})
    m.play("a", fade=0)
    run(m, 1)
    r = m.play("b", fade=4.0)
    assert r["starts_in"] == 3.0
    x = run(m, 8)
    spec = lambda a, b: np.abs(np.fft.rfft(x[int(a * SR):int(b * SR)]))
    f = np.fft.rfftfreq(int(0.5 * SR), 1 / SR)
    lo, hi = np.argmin(abs(f - 220)), np.argmin(abs(f - 660))
    s1 = spec(1.0, 1.5)  # 1 s depois: a velha ainda forte, a nova em silêncio
    assert s1[lo] > 10 * s1[hi]
    s2 = spec(7.0, 7.5)  # 7 s depois: só a nova
    assert s2[hi] > 10 * s2[lo]


def test_tape_ducks_music_and_releases():
    cues = {"m": {"kind": "music", "file": "m"}, "t": {"kind": "tape", "file": "t"}}
    m = mixer(cues, {"m": tone(60, 220), "t": tone(2, 880)})
    m.play("m", fade=0)
    run(m, 1)
    base = rms(run(m, 0.5), 0, 0.5)
    m.play("t")
    x = run(m, 6)
    f = np.fft.rfftfreq(int(0.5 * SR), 1 / SR)
    lo = np.argmin(abs(f - 220))
    a = np.abs(np.fft.rfft(x[int(1.0 * SR):int(1.5 * SR)]))[lo]
    b = np.abs(np.fft.rfft(x[int(5.0 * SR):int(5.5 * SR)]))[lo]
    assert 20 * math.log10(a / b) < -7, (a, b)  # baixada ~9 dB debaixo da fita
    assert abs(rms(x, 5.0, 5.5) - base) < 0.01  # e volta depois


def test_stop_fades_and_sfx_overlap():
    cues = {"m": {"kind": "music", "file": "m"}, "x": {"kind": "sfx", "file": "x"}}
    m = mixer(cues, {"m": tone(60), "x": tone(0.5, 1000)})
    m.play("m", fade=0); m.play("x"); m.play("x")
    assert len(m.state()["sfx"]) == 2
    m.stop("music", fade=1.0)
    x = run(m, 2)
    assert rms(x, 0.6, 0.7) > rms(x, 0.9, 1.0) > 0
    assert m.state()["music"] is None and rms(x, 1.5, 2.0) == 0


def test_next_jumps_to_following_section():
    data = np.concatenate([tone(2, 220), tone(2, 330), tone(2, 440)])
    cues = {"s": {"kind": "music", "file": "s", "sections": [
        {"name": "Intro", "start": 0, "end": 2, "loop": False},
        {"name": "Loop", "start": 2, "end": 4, "loop": True, "seam": 0.2},
        {"name": "End", "start": 4, "end": 6, "loop": False}]}}
    m = mixer(cues, {"s": data})
    m.play("s", fade=0)
    run(m, 3)
    assert m.next_section()["next"] == "End"
    run(m, 1.5)
    assert m.state()["music"]["section"] == "End"


def test_drop_and_reset_only_touch_voices():
    cues = {"m": {"kind": "music", "file": "m"}, "x": {"kind": "sfx", "file": "x", "loop": True}}
    m = mixer(cues, {"m": tone(10), "x": tone(1, 1000)})
    m.set_volume("music", -6, 0)
    m.play("m", fade=0); m.play("x")
    assert m.active_cues() == {"m", "x"}
    m.drop("x")
    assert m.active_cues() == {"m"} and m.state()["music"]["cue"] == "m"
    m.reset()
    assert m.active_cues() == set() and m.bus_db["music"] == -6
    assert rms(run(m, 0.5), 0, 0.5) == 0


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); n += 1; print("ok", name)
    print(n, "testes passaram")
