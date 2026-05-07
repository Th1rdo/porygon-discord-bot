"""
Build de ficheiros media para !mission do Porygon.
====================================================

Gera/copia todos os artefactos das quests legendárias para a pasta `media/`,
com nomes que o bot consegue descobrir automaticamente:

    media/mission003.wav  → Meloetta (8 notas)
    media/mission004.png  → Hoopa (anéis com glifos)
    media/mission005.png  → Mew (poema acróstico em pergaminho)
    media/mission006.wav  → Zeraora (estática com texto em espectrograma)
    media/mission007.png  → Marshadow (diário de Kova rendido como imagem)

Usa os ficheiros-fonte que vivem na vault Obsidian em
`Pokémon King/Worldbuilding/Quests/Side Quests/ARG/`.

Como executar:
    pip install pillow numpy scipy
    python build_missions.py

Depois faz commit + push para Railway re-deployar.
"""

from __future__ import annotations
import os
import re
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.io import wavfile


# --------------------------- caminhos ---------------------------------------

PORYGON_DIR = Path(__file__).parent
MEDIA_DIR = PORYGON_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)

VAULT_DIR = Path(
    "/Users/tiago/Library/Mobile Documents/iCloud~md~obsidian/Documents/Pokémon King"
)
ARG_DIR = VAULT_DIR / "Worldbuilding" / "Quests" / "Side Quests" / "ARG"


# --------------------------- helpers ----------------------------------------

