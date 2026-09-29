"""API Pública do DataJud (CNJ): metadados e movimentos processuais.

Endpoint: {base}/api_publica_{tribunal}/_search  (consulta estilo Elasticsearch)
Autenticação: header "Authorization: APIKey <chave pública>".
A chave pública é divulgada pelo CNJ em https://datajud-wiki.cnj.jus.br/api-publica/acesso
e pode ser trocada pelo CNJ a qualquer momento — por isso fica em variável de ambiente.

Observação: o DataJud traz movimentos codificados pelas Tabelas Processuais Unificadas,
não o inteiro teor dos atos. E há defasagem entre o ato no tribunal e sua chegada à base.
"""
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from ..cnj import digits
from ..config import settings


class DataJudError(Exception):
    pass


@dataclass
class DataJudMovement:
    external_id: str
    data: datetime
    titulo: str
    texto: str | None = None


@dataclass
class DataJudResult:
    found: bool
    classe: str | None = None
    orgao_julgador: str | None = None
    movements: list[DataJudMovement] = field(default_factory=list)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1]
    try:
        return datetime.fromisoformat(v).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y%m%d%H%M%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _complementos(mov: dict) -> str | None:
    partes = []
    for c in mov.get("complementosTabelados") or []:
        nome = c.get("nome") or ""
        desc = c.get("descricao") or ""
        txt = " ".join(p for p in (desc.replace("_", " "), nome) if p).strip()
        if txt:
            partes.append(txt)
    return "; ".join(partes) or None


def parse_response(payload: dict) -> DataJudResult:
    hits = (payload.get("hits") or {}).get("hits") or []
    if not hits:
        return DataJudResult(found=False)
    movements: dict[str, DataJudMovement] = {}
    classe = orgao = None
    # Um processo pode ter mais de um registro (ex.: um por grau). Consolidamos todos.
    for hit in hits:
        src = hit.get("_source") or {}
        classe = classe or (src.get("classe") or {}).get("nome")
        orgao = orgao or (src.get("orgaoJulgador") or {}).get("nome")
        grau = src.get("grau") or ""
        for mov in src.get("movimentos") or []:
            dt = parse_datetime(mov.get("dataHora"))
            if not dt:
                continue
            codigo = str(mov.get("codigo") or "")
            nome = mov.get("nome") or f"Movimento {codigo}"
            ext = f"{grau}|{codigo}|{dt.isoformat()}"
            titulo = f"[{grau}] {nome}" if grau else nome
            movements[ext] = DataJudMovement(external_id=ext, data=dt, titulo=titulo, texto=_complementos(mov))
    return DataJudResult(
        found=True,
        classe=classe,
        orgao_julgador=orgao,
        movements=sorted(movements.values(), key=lambda m: m.data),
    )


def fetch(numero_cnj: str, tribunal: str, client: httpx.Client | None = None) -> DataJudResult:
    if not settings.datajud_api_key:
        raise DataJudError("DATAJUD_API_KEY não configurada")
    url = f"{settings.datajud_base_url}/api_publica_{tribunal.lower()}/_search"
    body = {"query": {"match": {"numeroProcesso": digits(numero_cnj)}}, "size": 10}
    headers = {"Authorization": f"APIKey {settings.datajud_api_key}", "Content-Type": "application/json"}
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        r = client.post(url, json=body, headers=headers)
        if r.status_code != 200:
            raise DataJudError(f"DataJud HTTP {r.status_code}: {r.text[:200]}")
        return parse_response(r.json())
    except httpx.HTTPError as e:
        raise DataJudError(f"Falha de rede no DataJud: {e}") from e
    finally:
        if own:
            client.close()
