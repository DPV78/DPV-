"""Rotas da integração autenticada com o PJe (MNI)."""
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, crypto, pje_service
from .auth import current_user
from .cnj import digits
from .config import settings
from .db import get_db
from .integrations.mni import MniError
from .models import Case, PjeAviso, PjeCredential, PjeDocument, PjeEndpoint, User
from .web import back, check_csrf, flash, get_or_404, render

router = APIRouter()
CONFIRM_WORD = "CIENTE"


@router.get("/pje", response_class=HTMLResponse)
def pje_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user), status: str = "pendente"):
    q = select(PjeAviso).order_by(PjeAviso.data_disponibilizacao.desc(), PjeAviso.id.desc())
    if status != "todos":
        q = q.where(PjeAviso.status == status)
    avisos = db.scalars(q.limit(300)).all()
    my_endpoints = {c.endpoint_id for c in db.scalars(select(PjeCredential).where(PjeCredential.user_id == user.id, PjeCredential.ativo.is_(True)))}
    return render(request, "pje.html", user, avisos=avisos, status=status, my_endpoints=my_endpoints)


@router.post("/pje/check")
def pje_check(request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    res = pje_service.check_avisos(db, only_user_id=None if user.is_admin else user.id)
    msg = f"Consulta concluída: {res['novos']} intimação(ões) nova(s)."
    if res["erros"]:
        msg += " Problemas: " + " | ".join(res["erros"])
    flash(request, msg)
    return back("/pje")


@router.get("/pje/avisos/{aviso_id}", response_class=HTMLResponse)
def aviso_page(aviso_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    aviso = get_or_404(db, PjeAviso, aviso_id)
    has_cred = db.scalar(select(PjeCredential.id).where(PjeCredential.user_id == user.id, PjeCredential.endpoint_id == aviso.endpoint_id,
                                                        PjeCredential.ativo.is_(True))) is not None
    cases = db.scalars(select(Case).where(Case.deleted_at.is_(None)).order_by(Case.titulo)).all() if aviso.case_id is None else []
    return render(request, "pje_aviso.html", user, aviso=aviso, has_cred=has_cred, confirm_word=CONFIRM_WORD, cases=cases)


@router.post("/pje/avisos/{aviso_id}/abrir")
def aviso_open(aviso_id: int, request: Request, csrf: str = Form(""), confirmacao: str = Form(""),
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    aviso = get_or_404(db, PjeAviso, aviso_id)
    if confirmacao.strip().upper() != CONFIRM_WORD:
        flash(request, f"Para abrir o teor, digite {CONFIRM_WORD} no campo de confirmação.")
        return back(f"/pje/avisos/{aviso_id}")
    try:
        pje_service.open_teor(db, aviso, user)
        flash(request, "Teor aberto. A ciência foi registrada no PJe em seu nome — confira o prazo nos autos e registre o próximo passo.")
    except (pje_service.PjeUnavailable, crypto.CryptoUnavailable) as e:
        flash(request, str(e))
    return back(f"/pje/avisos/{aviso_id}")


@router.post("/pje/avisos/{aviso_id}/vincular")
def aviso_link(aviso_id: int, request: Request, csrf: str = Form(""), case_id: int = Form(...),
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    aviso = get_or_404(db, PjeAviso, aviso_id)
    aviso.case_id = get_or_404(db, Case, case_id).id
    db.commit()
    return back(f"/pje/avisos/{aviso_id}")


@router.get("/cases/{case_id}/pje/documentos", response_class=HTMLResponse)
def case_documents(case_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = get_or_404(db, Case, case_id)
    try:
        docs = pje_service.list_documents(db, case, user)
    except (pje_service.PjeUnavailable, crypto.CryptoUnavailable, MniError) as e:
        flash(request, f"Não foi possível listar as peças: {e}")
        return back(f"/cases/{case_id}#pje")
    baixados = {d.id_documento for d in db.scalars(select(PjeDocument).where(PjeDocument.case_id == case_id))}
    return render(request, "pje_docs.html", user, case=case, docs=docs, baixados=baixados)


@router.post("/cases/{case_id}/pje/documentos/{id_documento}")
def case_document_download(case_id: int, id_documento: str, request: Request, csrf: str = Form(""),
                           db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    case = get_or_404(db, Case, case_id)
    try:
        doc = pje_service.download_document(db, case, id_documento, user)
        flash(request, f"Documento salvo: {doc.descricao}")
    except (pje_service.PjeUnavailable, crypto.CryptoUnavailable) as e:
        flash(request, str(e))
    return back(f"/cases/{case_id}#pje")


@router.get("/documentos/{doc_id}")
def document_file(doc_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    doc = get_or_404(db, PjeDocument, doc_id)
    path = Path(doc.path).resolve()
    base = (Path(settings.data_dir) / "documentos").resolve()
    if base not in path.parents or not path.exists():
        raise HTTPException(404, "Arquivo não encontrado")
    audit.log(db, f"Abriu o documento {doc.descricao}", tipo="acesso", case_id=doc.case_id, entidade="pje_documento", entidade_id=doc.id)
    db.commit()
    return FileResponse(path, media_type=doc.mimetype, filename=path.name)


# ------------------------------------------------------------------ credenciais do próprio advogado

@router.post("/account/pje")
async def credential_save(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Salva CPF e senha do PJe para um ou mais endereços (ex.: 1º e 2º graus do mesmo tribunal)."""
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    cpf, senha = digits(str(form.get("cpf", ""))), str(form.get("senha", ""))
    ids = [int(i) for i in form.getlist("endpoint_id") if str(i).isdigit()]
    if len(cpf) != 11 or not senha or not ids:
        flash(request, "Informe CPF (11 dígitos), senha e ao menos um tribunal/instância.")
        return back("/account#pje")
    try:
        cpf_enc = crypto.encrypt(cpf)
    except crypto.CryptoUnavailable as e:
        flash(request, str(e))
        return back("/account#pje")
    nomes = []
    for ep_id in ids:
        ep = get_or_404(db, PjeEndpoint, ep_id)
        senha_enc = crypto.encrypt(senha)
        cred = db.scalar(select(PjeCredential).where(PjeCredential.user_id == user.id, PjeCredential.endpoint_id == ep.id))
        if cred is None:
            db.add(PjeCredential(user_id=user.id, endpoint_id=ep.id, cpf_enc=cpf_enc, senha_enc=senha_enc))
        else:
            cred.cpf_enc, cred.senha_enc, cred.ativo, cred.last_error = cpf_enc, senha_enc, True, None
        nomes.append(f"{ep.tribunal.upper()} {ep.instancia}")
    db.commit()
    flash(request, f"Credencial salva (criptografada) para: {', '.join(nomes)}. Use \"Testar\" em cada uma.")
    return back("/account#pje")


@router.post("/account/pje/{cred_id}/test")
def credential_test(cred_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Testa a credencial consultando intimações pendentes (operação que NÃO registra ciência)."""
    check_csrf(request, csrf)
    cred = get_or_404(db, PjeCredential, cred_id)
    if cred.user_id != user.id:
        raise HTTPException(403)
    cred.ativo = True
    try:
        avisos = pje_service.client_for(cred.endpoint).consultar_avisos_pendentes(pje_service.credential_of(cred))
        pje_service._mark(cred, None)
        flash(request, f"Credencial válida. {len(avisos)} intimação(ões) pendente(s) neste tribunal.")
    except MniError as e:
        pje_service._mark(cred, e)
        flash(request, f"Falha: {e}")
    except crypto.CryptoUnavailable as e:
        flash(request, str(e))
    audit.log(db, f"Testou credencial do PJe ({cred.endpoint.tribunal} {cred.endpoint.instancia})", tipo="pje")
    db.commit()
    return back("/account#pje")


@router.post("/account/pje/{cred_id}/delete")
def credential_delete(cred_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_csrf(request, csrf)
    cred = get_or_404(db, PjeCredential, cred_id)
    if cred.user_id != user.id and not user.is_admin:
        raise HTTPException(403)
    for aviso in db.scalars(select(PjeAviso).where(PjeAviso.credential_id == cred.id)):
        aviso.credential_id = None
    db.delete(cred)
    db.commit()
    flash(request, "Credencial removida.")
    return back("/account#pje")