def _find_font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    """Tenta encontrar uma fonte system; cai em default se nada disponível."""
    candidates_serif = [
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/Library/Fonts/Times New Roman.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ]
    candidates_mono = [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Courier New.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    candidates = candidates_mono if mono else candidates_serif
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _aged_paper(width: int, height: int, bg=(245, 235, 215)) -> Image.Image:
    """Cria uma textura de papel envelhecido."""
    img = Image.new("RGB", (width, height), bg)
    arr = np.array(img, dtype=np.float32)

    rng = np.random.default_rng(seed=42)
    noise = rng.normal(0, 4, arr.shape)
    arr += noise

    # vignette
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    cx, cy = width / 2.0, height / 2.0
    dist = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
    vignette = np.clip(1.0 - 0.18 * (dist - 0.4), 0.78, 1.0)
    arr *= vignette[:, :, None]

    # algumas manchas pequenas
    rng2 = np.random.default_rng(seed=7)
    for _ in range(rng2.integers(20, 40)):
        sx = rng2.integers(50, width - 50)
        sy = rng2.integers(50, height - 50)
        radius = rng2.integers(8, 25)
        intensity = rng2.uniform(0.85, 0.97)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy <= radius * radius:
                    yi, xi = sy + dy, sx + dx
                    if 0 <= yi < height and 0 <= xi < width:
                        arr[yi, xi] *= intensity

    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _journal_paper(width: int, height: int) -> Image.Image:
    """Páginas de caderno mais escuras para o diário de Kova (couro)."""
    return _aged_paper(width, height, bg=(238, 226, 200))


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Quebra texto em linhas que cabem em max_width."""
    if not text.strip():
        return [""]
    words = text.split(" ")
    lines = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        bbox = font.getbbox(test)
        w = bbox[2] - bbox[0]
        if w > max_width and current:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    return lines


# --------------------------- mission 003 — Meloetta -------------------------

def build_meloetta() -> Path:
    """8 notas: 7 de pergunta + silêncio + 1 de resposta. Sino-melancólico."""
    sr = 44100

    # Melodia em D menor: D5 F5 A5 G5 F5 E5 D5  (silêncio)  A4 (resposta)
    D5 = 587.33
    F5 = 698.46
    A5 = 880.00
    G5 = 783.99
    E5 = 659.25
    A4 = 440.00

    question_notes = [D5, F5, A5, G5, F5, E5, D5]
    answer_note = A4

    note_dur = 0.65
    gap = 0.04

    def synth(freq: float, dur: float, decay: float = 2.5, vol: float = 0.6) -> np.ndarray:
        t = np.arange(int(sr * dur)) / sr
        env = np.exp(-decay * t / dur)
        # tom + 2.º harmónico + 3.º harmónico subtil — som de carrilhão
        sig = (
            vol * np.sin(2 * np.pi * freq * t)
            + (vol * 0.35) * np.sin(2 * np.pi * 2 * freq * t)
            + (vol * 0.12) * np.sin(2 * np.pi * 3 * freq * t)
            + (vol * 0.04) * np.sin(2 * np.pi * 4 * freq * t)
        )
        return sig * env

    chunks: list[np.ndarray] = []

    # silêncio inicial
    chunks.append(np.zeros(int(sr * 0.4)))

    for f in question_notes:
        chunks.append(synth(f, note_dur))
        chunks.append(np.zeros(int(sr * gap)))

    # pausa antes da resposta
    chunks.append(np.zeros(int(sr * 0.9)))

    # nota final mais longa, mais grave, mais lenta no decay
    chunks.append(synth(answer_note, 1.8, decay=1.0, vol=0.7))

    # silêncio final
    chunks.append(np.zeros(int(sr * 0.3)))

    audio = np.concatenate(chunks).astype(np.float32)

    # adicionar reverb leve via convolução com um decaimento exponencial
    rev_len = int(sr * 0.6)
    rev_t = np.arange(rev_len) / sr
    impulse = 0.35 * np.exp(-5 * rev_t) * (np.random.default_rng(11).normal(0, 1, rev_len))
    impulse[0] = 1.0
    audio_rev = np.convolve(audio, impulse, mode="full")[: len(audio)]
    audio = 0.7 * audio + 0.3 * audio_rev

    audio = audio / np.max(np.abs(audio)) * 0.85

    out = MEDIA_DIR / "mission003.wav"
    wavfile.write(out, sr, (audio * 32767).astype(np.int16))
    return out


# --------------------------- mission 004 — Hoopa ----------------------------

def build_hoopa() -> Path:
    """Copia hoopa_rings.png; gera primeiro se não existir."""
    src = ARG_DIR / "Hoopa" / "hoopa_rings.png"
    if not src.exists():
        # gera correndo o script-fonte
        gen = ARG_DIR / "Hoopa" / "generate_rings.py"
        if gen.exists():
            cwd = os.getcwd()
            try:
                os.chdir(gen.parent)
                exec(gen.read_text(encoding="utf-8"), {"__file__": str(gen)})
            finally:
                os.chdir(cwd)
    dst = MEDIA_DIR / "mission004.png"
    shutil.copy(src, dst)
    return dst


# --------------------------- mission 005 — Mew ------------------------------

def build_mew() -> Path:
    """Renderiza o poema acróstico em pergaminho."""
    poem_path = ARG_DIR / "Mew" / "texto_escondido.md"
    raw = poem_path.read_text(encoding="utf-8")

    # extrair as linhas em itálico do corpo do poema (até ao primeiro ---)
    lines: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("---"):
            break
        if s.startswith("*") and not s.startswith("**"):
            txt = s.strip("*").strip()
            lines.append(txt)
        elif s == "":
            lines.append("")  # preserva quebras de estrofe

    # remover quebras à frente/atrás
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()

    width = 1400
    margin_x = 130
    margin_top = 130
    line_h = 58
    stanza_gap = 22
    height = margin_top * 2 + sum(line_h if l else stanza_gap for l in lines) + 180

    img = _aged_paper(width, height)
    draw = ImageDraw.Draw(img)

    font_body = _find_font(32)
    font_small = _find_font(22)
    font_italic_note = _find_font(20)

    y = margin_top - 60
    header = "[no verso, escrito em letra adulta]"
    draw.text((margin_x, y), header, fill=(150, 120, 90), font=font_italic_note)
    y += 80

    text_color = (55, 38, 22)
    for line in lines:
        if not line:
            y += stanza_gap
            continue
        draw.text((margin_x, y), line, fill=text_color, font=font_body)
        y += line_h

    y += 30
    draw.text((margin_x, y), "—  tinta antiga, sem assinatura", fill=(150, 120, 90), font=font_small)

    # JPG é muito mais leve para esta textura (papel + texto) e fica visualmente igual
    out = MEDIA_DIR / "mission005.jpg"
    img.save(out, "JPEG", quality=88, optimize=True, progressive=True)
    return out


# --------------------------- mission 006 — Zeraora --------------------------

def build_zeraora() -> Path:
    """Copia zeraora_static.wav; gera primeiro se não existir."""
    src = ARG_DIR / "Zeraora" / "zeraora_static.wav"
    if not src.exists():
        gen = ARG_DIR / "Zeraora" / "generate_static.py"
        if gen.exists():
            cwd = os.getcwd()
            try:
                os.chdir(gen.parent)
                exec(gen.read_text(encoding="utf-8"), {"__file__": str(gen)})
            finally:
                os.chdir(cwd)
    dst = MEDIA_DIR / "mission006.wav"
    shutil.copy(src, dst)
    return dst


# --------------------------- mission 007 — Marshadow ------------------------

def build_marshadow() -> Path:
    """Renderiza o diário da Kova como imagem alta de páginas de caderno."""
    diary_path = ARG_DIR / "Marshadow" / "diario_kova.md"
    raw = diary_path.read_text(encoding="utf-8")

    # parse bem simples: extrai entradas (Combate XXX, Resultado, descrição,
    # Movimento Decisivo, Margem) e blocos como cabeçalho/nota inicial/nota final.
    text = raw

    # Layout
    width = 1400
    margin_x = 110
    line_h = 38
    paragraph_gap = 18
    section_gap = 60

    font_title = _find_font(40, bold=True)
    font_subtitle = _find_font(28)
    font_body = _find_font(22)
    font_italic = _find_font(20)
    font_combat = _find_font(26, bold=True)
    font_move = _find_font(24, bold=True)
    font_margin = _find_font(20)

    text_color = (40, 28, 18)
    italic_color = (95, 75, 50)
    margin_red = (140, 35, 25)
    margin_black = (40, 28, 18)

    # Pré-processar: limpar bloco de código de header, separadores, etc.
    body = re.split(r"```", text)
    # se há um bloco de código, é o cabeçalho ASCII; descarta-se aqui (renderizamos um cabeçalho próprio)
    if len(body) >= 3:
        body_text = "```".join(body[2:])
    else:
        body_text = text

    # Parser ad-hoc: divide por ---
    sections = [s.strip() for s in body_text.split("\n---\n") if s.strip()]

    # Calcular altura total fazendo dry-run
    def render_section(draw, x, y, section: str, dry: bool = False) -> int:
        local_y = y
        # Linhas
        lines = section.splitlines()
        i = 0
        while i < len(lines):
            ln = lines[i].rstrip()

            # Cabeçalho ### Combate ...
            if ln.startswith("### Combate"):
                title = ln.replace("###", "").strip()
                if not dry:
                    draw.text((x, local_y), title, fill=text_color, font=font_combat)
                local_y += line_h + 6

            # Cabeçalho ### Nota...
            elif ln.startswith("### "):
                title = ln.replace("###", "").strip()
                if not dry:
                    draw.text((x, local_y), title, fill=text_color, font=font_subtitle)
                local_y += line_h + 6

            # Linhas em itálico (* ... *)
            elif ln.startswith("*") and ln.endswith("*") and ln.count("*") == 2:
                content = ln.strip("*").strip()
                wrapped = _wrap(content, font_italic, width - 2 * margin_x)
                for w_line in wrapped:
                    if not dry:
                        draw.text((x, local_y), w_line, fill=italic_color, font=font_italic)
                    local_y += line_h - 4

            # Movimento Decisivo
            elif "**Movimento Decisivo:**" in ln:
                _, after = ln.split("**Movimento Decisivo:**", 1)
                label = "Movimento Decisivo:"
                if not dry:
                    draw.text((x, local_y), label, fill=text_color, font=font_move)
                # após o label, o nome do movimento
                offset_x = font_move.getbbox(label + " ")[2]
                if not dry:
                    draw.text((x + offset_x, local_y), after.strip(), fill=text_color, font=font_move)
                local_y += line_h + 4

            # Margem (vermelho/preto)
            elif ln.startswith("> *Margem"):
                # apanha possível continuação de linhas até ao próximo bloco
                margin_lines = [ln]
                while i + 1 < len(lines) and (lines[i + 1].startswith(">") or lines[i + 1].startswith(" ")):
                    i += 1
                    margin_lines.append(lines[i])
                joined = " ".join(l.lstrip("> ").strip() for l in margin_lines)

                # decidir cor pelo "(vermelho)" ou "(preto)"
                color = margin_red if "vermelho" in joined.lower() else margin_black

                # remover "Margem (cor):" e os asteriscos
                joined = re.sub(r"\*Margem\s*\([^)]+\):\*", "", joined).strip()
                joined = joined.strip("\"")
                # adicionar bordas de aspas tipográficas
                content = f"—  margem: “{joined}”"

                wrapped = _wrap(content, font_margin, width - 2 * margin_x - 40)
                for w_line in wrapped:
                    if not dry:
                        draw.text((x + 30, local_y), w_line, fill=color, font=font_margin)
                    local_y += line_h - 6
                local_y += paragraph_gap

            # Linha em branco
            elif ln == "":
                local_y += paragraph_gap // 2

            # Travessão "—" no final
            elif ln.startswith("—"):
                wrapped = _wrap(ln, font_italic, width - 2 * margin_x)
                for w_line in wrapped:
                    if not dry:
                        draw.text((x, local_y), w_line, fill=italic_color, font=font_italic)
                    local_y += line_h - 4

            # Texto normal
            else:
                # remover negrito/itálico markdown simples
                clean = re.sub(r"\*\*(.+?)\*\*", r"\1", ln)
                clean = re.sub(r"\*(.+?)\*", r"\1", clean)
                wrapped = _wrap(clean, font_body, width - 2 * margin_x)
                for w_line in wrapped:
                    if not dry:
                        draw.text((x, local_y), w_line, fill=text_color, font=font_body)
                    local_y += line_h

            i += 1

        return local_y

    # 1.ª passagem: medir altura
    dummy = Image.new("RGB", (width, 100))
    dummy_draw = ImageDraw.Draw(dummy)
    y_test = 200  # margem topo + cabeçalho
    for sec in sections:
        y_test = render_section(dummy_draw, margin_x, y_test, sec, dry=True)
        y_test += section_gap
    y_test += 200

    height = y_test
    img = _journal_paper(width, height)
    draw = ImageDraw.Draw(img)

    # Cabeçalho próprio
    title = "DIÁRIO DE COMBATE — VOL. XI"
    sub = "K. — Líder do Ginásio de Pugnia"
    sub2 = "(Coliseu, registo pessoal)"
    title_y = 70
    bbox = font_title.getbbox(title)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) // 2, title_y), title, fill=text_color, font=font_title)
    bbox2 = font_subtitle.getbbox(sub)
    tw2 = bbox2[2] - bbox2[0]
    draw.text(((width - tw2) // 2, title_y + 60), sub, fill=text_color, font=font_subtitle)
    bbox3 = font_italic.getbbox(sub2)
    tw3 = bbox3[2] - bbox3[0]
    draw.text(((width - tw3) // 2, title_y + 100), sub2, fill=italic_color, font=font_italic)

    # Linha decorativa
    draw.line(((margin_x, title_y + 145), (width - margin_x, title_y + 145)), fill=italic_color, width=2)

    # Conteúdo
    y = 200
    for sec in sections:
        # saltar a secção de cabeçalho ASCII se chegou cá
        if sec.startswith("```") or "DIÁRIO DE COMBATE" in sec[:200]:
            continue
        # saltar o "*Caderno gasto, capa de couro escuro..." etc — descrição do físico
        if sec.startswith("*Caderno gasto"):
            continue
        y = render_section(draw, margin_x, y, sec, dry=False)
        y += section_gap
        # linha divisória subtil entre secções
        draw.line(((margin_x + 200, y - section_gap // 2), (width - margin_x - 200, y - section_gap // 2)), fill=(180, 160, 130), width=1)

    out = MEDIA_DIR / "mission007.jpg"
    img.save(out, "JPEG", quality=88, optimize=True, progressive=True)
    return out


# --------------------------- main -------------------------------------------

def main():
    print("Building mission media files...")
    print(f"  → {MEDIA_DIR}")
    print()

    builders = [
        ("mission003 (Meloetta)", build_meloetta),
        ("mission004 (Hoopa)",    build_hoopa),
        ("mission005 (Mew)",      build_mew),
        ("mission006 (Zeraora)",  build_zeraora),
        ("mission007 (Marshadow)", build_marshadow),
    ]

    for label, fn in builders:
        try:
            path = fn()
            size_kb = path.stat().st_size / 1024
            print(f"  ✓ {label:<26} → {path.name}  ({size_kb:.0f} KB)")
        except Exception as e:
            print(f"  ✗ {label:<26} → FALHOU: {e}")

    print()
    print("Done.")
    print()
    print("Próximo passo:")
    print("  cd ~/PycharmProjects/Porygon")
    print("  git add media/ build_missions.py")
    print("  git commit -m 'Add missions 003-007'")
    print("  git push")
    print("  → Railway redeploy automático.")


if __name__ == "__main__":
    main()
