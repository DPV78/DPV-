import base64
from pathlib import Path

import httpx
import pytest

from app.integrations import mni

FIXTURES = Path(__file__).parent / "fixtures"
NS = 'xmlns:ns2="http://www.cnj.jus.br/intercomunicacao-2.2.3" xmlns:ns7="http://www.cnj.jus.br/servico-intercomunicacao-2.2.3/" xmlns="http://www.cnj.jus.br/tipos-servico-intercomunicacao-2.2.3"'


def soap(inner: str) -> str:
    return f'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>{inner}</soap:Body></soap:Envelope>'


AVISOS = soap(f"""<ns7:consultarAvisosPendentesResposta {NS}><sucesso>true</sucesso><mensagem>ok</mensagem>
<aviso idAviso="555" tipoComunicacao="INT"><ns2:destinatario><ns2:pessoa nome="EMPRESA A"/></ns2:destinatario>
<ns2:processo numero="12345674720238260100"><ns2:orgaoJulgador nomeOrgao="1ª Vara Cível"/></ns2:processo>
<ns2:dataDisponibilizacao>20260310143000</ns2:dataDisponibilizacao></aviso></ns7:consultarAvisosPendentesResposta>""")

PROCESSO = soap(f"""<ns7:consultarProcessoResposta {NS}><sucesso>true</sucesso><mensagem/>
<processo><ns2:dadosBasicos numero="12345674720238260100" classeProcessual="7"><ns2:orgaoJulgador nomeOrgao="1ª Vara"/></ns2:dadosBasicos>
<ns2:movimento dataHora="20260301100000" identificadorMovimento="m1"><ns2:movimentoNacional codigoNacional="26"><ns2:complemento>Distribuído por sorteio</ns2:complemento></ns2:movimentoNacional></ns2:movimento>
<ns2:movimento dataHora="20260305090000"><ns2:movimentoLocal codigoMovimento="99" descricao="Juntada de petição"/></ns2:movimento>
<ns2:documento idDocumento="d1" descricao="Petição inicial" mimetype="application/pdf" dataHora="20260301100000"/>
</processo></ns7:consultarProcessoResposta>""")


def teor_resp(pdf: bytes) -> str:
    return soap(f"""<ns7:consultarTeorComunicacaoResposta {NS}><sucesso>true</sucesso><mensagem/>
<comunicacao prazo="15" tipoPrazo="DIAS_UTEIS"><ns2:teor>Fica a parte intimada.</ns2:teor>
<ns2:documento idDocumento="d9" descricao="Decisão" mimetype="application/pdf"><ns2:conteudo>{base64.b64encode(pdf).decode()}</ns2:conteudo></ns2:documento>
</comunicacao></ns7:consultarTeorComunicacaoResposta>""")


def test_real_tjmg_mtom_error_is_credential_failure():
    body = (FIXTURES / "mni-tjmg-avisos-senha-invalida.txt").read_bytes()
    with pytest.raises(mni.MniError) as e:
        mni.read_response(body, None, "consultarAvisosPendentes")
    assert e.value.kind == "credencial"
    assert "login" in str(e.value)


def test_envelope_escapes_and_namespaces():
    xml = mni.envelope("consultarAvisosPendentes", [("idConsultante", "123"), ("senhaConsultante", "a<b&c")], "2.2.3")
    assert "servico-intercomunicacao-2.2.3/" in xml and "tipos-servico-intercomunicacao-2.2.3" in xml
    assert "a&lt;b&amp;c" in xml


def test_parse_avisos():
    resp, _ = mni.read_response(AVISOS.encode(), "text/xml", "consultarAvisosPendentes")
    [a] = mni.parse_avisos(resp)
    assert a.id_aviso == "555" and a.numero_processo == "12345674720238260100"
    assert a.data_disponibilizacao.isoformat() == "2026-03-10"
    assert a.orgao == "1ª Vara Cível" and a.destinatario == "EMPRESA A" and a.tipo_comunicacao == "INT"


def test_parse_processo():
    resp, att = mni.read_response(PROCESSO.encode(), "text/xml", "consultarProcesso")
    p = mni.parse_processo(resp, att)
    assert [m.descricao for m in p.movimentos] == ["Distribuído por sorteio", "Juntada de petição"]
    assert p.movimentos[0].external_id == "m1" and p.movimentos[0].codigo == "26"
    assert p.documentos[0].id_documento == "d1" and p.documentos[0].conteudo is None


def test_mtom_attachment_via_xop():
    xml = soap(f"""<ns7:consultarTeorComunicacaoResposta {NS}><sucesso>true</sucesso>
<comunicacao><ns2:teor>T</ns2:teor><ns2:documento idDocumento="x" descricao="Anexo" mimetype="application/pdf">
<ns2:conteudo><xop:Include xmlns:xop="http://www.w3.org/2004/08/xop/include" href="cid:anexo1@cxf"/></ns2:conteudo></ns2:documento>
</comunicacao></ns7:consultarTeorComunicacaoResposta>""")
    body = (b"--B1\r\nContent-Type: application/xop+xml; charset=UTF-8\r\nContent-ID: <root>\r\n\r\n" + xml.encode()
            + b"\r\n--B1\r\nContent-Type: application/pdf\r\nContent-Transfer-Encoding: binary\r\nContent-ID: <anexo1@cxf>\r\n\r\n%PDF-bin\r\n--B1--\r\n")
    resp, att = mni.read_response(body, 'multipart/related; boundary="B1"; type="application/xop+xml"', "consultarTeorComunicacao")
    teor = mni.parse_teor(resp, att)
    assert teor.documentos[0].conteudo == b"%PDF-bin"


def test_client_sends_soapaction_and_classifies_block():
    seen = {}

    def handler(req: httpx.Request):
        seen["action"] = req.headers["soapaction"]
        seen["body"] = req.content.decode()
        return httpx.Response(200, text=AVISOS, headers={"content-type": "text/xml"})

    c = mni.MniClient("https://pje.exemplo/pje/intercomunicacao", "2.2.3", client=httpx.Client(transport=httpx.MockTransport(handler)))
    avisos = c.consultar_avisos_pendentes(mni.Credential("111.222.333-44", "s"))
    assert len(avisos) == 1
    assert seen["action"] == '"http://www.cnj.jus.br/servico-intercomunicacao-2.2.3/consultarAvisosPendentes"'
    assert "<tip:idConsultante>11122233344</tip:idConsultante>" in seen["body"]

    blocked = mni.MniClient("https://x", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="no"))))
    with pytest.raises(mni.MniError) as e:
        blocked.consultar_avisos_pendentes(mni.Credential("1", "s"))
    assert e.value.kind == "bloqueio"


def test_certificate_required_is_not_a_wrong_password():
    xml = soap(f'<ns7:consultarTeorComunicacaoResposta {NS}><sucesso>false</sucesso>'
               '<mensagem>Operação permitida somente com login por certificado digital</mensagem></ns7:consultarTeorComunicacaoResposta>')
    with pytest.raises(mni.MniError) as e:
        mni.read_response(xml.encode(), "text/xml", "consultarTeorComunicacao")
    assert e.value.kind == "exige_certificado"
