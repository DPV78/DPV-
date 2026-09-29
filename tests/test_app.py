import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ai, cnj
from app.db import SessionLocal
from app.main import app
from app.models import Case, Decision, DiscussionMessage, Movement, NextStep, User
from app.sync import sync_case
from tests.test_integrations import DATAJUD_PAYLOAD, DJEN_PAYLOAD

NUMERO = f"1234567-{cnj.check_digits('1234567', '2023', '8', '26', '0100')}.2023.8.26.0100"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        r = c.post("/login", data={"email": "admin@escritorio.test", "password": "senha-segura-123"})
        assert r.status_code == 200 and "Painel da carteira" in r.text
        yield c


def csrf(client, url="/"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def test_requires_login():
    with TestClient(app) as anon:
        r = anon.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_csrf_enforced(client):
    r = client.post("/cases/new", data={"csrf": "x", "numero_cnj": NUMERO, "titulo": "t", "cliente": "c"})
    assert r.status_code == 400


def test_full_flow(client):
    token = csrf(client, "/cases/new")
    r = client.post("/cases/new", data={
        "csrf": token, "numero_cnj": NUMERO, "titulo": "Anulatória Contrato X", "cliente": "Empresa A",
        "objetivo": "Anular a cláusula de multa", "estrategia": "Tese de abusividade; tutela de urgência.",
    })
    assert r.status_code == 200 and "Anulatória Contrato X" in r.text
    with SessionLocal() as db:
        case = db.query(Case).filter_by(numero_cnj=NUMERO).one()
        assert case.tribunal == "tjsp"
        assert len(case.strategy_versions) == 1
        case_id = case.id

    # número inválido é rejeitado
    r = client.post("/cases/new", data={"csrf": token, "numero_cnj": "1234567-00.2023.8.26.0100", "titulo": "x", "cliente": "y"})
    assert "inválido" in r.text

    # revisão da estratégia exige motivo e gera versão
    client.post(f"/cases/{case_id}/strategy", data={"csrf": token, "estrategia": "Nova", "motivo": "Liminar negada"})
    with SessionLocal() as db:
        assert len(db.get(Case, case_id).strategy_versions) == 2

    client.post(f"/cases/{case_id}/movements", data={"csrf": token, "data": "2024-05-01", "titulo": "Audiência designada"})
    client.post(f"/cases/{case_id}/decisions", data={"csrf": token, "titulo": "Recorrer?", "contexto": "Cliente quer acordo"})
    with SessionLocal() as db:
        d = db.query(Decision).filter_by(case_id=case_id).one()
        decision_id = d.id

    client.post(f"/decisions/{decision_id}/messages", data={"csrf": token, "content": "Acho que devemos agravar."})
    client.post(f"/decisions/{decision_id}/decide", data={"csrf": token, "decisao": "Agravar", "fundamentos": "Risco de dano"})
    client.post(f"/cases/{case_id}/steps", data={"csrf": token, "descricao": "Minutar agravo", "prazo": "2024-05-20", "prazo_fatal": "1", "decision_id": str(decision_id)})
    with SessionLocal() as db:
        d = db.get(Decision, decision_id)
        assert d.status == "decidida" and d.decisao == "Agravar"
        step = db.query(NextStep).filter_by(case_id=case_id).one()
        assert step.prazo_fatal and step.decision_id == decision_id

    page = client.get(f"/decisions/{decision_id}").text
    assert "Acho que devemos agravar." in page and "Agravar" in page
    assert "Minutar agravo" in client.get("/agenda").text

    # assistente sem chave responde 503 sem quebrar
    r = client.post(f"/decisions/{decision_id}/assistant", headers={"X-CSRF-Token": token})
    assert r.status_code == 503


def test_sync_dedup():
    with SessionLocal() as db:
        case = db.query(Case).filter_by(numero_cnj=NUMERO).one()
        dj = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=DATAJUD_PAYLOAD)))
        dn = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=DJEN_PAYLOAD)))
        first = sync_case(db, case, datajud_client=dj, djen_client=dn)
        second = sync_case(db, case, datajud_client=dj, djen_client=dn)
        assert first["novos"] == {"pje": 0, "datajud": 2, "djen": 1}
        assert second["novos"] == {"pje": 0, "datajud": 0, "djen": 0}
        assert db.query(Movement).filter_by(case_id=case.id, fonte="datajud").count() == 2
        assert case.classe == "Procedimento Comum Cível"


def test_build_messages_alternates():
    with SessionLocal() as db:
        d = db.query(Decision).first()
        u = db.query(User).first()
        db.add_all([
            DiscussionMessage(decision_id=d.id, role="user", author_id=u.id, content="Segunda opinião"),
            DiscussionMessage(decision_id=d.id, role="assistant", content="Análise"),
        ])
        db.commit()
        db.refresh(d)
        msgs = ai.build_messages(d.case, d)
    roles = [m["role"] for m in msgs]
    assert roles[0] == "user" and roles[-1] == "user"
    assert all(a != b for a, b in zip(roles, roles[1:]))
    assert "<registro_do_processo>" in msgs[0]["content"]
    assert "Estratégia:\nNova" in msgs[0]["content"]
