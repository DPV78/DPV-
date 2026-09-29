import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import audit, cnj, pje_service
from app.db import SessionLocal
from app.integrations import mni
from app.main import app
from app.models import AuditLog, Case, Movement, NextStep, PjeAviso, PjeCredential, PjeDocument, PjeEndpoint
from tests.test_mni import AVISOS, PROCESSO, teor_resp

NUM = f"7654321-{cnj.check_digits('7654321', '2024', '8', '13', '0024')}.2024.8.13.0024"


def login(c, email, pw):
    r = c.post("/login", data={"email": email, "password": pw})
    assert r.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/account").text).group(1)


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        token = login(c, "admin@escritorio.test", "senha-segura-123")
        c.post("/users", data={"csrf": token, "name": "Beatriz Advogada", "email": "bia@escritorio.test", "password": "outra-senha-123"})
        yield c, token


@pytest.fixture(scope="module")
def lawyer(admin):
    with TestClient(app) as c:
        yield c, login(c, "bia@escritorio.test", "outra-senha-123")


@pytest.fixture(scope="module")
def case_id(lawyer):
    c, t = lawyer
    c.post("/cases/new", data={"csrf": t, "numero_cnj": NUM, "titulo": "Caso MG", "cliente": "Cliente B"})
    with SessionLocal() as db:
        return db.scalar(select(Case.id).where(Case.numero_cnj == NUM))


def logs(**kw):
    with SessionLocal() as db:
        q = select(AuditLog)
        for k, v in kw.items():
            q = q.where(getattr(AuditLog, k) == v)
        return db.scalars(q.order_by(AuditLog.id)).all()


def test_non_admin_blocked_from_admin_pages(lawyer):
    c, _ = lawyer
    for url in ("/admin/auditoria", "/admin/lixeira", "/admin/pje", "/users"):
        assert c.get(url).status_code == 403


def test_create_is_audited_with_author(case_id):
    [entry] = [e for e in logs(case_id=case_id, entidade="processo", tipo="criar")]
    assert entry.user_name == "Beatriz Advogada" and entry.ip
    assert json.loads(entry.detalhes)["depois"]["numero_cnj"] == NUM


def test_movement_edit_and_delete_are_audited(lawyer, case_id):
    c, t = lawyer
    c.post(f"/cases/{case_id}/movements", data={"csrf": t, "data": "2026-01-10", "titulo": "Audiência designada"})
    with SessionLocal() as db:
        mid = db.scalar(select(Movement.id).where(Movement.case_id == case_id))
    c.post(f"/movements/{mid}/edit", data={"csrf": t, "data": "2026-01-11", "titulo": "Audiência redesignada", "texto": "novo"})
    edit = logs(entidade="andamento", tipo="editar")[-1]
    det = json.loads(edit.detalhes)
    assert det["antes"]["titulo"] == "Audiência designada" and det["depois"]["titulo"] == "Audiência redesignada"
    c.post(f"/movements/{mid}/delete", data={"csrf": t})
    assert logs(entidade="andamento", tipo="excluir")
    assert f"/movements/{mid}/edit" not in c.get(f"/cases/{case_id}").text


def test_step_crud(lawyer, case_id):
    c, t = lawyer
    c.post(f"/cases/{case_id}/steps", data={"csrf": t, "descricao": "Contestação", "prazo": "2026-02-01"})
    with SessionLocal() as db:
        sid = db.scalar(select(NextStep.id).where(NextStep.case_id == case_id))
    r = c.post(f"/steps/{sid}/edit", data={"csrf": t, "descricao": "Contestar", "prazo": "2026-02-03", "status": "pendente"})
    assert r.status_code == 200
    det = json.loads(logs(entidade="proximo_passo", tipo="editar")[-1].detalhes)
    assert det["antes"]["prazo"] == "2026-02-01" and det["depois"]["prazo"] == "2026-02-03"
    r = c.post(f"/steps/{sid}/delete", data={"csrf": t}, headers={"referer": f"http://testserver/steps/{sid}/edit"})
    assert r.url.path == f"/cases/{case_id}"


