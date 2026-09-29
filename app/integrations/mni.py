"""Cliente do MNI (Modelo Nacional de Interoperabilidade) — webservice SOAP do PJe para sistemas de advogados.

Autenticação: CPF e senha do PJe do advogado, enviados no corpo da mensagem (idConsultante/senhaConsultante).
Cada tribunal (e cada instância) tem seu próprio endereço; em geral algo como
    https://pje.<tribunal>.jus.br/<instancia>/intercomunicacao
mas o endereço exato deve ser obtido com o tribunal — nem todo PJe tem o MNI habilitado para advogados.

Operações usadas:
  * consultarAvisosPendentes — lista intimações/citações pendentes. NÃO registra ciência.
  * consultarProcesso        — dados do processo e movimentos (e, opcionalmente, documentos).
  * consultarTeorComunicacao — abre o teor de uma intimação. REGISTRA A CIÊNCIA NO PJe E PODE INICIAR O
                               PRAZO (Lei 11.419/2006, art. 5º). Só é chamada por ação expressa e confirmada.

Referências usadas para os nomes de elementos e namespaces: esquemas do CNJ do MNI 2.2.x e uma resposta real
do MNI 2.2.3 do TJMG publicada no projeto aberto araujodgdev/lume-os. Os namespaces seguem o padrão
http://www.cnj.jus.br/(servico|tipos-servico)-intercomunicacao-<versão>; confirme a versão de cada tribunal
no WSDL (<endereço>?wsdl).
"""
import base64
import email.parser
import email.policy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import unquote
from xml.sax.saxutils import escape

import httpx

from ..cnj import digits


class MniError(Exception):
    def __init__(self, message: str, kind: str = "resposta"):
        super().__init__(message)
        self.kind = kind  # credencial / bloqueio / endpoint / resposta


@dataclass
class Credential:
    cpf: str
    senha: str


@dataclass
class Aviso:
    id_aviso: str
    numero_processo: str  # só dígitos
    data_disponibilizacao: date | None
    tipo_comunicacao: str | None = None
    orgao: str | None = None
    destinatario: str | None = None


@dataclass
class Documento:
    id_documento: str
    descricao: str
    mimetype: str = "application/pdf"
    data: datetime | None = None
    conteudo: bytes | None = None


@dataclass
class MovimentoMni:
    data: datetime
    descricao: str
    codigo: str | None = None
    external_id: str = ""


@dataclass
class ProcessoMni:
    numero: str
    orgao: str | None = None
    classe: str | None = None
    movimentos: list[MovimentoMni] = field(default_factory=list)
    documentos: list[Documento] = field(default_factory=list)


@dataclass
class Teor:
    texto: str
    prazo_dias: int | None = None
    tipo_prazo: str | None = None
    documentos: list[Documento] = field(default_factory=list)


def namespaces(versao: str) -> tuple[str, str]:
    return (f"http://www.cnj.jus.br/servico-intercomunicacao-{versao}/",
            f"http://www.cnj.jus.br/tipos-servico-intercomunicacao-{versao}")


def envelope(operacao: str, campos: list[tuple[str, object]], versao: str) -> str:
    ns_ser, ns_tip = namespaces(versao)
    corpo = "".join(
        f"<tip:{k}>{escape(str(v).lower() if isinstance(v, bool) else str(v))}</tip:{k}>"
        for k, v in campos if v is not None
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ser="{ns_ser}" xmlns:tip="{ns_tip}">'
        f"<soapenv:Header/><soapenv:Body><ser:{operacao}>{corpo}</ser:{operacao}></soapenv:Body></soapenv:Envelope>"
    )


# ------------------------------------------------------------------ leitura da resposta

