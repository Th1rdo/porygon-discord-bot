"""O nome que os utilizadores veem é "Ajudante": nenhum texto do bot diz "Porygon".

Percorre todas as strings de bot.py (descrições de comandos e opções, respostas,
ajuda, docstrings). Os identificadores, o README e o nome do repositório ficam.
Sem Discord, sem rede: `python3 tests/test_nome.py`.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ["bot.py", "consulta.py", "som.py", "visita.py"]


def strings(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def test_no_porygon_in_user_visible_strings():
    hits = [
        f"{name}:{line}: {value[:60]!r}"
        for name in SOURCES
        for line, value in strings(ROOT / name)
        if "porygon" in value.lower()
    ]
    assert not hits, "\n".join(hits)


def test_ajudante_named_in_config_and_manual():
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert 'description="Ligar/desligar funções do Ajudante neste servidor (só admins)"' in src
    assert 'description="Receber o manual do Ajudante por DM"' in src


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); n += 1; print("ok", name)
    print(n, "testes passaram")