def test_case_soft_delete_restore_and_purge(admin, lawyer):
    c, t = lawyer
    num = f"1111111-{cnj.check_digits('1111111', '2024', '8', '26', '0001')}.2024.8.26.0001"
    c.post("/cases/new", data={"csrf": t, "numero_cnj": num, "titulo": "Caso descartável", "cliente": "X"})
    with SessionLocal() as db:
        cid = db.scalar(select(Case.id).where(Case.numero_cnj == num))
    c.post(f"/cases/{cid}/notes", data={"csrf": t, "texto": "nota sigilosa"})
    r = c.post(f"/cases/{cid}/delete", data={"csrf": t, "motivo": ""})
    assert "motivo" in r.text
    c.post(f"/cases/{cid}/delete", data={"csrf": t, "motivo": "cadastrado por engano"})
    assert c.get(f"/cases/{cid}").status_code == 404
    assert "Caso descartável" not in c.get("/").text
    # advogado não restaura
    assert c.post(f"/admin/lixeira/processo/{cid}/restaurar", data={"csrf": t}).status_code == 403

    a, at = admin
    assert "Caso descartável" in a.get("/admin/lixeira").text
    a.post(f"/admin/lixeira/processo/{cid}/restaurar", data={"csrf": at})
    assert a.get(f"/cases/{cid}").status_code == 200
    # expurgo exige estar na lixeira e confirmação
    assert a.post(f"/admin/lixeira/processo/{cid}/expurgar", data={"csrf": at, "confirmacao": "EXPURGAR"}).status_code == 400
    c.post(f"/cases/{cid}/delete", data={"csrf": t, "motivo": "de novo"})
    a.post(f"/admin/lixeira/processo/{cid}/expurgar", data={"csrf": at, "confirmacao": "nao"})
    with SessionLocal() as db:
        assert db.get(Case, cid) is not None
    a.post(f"/admin/lixeira/processo/{cid}/expurgar", data={"csrf": at, "confirmacao": "EXPURGAR"})
    with SessionLocal() as db:
        assert db.get(Case, cid) is None
    purged = [e for e in logs(case_id=cid, tipo="expurgar")]
    assert any("nota sigilosa" in (e.detalhes or "") for e in purged)  # conteúdo preservado na auditoria


def test_audit_page_filters_and_csv(admin, case_id):
    a, _ = admin
    page = a.get(f"/admin/auditoria?case_id={case_id}").text
    assert "Beatriz Advogada" in page and "Criou processo" in page
    csv = a.get(f"/admin/auditoria.csv?case_id={case_id}")
    assert csv.headers["content-type"].startswith("text/csv") and "Criou processo" in csv.text