def split_mtom(body: bytes, content_type: str | None) -> tuple[bytes, dict[str, bytes]]:
    """Separa a parte XML e os anexos binários de uma resposta MTOM (multipart/related)."""
    stripped = body.lstrip()
    if not (content_type and "multipart" in content_type.lower()) and not stripped.startswith(b"--"):
        return body, {}
    if not (content_type and "boundary" in content_type.lower()):
        boundary = stripped.split(b"\n", 1)[0].strip()[2:].decode(errors="replace")
        content_type = f'multipart/related; boundary="{boundary}"'
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    xml_part, attachments = None, {}
    for part in msg.iter_parts():
        payload = part.get_payload(decode=True) or b""
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if xml_part is None and b"Envelope" in payload[:4000]:
            xml_part = payload
        elif cid:
            attachments[cid] = payload
    if xml_part is None:
        raise MniError("Resposta MTOM sem envelope SOAP.", "endpoint")
    return xml_part, attachments


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _child(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    found = _children(el, name)
    return found[0] if found else None


def _find_all(el: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in el.iter() if _local(e.tag) == name]


def _attr(el: ET.Element | None, name: str) -> str | None:
    if el is None:
        return None
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return None


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_datahora(value: str | None) -> datetime | None:
    """tipoDataHora do MNI: AAAAMMDDHHMMSS (também aceita AAAA-MM-DD…)."""
    if not value:
        return None
    d = re.sub(r"\D", "", value)
    for size, fmt in ((14, "%Y%m%d%H%M%S"), (12, "%Y%m%d%H%M"), (8, "%Y%m%d")):
        if len(d) >= size:
            try:
                return datetime.strptime(d[:size], fmt)
            except ValueError:
                continue
    return None


_CRED_RE = re.compile(r"senha|autentic|credenc|usu[áa]rio|acesso negado|login", re.I)


def read_response(body: bytes, content_type: str | None, operacao: str) -> tuple[ET.Element, dict[str, bytes]]:
    xml, attachments = split_mtom(body, content_type)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise MniError(f"Resposta do MNI não é XML válido: {e}", "endpoint") from e
    faults = _find_all(root, "Fault")
    if faults:
        msg = _text(_find_all(faults[0], "faultstring")[0]) if _find_all(faults[0], "faultstring") else "Falha SOAP."
        raise MniError(msg, "credencial" if _CRED_RE.search(msg) else "resposta")
    found = _find_all(root, f"{operacao}Resposta")
    if not found:
        raise MniError(f"Resposta do MNI sem {operacao}Resposta.", "resposta")
    resp = found[0]
    if _text(_child(resp, "sucesso")).lower() != "true":
        msg = _text(_child(resp, "mensagem")) or "O tribunal recusou a consulta."
        raise MniError(msg, "credencial" if _CRED_RE.search(msg) else "resposta")
    return resp, attachments


def _conteudo(doc_el: ET.Element, attachments: dict[str, bytes]) -> bytes | None:
    cont = _child(doc_el, "conteudo")
    if cont is None:
        return None
    for inc in cont.iter():
        if _local(inc.tag) == "Include":
            href = (_attr(inc, "href") or "").removeprefix("cid:")
            return attachments.get(href) or attachments.get(unquote(href))
    raw = re.sub(r"\s+", "", _text(cont))
    if not raw:
        return None
    try:
        return base64.b64decode(raw)
    except ValueError:
        return None


def _documentos(parent: ET.Element, attachments: dict[str, bytes]) -> list[Documento]:
    docs = []
    for d in _children(parent, "documento"):
        docs.append(Documento(
            id_documento=_attr(d, "idDocumento") or "",
            descricao=_attr(d, "descricao") or _attr(d, "tipoDocumento") or "Documento",
            mimetype=_attr(d, "mimetype") or "application/pdf",
            data=parse_datahora(_attr(d, "dataHora")),
            conteudo=_conteudo(d, attachments),
        ))
        for v in _children(d, "documentoVinculado"):
            docs.append(Documento(
                id_documento=_attr(v, "idDocumento") or "",
                descricao=_attr(v, "descricao") or "Documento vinculado",
                mimetype=_attr(v, "mimetype") or "application/pdf",
                data=parse_datahora(_attr(v, "dataHora")),
                conteudo=_conteudo(v, attachments),
            ))
    return docs


def parse_avisos(resp: ET.Element) -> list[Aviso]:
    out = []
    for av in _children(resp, "aviso"):
        proc = _child(av, "processo")
        orgao = _child(proc, "orgaoJulgador")
        dest = _child(av, "destinatario")
        pessoa = next((e for e in dest.iter() if _local(e.tag) == "pessoa"), None) if dest is not None else None
        disp = parse_datahora(_text(_child(av, "dataDisponibilizacao")))
        aviso = Aviso(
            id_aviso=_attr(av, "idAviso") or "",
            numero_processo=digits(_attr(proc, "numero") or ""),
            data_disponibilizacao=disp.date() if disp else None,
            tipo_comunicacao=_attr(av, "tipoComunicacao"),
            orgao=_attr(orgao, "nomeOrgao"),
            destinatario=_attr(pessoa, "nome"),
        )
        if aviso.id_aviso and aviso.numero_processo:
            out.append(aviso)
    return out


def parse_processo(resp: ET.Element, attachments: dict[str, bytes]) -> ProcessoMni:
    proc = _child(resp, "processo")
    dados = _child(proc, "dadosBasicos")
    if proc is None or dados is None:
        raise MniError("Processo não encontrado ou sem acesso para esta credencial.", "resposta")
    movimentos = []
    for m in _children(proc, "movimento"):
        dt = parse_datahora(_attr(m, "dataHora"))
        if not dt:
            continue
        nacional = _child(m, "movimentoNacional")
        local = _child(m, "movimentoLocal")
        compl = [_text(c) for c in _children(m, "complemento")]
        if nacional is not None:
            compl += [_text(c) for c in _children(nacional, "complemento")]
        compl = [c for c in compl if c]
        codigo = _attr(nacional, "codigoNacional") or _attr(local, "codigoMovimento")
        descricao = _attr(local, "descricao") or " · ".join(compl) or (f"Movimento {codigo}" if codigo else "Movimentação")
        ident = _attr(m, "identificadorMovimento") or f"{dt.strftime('%Y%m%d%H%M%S')}|{codigo or ''}|{descricao[:80]}"
        movimentos.append(MovimentoMni(data=dt, descricao=descricao, codigo=codigo, external_id=ident))
    return ProcessoMni(
        numero=digits(_attr(dados, "numero") or ""),
        orgao=_attr(_child(dados, "orgaoJulgador"), "nomeOrgao"),
        classe=_attr(dados, "classeProcessual"),
        movimentos=sorted(movimentos, key=lambda x: x.data),
        documentos=_documentos(proc, attachments),
    )


def parse_teor(resp: ET.Element, attachments: dict[str, bytes]) -> Teor:
    com = _child(resp, "comunicacao")
    if com is None:
        raise MniError("O tribunal não devolveu o teor da comunicação.", "resposta")
    prazo = _attr(com, "prazo")
    return Teor(
        texto=_text(_child(com, "teor")),
        prazo_dias=int(prazo) if prazo and prazo.isdigit() and int(prazo) > 0 else None,
        tipo_prazo=_attr(com, "tipoPrazo"),
        documentos=_documentos(com, attachments),
    )


# ------------------------------------------------------------------ chamadas

class MniClient:
    def __init__(self, url: str, versao: str = "2.2.2", client: httpx.Client | None = None, timeout: float = 60):
        self.url = url
        self.versao = versao
        self._client = client
        self._timeout = timeout

    def _call(self, operacao: str, campos: list[tuple[str, object]]) -> tuple[ET.Element, dict[str, bytes]]:
        ns_ser, _ = namespaces(self.versao)
        body = envelope(operacao, campos, self.versao)
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{ns_ser}{operacao}"'}
        own = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            r = client.post(self.url, content=body.encode(), headers=headers)
        except httpx.HTTPError as e:
            raise MniError(f"Sem conexão com o MNI: {e}", "endpoint") from e
        finally:
            if own:
                client.close()
        if r.status_code in (401, 403, 429):
            raise MniError(f"O tribunal bloqueou o acesso (HTTP {r.status_code}).", "bloqueio")
        if b"Envelope" not in r.content[:200000]:
            raise MniError(f"O endereço do MNI respondeu HTTP {r.status_code} sem SOAP.", "endpoint")
        return read_response(r.content, r.headers.get("content-type"), operacao)

    def consultar_avisos_pendentes(self, cred: Credential, data_referencia: datetime | None = None) -> list[Aviso]:
        """Lista intimações pendentes. Não registra ciência."""
        campos = [("idConsultante", digits(cred.cpf)), ("senhaConsultante", cred.senha)]
        if data_referencia:
            campos.append(("dataReferencia", data_referencia.strftime("%Y%m%d%H%M%S")))
        resp, _ = self._call("consultarAvisosPendentes", campos)
        return parse_avisos(resp)

    def consultar_processo(self, cred: Credential, numero: str, *, incluir_documentos: bool = False,
                           documentos: list[str] | None = None) -> ProcessoMni:
        campos: list[tuple[str, object]] = [
            ("idConsultante", digits(cred.cpf)), ("senhaConsultante", cred.senha), ("numeroProcesso", digits(numero)),
            ("movimentos", True), ("incluirCabecalho", True), ("incluirDocumentos", incluir_documentos),
        ]
        campos += [("documento", d) for d in documentos or []]
        resp, att = self._call("consultarProcesso", campos)
        return parse_processo(resp, att)

    def consultar_teor_comunicacao(self, cred: Credential, numero: str, id_aviso: str) -> Teor:
        """ATENÇÃO: registra a ciência da intimação no PJe (pode iniciar o prazo)."""
        campos = [("idConsultante", digits(cred.cpf)), ("senhaConsultante", cred.senha),
                  ("numeroProcesso", digits(numero)), ("identificadorAviso", id_aviso)]
        resp, att = self._call("consultarTeorComunicacao", campos)
        return parse_teor(resp, att)
