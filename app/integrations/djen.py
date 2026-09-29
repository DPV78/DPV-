"""Diário de Justiça Eletrônico Nacional (DJEN) — API pública de Comunicações Processuais (Comunica PJe).

Endpoint: GET {base}/comunicacao  (base padrão: https://comunicaapi.pje.jus.br/api/v1)
Filtros usados aqui: numeroProcesso, numeroOab + ufOab, dataDisponibilizacaoInicio/Fim, pagina, itensPorPagina.
Documentação (Swagger) do CNJ: https://app.swaggerhub.com/apis-docs/cnj/pcp/1.0.0

ATENÇÃO: os nomes exatos dos campos da resposta e o formato de data aceito devem ser confirmados
na documentação vigente. O parser abaixo aceita as variações conhecidas de nomes de campo, e o
formato de data é configurável (DJEN_DATE_FORMAT=iso|br).
"""
import html
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from ..cnj import digits
from ..config import settings
from .datajud import parse_datetime


class DjenError(Exception):
    pass


@dataclass
class DjenItem:
    external_id: str
    numero_processo: str  # só dígitos
    data: datetime
    titulo: str
    texto: str | None
    link: str | None


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def html_to_text(value: str | None) -> str | None:
    if not value:
        return value
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", value)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_items(payload: dict) -> list[DjenItem]:
    items = payload.get("items") or payload.get("content") or []
    out = []
    for it in items:
        numero = digits(str(_first(it, "numero_processo", "numeroProcesso", "numeroprocessocommascara") or ""))
        dt = parse_datetime(str(_first(it, "data_disponibilizacao", "datadisponibilizacao", "dataDisponibilizacao") or ""))
        if not numero or not dt:
            continue
        tipo = _first(it, "tipoComunicacao", "tipo_comunicacao") or "Comunicação"
        tribunal = _first(it, "siglaTribunal", "sigla_tribunal") or ""
        orgao = _first(it, "nomeOrgao", "nome_orgao") or ""
        tipo_doc = _first(it, "tipoDocumento", "tipo_documento") or ""
        ext = str(_first(it, "id", "hash", "numeroComunicacao") or f"{numero}|{dt.isoformat()}|{tipo}")
        titulo = " · ".join(p for p in (f"DJEN {tribunal}".strip(), str(tipo), str(tipo_doc), str(orgao)) if p)
        out.append(
            DjenItem(
                external_id=ext,
                numero_processo=numero,
                data=dt,
                titulo=titulo,
                texto=html_to_text(_first(it, "texto", "conteudo")),
                link=_first(it, "link", "url"),
            )
        )
    return out


def _fmt(d: date) -> str:
    return d.strftime("%d/%m/%Y") if settings.djen_date_format == "br" else d.isoformat()


def fetch(
    *,
    numero_processo: str | None = None,
    oab: str | None = None,
    inicio: date | None = None,
    fim: date | None = None,
    max_pages: int = 20,
    client: httpx.Client | None = None,
) -> list[DjenItem]:
    params: dict[str, str | int] = {"itensPorPagina": 100}
    if numero_processo:
        params["numeroProcesso"] = digits(numero_processo)
    if oab:
        numero, _, uf = oab.partition("/")
        params["numeroOab"] = numero.strip()
        params["ufOab"] = uf.strip().upper()
    if inicio:
        params["dataDisponibilizacaoInicio"] = _fmt(inicio)
    if fim:
        params["dataDisponibilizacaoFim"] = _fmt(fim)

    own = client is None
    client = client or httpx.Client(timeout=30)
    results: list[DjenItem] = []
    try:
        for page in range(1, max_pages + 1):
            params["pagina"] = page
            r = client.get(f"{settings.djen_base_url}/comunicacao", params=params)
            if r.status_code == 429:
                time.sleep(60)  # a API limita a taxa de requisições
                r = client.get(f"{settings.djen_base_url}/comunicacao", params=params)
            if r.status_code != 200:
                raise DjenError(f"DJEN HTTP {r.status_code}: {r.text[:200]}")
            payload = r.json()
            page_items = parse_items(payload)
            results.extend(page_items)
            raw = payload.get("items") or payload.get("content") or []
            if len(raw) < 100:
                break
        return results
    except httpx.HTTPError as e:
        raise DjenError(f"Falha de rede no DJEN: {e}") from e
    finally:
        if own:
            client.close()