def test_pje_flow(admin, lawyer, case_id, monkeypatch):
    a, at = admin
    c, t = lawyer
    a.post("/admin/pje", data={"csrf": at, "tribunal": "tjmg", "instancia": "1g",
                               "url": "https://pje.exemplo.jus.br/pje/intercomunicacao", "versao_mni": "2.2.3"})
    with SessionLocal() as db:
        ep_id = db.scalar(select(PjeEndpoint.id).where(PjeEndpoint.tribunal == "tjmg"))
    c.post("/account/pje", data={"csrf": t, "endpoint_id": ep_id, "cpf": "111.222.333-44", "senha": "segredo-pje"})
    with SessionLocal() as db:
        cred = db.scalar(select(PjeCredential))
        assert "segredo" not in cred.senha_enc and "11122233344" not in cred.cpf_enc
        assert pje_service.credential_of(cred).senha == "segredo-pje"
    assert not any("segredo-pje" in (e.detalhes or "") for e in logs())

    calls = []
    avisos_xml = AVISOS.replace("12345674720238260100", cnj.digits(NUM))
    proc_xml = PROCESSO.replace("12345674720238260100", cnj.digits(NUM))

    def handler(req: httpx.Request):
        op = req.headers["soapaction"].strip('"').rsplit("/", 1)[-1]
        calls.append(op)
        body = {"consultarAvisosPendentes": avisos_xml, "consultarProcesso": proc_xml,
                "consultarTeorComunicacao": teor_resp(b"%PDF-teor")}[op]
        return httpx.Response(200, text=body, headers={"content-type": "text/xml"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    real = pje_service.client_for
    monkeypatch.setattr(pje_service, "client_for", lambda ep, h=None: real(ep, http))

    # consulta de avisos não abre teor
    c.post("/pje/check", data={"csrf": t})
    assert calls == ["consultarAvisosPendentes"]
    with SessionLocal() as db:
        aviso = db.scalar(select(PjeAviso))
        assert aviso.case_id == case_id and aviso.status == "pendente"
        aviso_id = aviso.id
    assert "Intimações pendentes no PJe" in c.get("/").text

    # sem a palavra de confirmação, nada acontece
    c.post(f"/pje/avisos/{aviso_id}/abrir", data={"csrf": t, "confirmacao": "sim"})
    assert "consultarTeorComunicacao" not in calls
    # admin sem credencial própria não pode registrar ciência em nome de outro advogado
    a.post(f"/pje/avisos/{aviso_id}/abrir", data={"csrf": at, "confirmacao": "CIENTE"})
    assert "consultarTeorComunicacao" not in calls

    c.post(f"/pje/avisos/{aviso_id}/abrir", data={"csrf": t, "confirmacao": "ciente"})
    assert calls.count("consultarTeorComunicacao") == 1
    with SessionLocal() as db:
        aviso = db.get(PjeAviso, aviso_id)
        assert aviso.status == "aberto" and aviso.prazo_dias == 15 and aviso.opened_by.name == "Beatriz Advogada"
        mov = db.scalar(select(Movement).where(Movement.fonte == "pje-intimacao"))
        assert "ciência registrada" in mov.titulo and "15 dia(s)" in mov.titulo
        doc = db.scalar(select(PjeDocument))
        assert open(doc.path, "rb").read() == b"%PDF-teor"
    assert [e for e in logs(tipo="pje_ciencia") if "CIÊNCIA REGISTRADA" in e.acao]
    assert c.get(f"/documentos/{doc.id}").content == b"%PDF-teor"

    # sincronização traz movimentos do PJe sem duplicar
    c.post(f"/cases/{case_id}/sync", data={"csrf": t})
    c.post(f"/cases/{case_id}/sync", data={"csrf": t})
    with SessionLocal() as db:
        assert db.query(Movement).filter_by(case_id=case_id, fonte="pje").count() == 2


def test_wrong_password_suspends_credential(lawyer, monkeypatch):
    c, t = lawyer
    from pathlib import Path

    body = (Path(__file__).parent / "fixtures" / "mni-tjmg-avisos-senha-invalida.txt").read_bytes()
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, content=body)))
    real = pje_service.client_for
    monkeypatch.setattr(pje_service, "client_for", lambda ep, h=None: real(ep, http))
    c.post("/pje/check", data={"csrf": t})
    with SessionLocal() as db:
        cred = db.scalar(select(PjeCredential))
        assert cred.ativo is False and cred.last_error.startswith("credencial")


def test_chain_detects_tampering():
    with SessionLocal() as db:
        ok, bad, total = audit.verify_chain(db)
        assert ok and total > 10
        victim = db.scalar(select(AuditLog.id).where(AuditLog.hash.is_not(None)).order_by(AuditLog.id).offset(3).limit(1))
        db.execute(text("UPDATE audit_log SET acao = 'adulterado' WHERE id = :i"), {"i": victim})
        db.commit()
        ok, bad, _ = audit.verify_chain(db)
        assert not ok and bad == victim
