"""Endereços MNI pré-cadastrados para os tribunais da carteira: TJRO e TRF1, 1º e 2º graus.

Nenhum destes endereços pôde ser testado a partir do ambiente de desenvolvimento. Rode o diagnóstico em
Administração › Endereços do PJe no servidor do escritório: ele confirma o endereço e detecta a versão do MNI.
Se o tribunal informar outro endereço, edite-o na mesma tela.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PjeEndpoint

PRESETS = [
    {
        "tribunal": "tjro", "instancia": "1g", "url": "https://pjepg.tjro.jus.br/pje/intercomunicacao",
        "origem": "Host oficial do PJe 1º grau do TJRO (pjepg.tjro.jus.br/pje); caminho /intercomunicacao "
                  "inferido do padrão do PJe — confirmar com o diagnóstico ou com o TJRO.",
    },
    {
        "tribunal": "tjro", "instancia": "2g", "url": "https://pjesg.tjro.jus.br/pje/intercomunicacao",
        "origem": "Host oficial do PJe 2º grau do TJRO (pjesg.tjro.jus.br/pje); caminho /intercomunicacao "
                  "inferido do padrão do PJe — confirmar com o diagnóstico ou com o TJRO.",
    },
    {
        "tribunal": "trf1", "instancia": "1g", "url": "https://pje1g.trf1.jus.br/pje/intercomunicacao",
        "origem": "Tabela de endereços MNI do projeto aberto araujodgdev/lume-os (não é fonte oficial) — "
                  "confirmar com o diagnóstico ou com o TRF1.",
    },
    {
        "tribunal": "trf1", "instancia": "2g", "url": "https://pje2g.trf1.jus.br/pje/intercomunicacao",
        "origem": "Tabela de endereços MNI do projeto aberto araujodgdev/lume-os (não é fonte oficial) — "
                  "confirmar com o diagnóstico ou com o TRF1.",
    },
]


def seed_presets(db: Session) -> int:
    """Cadastra os endereços que ainda não existem (mesmo tribunal e instância). Não altera os existentes."""
    added = 0
    for p in PRESETS:
        exists = db.scalar(select(PjeEndpoint.id).where(PjeEndpoint.tribunal == p["tribunal"], PjeEndpoint.instancia == p["instancia"]))
        if exists is None:
            db.add(PjeEndpoint(versao_mni="2.2.2", **p))
            added += 1
    return added
