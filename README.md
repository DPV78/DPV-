# Carteira Estratégica — Nogueira Vasconcelos

Aplicação web compartilhada para gestão da carteira de **processos estratégicos** do escritório: registra
andamentos, busca atualizações automaticamente (DataJud e DJEN/Comunica PJe), mantém a **estratégia de longo
prazo** de cada processo (versionada), abre **pontos de decisão** discutidos com a equipe e com um assistente
estratégico (Claude), e transforma cada decisão em **próximos passos** com prazo e responsável.

Identidade visual conforme o Manual de Marca Nogueira Vasconcelos (paleta institucional, Lato, logotipo em vetor).

## Fluxo de trabalho

1. **Cadastro** do processo com número CNJ (dígito verificador validado; tribunal detectado pelo número) e a
   estratégia de longo prazo: objetivo do cliente, estratégia, premissas e riscos.
2. **Sincronização** automática (a cada `SYNC_INTERVAL_HOURS`) ou manual:
   - **DataJud (CNJ)**: movimentos processuais codificados (TPU) de todos os tribunais;
   - **DJEN / Comunica PJe (CNJ)**: publicações e intimações dos diários eletrônicos, por processo e,
     opcionalmente, varrendo as OABs da equipe (`DJEN_OABS`).
   Andamentos novos entram como "não lidos" no painel.
3. **Triagem pelo assistente** ("Analisar novos andamentos"): resume cada andamento, classifica o impacto
   para a estratégia e abre pontos de decisão quando necessário.
4. **Discussão da decisão**: advogados e assistente debatem no mesmo fio. O assistente recebe o registro
   completo (estratégia, premissas, decisões anteriores, passos pendentes, andamentos, notas).
5. **Registro da decisão** (o quê e por quê) e **próximos passos**, digitados ou sugeridos pelo assistente e
   revisados antes de salvar.
6. **Revisão da estratégia** sempre com motivo; todas as versões ficam guardadas.

Tudo fica em histórico de atividades por processo (quem fez o quê e quando).

## Instalação

Requer Python 3.11+.

```bash
pip install -r requirements.txt
cp .env.example .env      # preencha as variáveis
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Na primeira execução, o usuário definido em `ADMIN_EMAIL`/`ADMIN_PASSWORD` é criado como administrador.
Os demais advogados são cadastrados em **Equipe**.

Testes: `python -m pytest -q`

## Configuração

| Variável | Uso |
|---|---|
| `SECRET_KEY` | Assinatura dos cookies de sessão. Obrigatório trocar em produção. |
| `DATABASE_URL` | SQLite por padrão. Para vários advogados em servidor, prefira PostgreSQL. |
| `ANTHROPIC_API_KEY` | Habilita o assistente estratégico. |
| `CLAUDE_MODEL` / `CLAUDE_EFFORT` | Modelo (padrão `claude-opus-5-5`) e profundidade de raciocínio (`low` a `max`; padrão `high`). |
| `DATAJUD_API_KEY` | Chave pública do DataJud, publicada pelo CNJ em <https://datajud-wiki.cnj.jus.br/api-publica/acesso>. |
| `DJEN_OABS` | OABs a monitorar no DJEN, ex.: `123456/SP,654321/RJ`. |
| `DJEN_DATE_FORMAT` | `iso` ou `br` (veja "Pontos a confirmar"). |
| `SYNC_INTERVAL_HOURS` | Intervalo da sincronização automática; `0` desativa. |

## Integração com PJe: o que está coberto e o que não está

- **Coberto sem credenciais:** movimentos (DataJud) e publicações/intimações (DJEN), para todos os tribunais.
- **Não coberto (ainda):** inteiro teor de peças, expedientes com prazo aberto no painel do PJe e peticionamento.
  Isso depende do acesso autenticado do advogado (MNI, webservice SOAP de cada tribunal, em regra com
  certificado digital). O ponto de extensão está descrito em `app/integrations/pje.py`. Não recomendo automatizar
  o login com o certificado por navegador (scraping).
- O DataJud tem **defasagem** em relação ao tribunal e não traz o texto dos atos. **O controle oficial de prazos
  continua sendo a intimação/publicação e os autos.** O aplicativo é apoio, não substitui a conferência.

## Pontos a confirmar antes de usar em produção

As APIs reais do CNJ não puderam ser testadas no ambiente de desenvolvimento (sem acesso de rede a elas). Os
clientes foram testados com respostas simuladas. Confira:

1. **DJEN:** o formato de data dos filtros `dataDisponibilizacaoInicio/Fim` (as fontes divergem entre
   `AAAA-MM-DD` e `DD/MM/AAAA`; ajuste com `DJEN_DATE_FORMAT`) e os nomes dos campos da resposta na documentação
   oficial (Swagger do CNJ: <https://app.swaggerhub.com/apis-docs/cnj/pcp/1.0.0>). O parser aceita as variações
   conhecidas (`numero_processo`/`numeroProcesso`, `data_disponibilizacao` etc.).
2. **DJEN por OAB:** o número pode estar gravado com sufixos (ex.: `123456-A`) em alguns tribunais; teste as
   variações da OAB de cada advogado.
3. **DataJud:** o alias do tribunal (`tjsp`, `trf3`, `trt2`, `stj`…) sugerido a partir do número CNJ; ajuste
   no cadastro se o processo não for localizado.

## Privacidade e sigilo (LGPD / sigilo profissional)

Ao usar o assistente, o registro do processo (estratégia, andamentos, notas e discussão) é enviado à API da
Anthropic. Avalie a política de retenção de dados do contrato com a Anthropic e a ciência do cliente,
especialmente para processos em segredo de justiça. Sem `ANTHROPIC_API_KEY`, nada é enviado.
Em produção: HTTPS obrigatório, `SECRET_KEY` forte, backup do banco e acesso restrito à rede do escritório ou VPN.

## Marca

- Paleta: `#2e2e2d` grafite · `#6f7b63` sálvia · `#ccc4b8` areia · `#f1f5f0` gelo (`app/static/style.css`).
- Logotipo: `app/static/logo.svg`, extraído em vetor do manual (sem redesenho).
- A tipografia primária **Boston Angel Bold** é fonte licenciada e não está no repositório. Se o escritório tiver
  licença para web, coloque `BostonAngel-Bold.woff2` em `app/static/fonts/`; até lá, os títulos usam Lato Bold em
  caixa alta (tipografia secundária oficial).
- Única cor fora da paleta: um vermelho sóbrio usado **apenas** para sinalizar prazo vencido ou fatal, por
  segurança operacional. Nunca é aplicado à marca.

## Estrutura

```
app/
  main.py              rotas web (FastAPI) e agendador de sincronização
  models.py            processos, andamentos, decisões, discussão, passos, versões da estratégia, auditoria
  ai.py                assistente estratégico (Claude): discussão, triagem, sugestão de passos
  sync.py              sincronização e deduplicação de andamentos
  cnj.py               validação/formatação do número CNJ e detecção do tribunal
  integrations/        datajud.py, djen.py, pje.py (ponto de extensão)
  templates/, static/  interface
tests/                 testes automatizados
```
