"""Comando /consulta reencaminhado: o bot passa o pedido a um serviço HTTP e mostra a resposta só a quem pediu.

Configuração (env): `CONSULTA_URL` (o endpoint, https) e `CONSULTA_TOKEN` (segredo partilhado, vai em
`Authorization: Bearer …`). Sem as duas o comando nem é registado.

Pedido:   POST CONSULTA_URL  {"user_id", "display_name", "guild_id", "channel_id", "pedido"}  (timeout 8 s)
Resposta: 200 {"content": str (1–2000 chars), "link"?: {"label": str (≤80), "url": "https://<o host de CONSULTA_URL>/…"}}
O link aparece como botão. Qualquer outra coisa (erro de rede, status ≠ 200, JSON inválido) é um erro: quem
pediu vê uma mensagem neutra e o motivo vai para o log.

O núcleo (`config_from_env`, `build_payload`, `parse_reply`, `forward`) não depende do discord.py.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

log = logging.getLogger("consulta")

TIMEOUT = 8.0          # s
CONTENT_MAX = 2000     # limite do Discord para uma mensagem
LABEL_MAX = 80         # limite do Discord para o texto de um botão
URL_MAX = 512          # limite do Discord para o URL de um botão
PEDIDO_MAX = 100       # chars aceites na opção do comando
RATE = (5, 60.0)       # 5 consultas / 60 s por utilizador

TEXT = {
    "name": "consulta",
    "description": "Consultar um pedido",
    "option": "O número ou referência do pedido",
    "unavailable": "Serviço indisponível. Tente mais tarde.",
    "rate_limited": "Demasiadas consultas seguidas. Tente daqui a um minuto.",
}


class ConsultaError(Exception):
    """A resposta do serviço não serve; a mensagem vai só para o log."""


def config_from_env(env=os.environ) -> tuple[str, str] | None:
    """(url, token) se ambas estiverem definidas, senão None (e um aviso se só uma estiver)."""
    url = (env.get("CONSULTA_URL") or "").strip()
    token = (env.get("CONSULTA_TOKEN") or "").strip()
    if url and token:
        if not url.startswith("https://") or not urlsplit(url).hostname:
            log.warning("consulta: CONSULTA_URL tem de ser https://; comando desligado")
            return None
        return url, token
    if url or token:
        log.warning("consulta: falta CONSULTA_URL ou CONSULTA_TOKEN; comando desligado")
    return None


def build_payload(user_id, display_name, guild_id, channel_id, pedido) -> dict:
    return {
        "user_id": str(user_id),
        "display_name": str(display_name or "")[:100],
        "guild_id": str(guild_id) if guild_id is not None else None,
        "channel_id": str(channel_id) if channel_id is not None else None,
        "pedido": str(pedido or "").strip()[:PEDIDO_MAX],
    }


def parse_reply(data, host: str | None = None) -> tuple[str, dict | None]:
    """Valida o JSON devolvido. Devolve (content, link|None) ou levanta ConsultaError.
    O link tem de ser https:// e, com `host`, desse host (o do próprio serviço)."""
    if not isinstance(data, dict):
        raise ConsultaError("resposta não é um objeto JSON")
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ConsultaError("content em falta ou vazio")
    if len(content) > CONTENT_MAX:
        raise ConsultaError(f"content com {len(content)} chars (máx. {CONTENT_MAX})")
    link = data.get("link")
    if link is None:
        return content, None
    if not isinstance(link, dict):
        raise ConsultaError("link não é um objeto")
    label, url = link.get("label"), link.get("url")
    if not isinstance(label, str) or not label.strip() or len(label) > LABEL_MAX:
        raise ConsultaError("link.label inválido")
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > URL_MAX:
        raise ConsultaError("link.url inválido")
    if host is not None and (urlsplit(url).hostname or "").lower() != host.lower():
        raise ConsultaError("link.url fora do host do serviço")
    return content, {"label": label, "url": url}


async def forward(url: str, token: str, payload: dict, timeout: float = TIMEOUT, session=None):
    """POST ao serviço; devolve (content, link|None). Levanta ConsultaError (ou erros de rede/timeout)."""
    import aiohttp

    own = session is None
    if own:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
    try:
        async with session.post(url, json=payload, allow_redirects=False,
                                headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status != 200:
                raise ConsultaError(f"o serviço respondeu {r.status}")
            try:
                data = await r.json(content_type=None)
            except ValueError as e:
                raise ConsultaError("resposta não é JSON") from e
        return parse_reply(data, host=urlsplit(url).hostname)
    finally:
        if own:
            await session.close()
