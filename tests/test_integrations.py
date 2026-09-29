import json
from datetime import datetime

import httpx

from app.integrations import datajud, djen

DATAJUD_PAYLOAD = {
    "hits": {"hits": [{"_source": {
        "numeroProcesso": "12345672023826010",
        "grau": "G1",
        "classe": {"codigo": 7, "nome": "Procedimento Comum Cível"},
        "orgaoJulgador": {"nome": "1ª Vara Cível"},
        "movimentos": [
            {"codigo": 26, "nome": "Distribuição", "dataHora": "2023-02-01T10:00:00.000Z"},
            {"codigo": 85, "nome": "Petição", "dataHora": "2023-03-05T09:30:00.000Z",
             "complementosTabelados": [{"descricao": "tipo_de_peticao", "nome": "Contestação"}]},
            {"codigo": 1, "nome": "Sem data"},
        ],
    }}]}
}

DJEN_PAYLOAD = {
    "status": "success", "count": 1,
    "items": [{
        "id": 987, "numero_processo": "12345672023826010", "data_disponibilizacao": "2024-05-10",
        "siglaTribunal": "TJSP", "tipoComunicacao": "Intimação", "nomeOrgao": "1ª Vara Cível",
        "texto": "<p>Fica a parte intimada&nbsp;para <b>manifestação</b>.</p>", "link": "https://exemplo/doc",
    }],
}


def test_datajud_parse():
    res = datajud.parse_response(DATAJUD_PAYLOAD)
    assert res.found and res.classe == "Procedimento Comum Cível"
    assert [m.titulo for m in res.movements] == ["[G1] Distribuição", "[G1] Petição"]
    assert res.movements[1].texto == "tipo de peticao Contestação"
    assert res.movements[0].data == datetime(2023, 2, 1, 10, 0)


def test_datajud_not_found():
    assert not datajud.parse_response({"hits": {"hits": []}}).found


def test_datajud_fetch_sends_apikey_and_digits():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=DATAJUD_PAYLOAD)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        res = datajud.fetch("1234567-00.2023.8.26.0100", "tjsp", client=c)
    assert res.found
    assert seen["auth"] == "APIKey chave-teste"
    assert seen["url"].endswith("/api_publica_tjsp/_search")
    assert seen["body"]["query"]["match"]["numeroProcesso"] == "12345670020238260100"


def test_djen_parse_and_html():
    items = djen.parse_items(DJEN_PAYLOAD)
    assert len(items) == 1
    it = items[0]
    assert it.external_id == "987"
    assert "Intimação" in it.titulo and "TJSP" in it.titulo
    assert it.texto == "Fica a parte intimada\xa0para manifestação."


def test_djen_fetch_paginates_and_errors():
    calls = []

    def handler(request: httpx.Request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=DJEN_PAYLOAD)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        items = djen.fetch(oab="123456/sp", client=c)
    assert len(items) == 1 and len(calls) == 1
    assert calls[0]["numeroOab"] == "123456" and calls[0]["ufOab"] == "SP"

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="erro"))) as c:
        try:
            djen.fetch(numero_processo="1", client=c)
            assert False, "deveria falhar"
        except djen.DjenError:
            pass
