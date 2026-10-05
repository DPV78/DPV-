import re

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import cnj, pje_service
from app.db import SessionLocal
from app.integrations import mni
from app.main import app
from app.models import Case, Movement, PjeCredential, PjeEndpoint
from tests.test_mni import NS, PROCESSO, soap

# Processo fictício no TRF1 (J=4, TR=01) e no TJRO (J=8, TR=22)
TRF1 = f"1000001-{cnj.check_digits('1000001', '2025', '4', '01', '4100')}.2025.4.01.4100"
TJRO = f"7000001-{cnj.check_digits('7000001', '2025', '8', '22', '0001')}.2025.8.22.0001"

WSDL_223 = """<?xml version="1.0"?><wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
 targetNamespace="http://www.cnj.jus.br/servico-intercomunicacao-2.2.3/"><wsdl:portType name="servico-intercomunicacao-223">
 <wsdl:operation name="consultarAvisosPendentes"/><wsdl:operation name="consultarProcesso"/><wsdl:operation name="consultarTeorComunicacao"/>
 </wsdl:portType></wsdl:definitions>"""


def test_tribunal_detection():
    assert cnj.is_valid(TRF1) and cnj.guess_tribunal(TRF1) == "trf1"
    assert cnj.is_valid(TJRO) and cnj.guess_tribunal(TJRO) == "tjro"


def test_presets_seeded_on_first_run():
    with TestClient(app):
        pass
    with SessionLocal() as db:
        eps = {(e.tribunal, e.instancia): e for e in db.scalars(select(PjeEndpoint))}
    for key in [("tjro", "1g"), ("tjro", "2g"), ("trf1", "1g"), ("trf1", "2g")]:
        assert key in eps and eps[key].url.startswith("https://") and eps[key].origem
    assert eps[("tjro", "1g")].url == "https://pjepg.tjro.jus.br/pje/intercomunicacao"
    assert eps[("trf1", "2g")].url == "https://pje2g.trf1.jus.br/pje/intercomunicacao"


def test_diagnostico():
    ok = mni.diagnosticar("https://x/pje/intercomunicacao", client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=WSDL_223)) ))
    assert ok.ok and ok.versao == "2.2.3"
    redirect = mni.diagnosticar("https://x", client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://x/login.seam"}))))
    assert not redirect.ok and "login" in redirect.mensagem
    html = mni.diagnosticar("https://x", client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>PJe</html>"))))
    assert not html.ok and "HTML" in html.mensagem


def login(c):
    c.post("/login", data={"email": "admin@escritorio.test", "password": "senha-segura-123"})
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/account").text).group(1)


def test_one_password_for_both_degrees_and_2g_only_case(monkeypatch):
    with TestClient(app) as c:
        t = login(c)
        with SessionLocal() as db:
            ids = [e.id for e in db.scalars(select(PjeEndpoint).where(PjeEndpoint.tribunal == "trf1"))]
        r = c.post("/account/pje", data={"csrf": t, "cpf": "123.456.789-09", "senha": "pje-trf1", "endpoint_id": [str(i) for i in ids]})
        assert "TRF1 1g" in r.text and "TRF1 2g" in r.text
        with SessionLocal() as db:
            creds = db.scalars(select(PjeCredential).where(PjeCredential.endpoint_id.in_(ids))).all()
            assert len(creds) == 2 and all(pje_service.credential_of(cr).senha == "pje-trf1" for cr in creds)

        c.post("/cases/new", data={"csrf": t, "numero_cnj": TRF1, "titulo": "Caso TRF1", "cliente": "C"})
        with SessionLocal() as db:
            case_id = db.scalar(select(Case.id).where(Case.numero_cnj == TRF1))

        nao_achado = soap(f'<ns7:consultarProcessoResposta {NS}><sucesso>false</sucesso><mensagem>Processo não encontrado</mensagem></ns7:consultarProcessoResposta>')
        proc = PROCESSO.replace("12345674720238260100", cnj.digits(TRF1))

        def handler(req: httpx.Request):
            body = nao_achado if "pje1g" in str(req.url) else proc
            return httpx.Response(200, text=body, headers={"content-type": "text/xml"})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        real = pje_service.client_for
        monkeypatch.setattr(pje_service, "client_for", lambda ep, h=None: real(ep, http))
        r = c.post(f"/cases/{case_id}/sync", data={"csrf": t})
        assert "não encontrado" not in r.text  # normal: o processo só existe no 2º grau
        with SessionLocal() as db:
            movs = db.scalars(select(Movement).where(Movement.case_id == case_id, Movement.fonte == "pje")).all()
            assert len(movs) == 2 and all(m.titulo.startswith("[PJe 2g]") for m in movs)
            assert all(cr.ativo and cr.last_error is None for cr in db.scalars(select(PjeCredential).where(PjeCredential.endpoint_id.in_(ids))))


def test_admin_diagnose_route_updates_version(monkeypatch):
    with TestClient(app) as c:
        t = login(c)
        real = mni.diagnosticar
        monkeypatch.setattr(mni, "diagnosticar", lambda url, client=None: real(url, client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=WSDL_223)))))
        r = c.post("/admin/pje/diagnosticar", data={"csrf": t})
        assert "respondendo como MNI" in r.text
        with SessionLocal() as db:
            ep = db.scalar(select(PjeEndpoint).where(PjeEndpoint.tribunal == "tjro", PjeEndpoint.instancia == "2g"))
            assert ep.last_check_ok and ep.versao_mni == "2.2.3"


def test_portal_link_derived_from_mni_url():
    with SessionLocal() as db:
        ep = db.scalar(select(PjeEndpoint).where(PjeEndpoint.tribunal == "tjro", PjeEndpoint.instancia == "1g"))
        assert ep.portal == "https://pjepg.tjro.jus.br/pje/"
        ep.portal_url = "https://pjepg.tjro.jus.br/pje/login.seam"
        assert ep.portal.endswith("login.seam")
        db.rollback()


def test_external_ciencia_via_whom_is_audited():
    from datetime import date

    from app.models import AuditLog, PjeAviso

    with SessionLocal() as db:
        ep = db.scalar(select(PjeEndpoint).where(PjeEndpoint.tribunal == "tjro", PjeEndpoint.instancia == "2g"))
        aviso = PjeAviso(endpoint_id=ep.id, id_aviso="w1", numero_processo=TJRO, data_disponibilizacao=date.today())
        db.add(aviso)
        db.commit()
        aviso_id = aviso.id
    with TestClient(app) as c:
        t = login(c)
        page = c.get(f"/pje/avisos/{aviso_id}").text
        assert "https://pjesg.tjro.jus.br/pje/" in page and "Whom" in page
        c.post(f"/pje/avisos/{aviso_id}/ciencia-externa", data={"csrf": t, "observacao": "aberto com Whom"})
    with SessionLocal() as db:
        a = db.get(PjeAviso, aviso_id)
        assert a.status == "ciencia_externa" and a.opened_by_id is not None
        log = db.scalar(select(AuditLog).where(AuditLog.tipo == "pje_ciencia", AuditLog.entidade_id == aviso_id).order_by(AuditLog.id.desc()))
        assert "fora do aplicativo" in log.acao and "aberto com Whom" in log.acao
