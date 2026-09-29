# Carteira Estratégica — Nogueira Vasconcelos

Aplicação web compartilhada, instalada no servidor local do escritório, para gestão da carteira de **processos
estratégicos**. Ela registra andamentos e os atualiza automaticamente (PJe autenticado via MNI, DataJud e
DJEN/Comunica PJe), mostra as **intimações pendentes no PJe**, mantém a **estratégia de longo prazo** de cada
processo (versionada), abre **pontos de decisão** discutidos com a equipe e com um assistente estratégico
(Claude) e transforma cada decisão em **próximos passos** com prazo e responsável. **Toda alteração fica
registrada para o administrador.**

Identidade visual conforme o Manual de Marca Nogueira Vasconcelos.

## Fluxo de trabalho

1. **Cadastro** do processo com número CNJ (dígito verificador validado; tribunal detectado pelo número) e a
   estratégia de longo prazo: objetivo do cliente, estratégia, premissas e riscos.
2. **Atualização automática dos andamentos** (a cada `SYNC_INTERVAL_HOURS`) ou manual, por três fontes:
   - **PJe autenticado (MNI)**: movimentos do processo com a credencial do advogado;
   - **DataJud (CNJ)**: movimentos processuais de todos os tribunais;
   - **DJEN / Comunica PJe (CNJ)**: publicações dos diários eletrônicos (também por OAB, com `DJEN_OABS`).
3. **Intimações do PJe** (a cada `AVISOS_INTERVAL_HOURS`): o aplicativo lista as intimações pendentes de cada
   advogado **sem registrar ciência** e as mostra no painel.
4. **Triagem pelo assistente** e **discussão de cada decisão** com a equipe e o assistente.
5. **Registro da decisão** e **próximos passos** (prazo, prazo fatal, responsável); agenda consolidada.
6. **Revisão da estratégia** sempre com motivo; todas as versões ficam guardadas.

No dia a dia, tudo pode ser incluído, editado e excluído: processos, andamentos (inclusive os importados),
próximos passos, notas e pontos de decisão.

## Auditoria (o que fica registrado)

- **Tudo que é gravado no banco** é registrado automaticamente: quem fez, quando, de qual IP e o que mudou,
  com os valores **antes e depois** de cada campo. Isso inclui inclusões, edições, exclusões, restaurações,
  expurgos e o que a sincronização automática faz (usuário "Sistema").
- Também ficam registrados: logins (e tentativas recusadas), logouts, consultas ao PJe, **abertura de teor de
  intimação (registro de ciência)**, download e abertura de documentos, exportações da auditoria.
- A trilha **não pode ser editada nem apagada pelo aplicativo**: não existe tela nem rota para isso.
- Os registros são **encadeados por hash (SHA-256)**. Em Administração › Auditoria, "Verificar integridade"
  detecta alterações feitas diretamente no arquivo do banco. Limite: quem tiver acesso ao arquivo do banco pode
  apagar os registros mais recentes ou recalcular a cadeia inteira. Para se proteger, restrinja o acesso ao
  servidor e exporte a auditoria (CSV) periodicamente para um local separado.
- Senhas e credenciais do PJe **nunca** aparecem na auditoria (só a indicação de que foram alteradas).
- Filtros por usuário, processo, tipo e período; exportação em CSV (abre no Excel).

## Exclusões e lixeira

Excluir **não apaga**: o item vai para a **lixeira** e some das telas. Para excluir um processo é obrigatório
informar o motivo. Somente o **administrador** pode:
- **restaurar** um item da lixeira;
- **expurgar** (apagar definitivamente), digitando `EXPURGAR`. Mesmo assim, o conteúdo apagado continua
  preservado na trilha de auditoria.

Um andamento importado que foi excluído **não volta** na próxima sincronização. Um andamento importado que foi
editado fica marcado como "editado", e a sincronização não sobrescreve a edição.

## Integração autenticada com o PJe (MNI)

O PJe oferece aos sistemas de advogados o **MNI (Modelo Nacional de Interoperabilidade)**, um webservice SOAP
autenticado com o **CPF e a senha do PJe** do advogado. O aplicativo usa três operações:

| Operação | Uso no aplicativo | Registra ciência? |
|---|---|---|
| `consultarAvisosPendentes` | lista as intimações pendentes de cada advogado (automático) | **não** |
| `consultarProcesso` | importa os movimentos; lista e baixa peças | não* |
| `consultarTeorComunicacao` | abre o teor da intimação | **SIM: pode iniciar o prazo** |

\* Até onde pude verificar, consultar o processo não equivale a abrir a intimação. Mas o tratamento pode
variar entre tribunais; confira antes de baixar uma peça ligada a uma intimação pendente.

**Abrir o teor registra a ciência no PJe** (Lei 11.419/2006, art. 5º). Por isso o aplicativo:
- nunca abre teor automaticamente;
- exige que o advogado digite `CIENTE` para confirmar;
- usa **somente a credencial de quem está abrindo**: a ciência é sempre registrada em nome de quem abriu, e o
  administrador não consegue abrir em nome de outro advogado;
- registra a ciência na auditoria, cria um andamento no processo com data, hora, autor e o prazo informado pelo
  tribunal, e salva os documentos anexos.

### Configuração
1. Gere a chave de criptografia com `python -m app.crypto` e coloque-a em `CREDENTIALS_KEY` no `.env`.
   **Guarde uma cópia dessa chave fora do servidor**: sem ela, as credenciais salvas não podem ser lidas
   (basta cadastrá-las de novo).
2. O administrador cadastra em **Administração › Endereços do PJe** o endereço MNI de cada tribunal e instância.
   Obtenha o endereço oficial com o tribunal. Em geral é algo como
   `https://pje.<tribunal>.jus.br/<instancia>/intercomunicacao`, e o WSDL fica no mesmo endereço com `?wsdl`.
   Informe a versão do MNI (2.2.2 ou 2.2.3), que aparece no WSDL.
3. Cada advogado cadastra o próprio CPF e a senha do PJe em **Minha conta** e clica em **Testar**. O teste só
   consulta as intimações pendentes e não registra ciência.

### Cuidados e limitações
- **Nem todo PJe libera o MNI para advogados**, e um WSDL publicado não garante que o serviço funcione.
  Teste tribunal por tribunal.
- O MNI funciona com login e senha. **Certificado digital A3 (token/cartão) não é usado**; se um tribunal exigir
  certificado para o MNI, a integração com aquele tribunal não funcionará.
- Se a senha for recusada, a credencial é **suspensa automaticamente**, para evitar o bloqueio da conta do
  advogado no tribunal por tentativas repetidas. Corrija a senha e clique em "Testar" para reativar.
- As credenciais ficam criptografadas no banco, com a chave no `.env` do mesmo servidor. Proteja o acesso ao
  servidor.
- **Validação:** o cliente MNI foi testado com uma **resposta real do MNI 2.2.3 do TJMG** (erro de login,
  publicada no projeto aberto `araujodgdev/lume-os`) e com respostas sintéticas montadas conforme os esquemas do
  MNI. **Não foi testado contra um tribunal real com credenciais válidas.** Faça o primeiro uso em um processo de
  controle e compare com o PJe.
- O controle oficial de prazos continua sendo o PJe e os autos. O aplicativo é apoio, não substitui a
  conferência.

## Instalação no servidor local

Requer Python 3.11 ou superior. Rode **uma única instância** (`--workers 1`), porque as tarefas automáticas
(sincronização, intimações e backup) rodam dentro do processo.

```bash
python -m venv .venv
# Linux:  source .venv/bin/activate        Windows:  .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # preencha SECRET_KEY, ADMIN_EMAIL/ADMIN_PASSWORD, CREDENTIALS_KEY…
python -m app.crypto            # gera a CREDENTIALS_KEY
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Acesse de qualquer computador da rede pelo endereço `http://<ip-do-servidor>:8000`. Na primeira execução, o
usuário de `ADMIN_EMAIL`/`ADMIN_PASSWORD` é criado como administrador; os demais advogados são cadastrados em
**Administração › Equipe**.

- **Iniciar com o servidor:** Linux: `deploy/carteira.service` (systemd). Windows: `deploy/iniciar-windows.bat`
  no Agendador de Tarefas, com o disparo "Ao iniciar o computador".
- **HTTPS (recomendado):** as senhas trafegam pela rede do escritório. Com um proxy reverso com certificado (por
  exemplo Caddy ou nginx), ative `SESSION_HTTPS_ONLY=true`. Sem HTTPS, restrinja o acesso à rede interna e à VPN
  e **não exponha a porta para a internet**.
- **Backup:** automático todos os dias às 23h em `DATA_DIR/backups` (mantém os últimos `BACKUP_KEEP`). Para um
  backup manual, use `python -m app.backup`. Copie a pasta `DATA_DIR` (backups e documentos) para outro disco ou
  para a nuvem do escritório, e guarde o `.env` à parte.
- **Atualizações:** o banco é migrado automaticamente (colunas novas são adicionadas e nenhum dado é apagado).
  Faça um backup antes.

Testes: `python -m pytest -q`

## Configuração

| Variável | Uso |
|---|---|
| `SECRET_KEY` | Assinatura dos cookies de sessão. Obrigatório trocar. |
| `DATABASE_URL` | SQLite por padrão (adequado para uma equipe pequena em servidor local). |
| `CREDENTIALS_KEY` | Chave de criptografia das credenciais do PJe (`python -m app.crypto`). |
| `AVISOS_INTERVAL_HOURS` | Intervalo da consulta de intimações pendentes no PJe; `0` desativa. |
| `SYNC_INTERVAL_HOURS` | Intervalo da atualização de andamentos; `0` desativa. |
| `SESSION_HTTPS_ONLY` | `true` somente se o acesso for por HTTPS. |
| `DATA_DIR` / `BACKUP_KEEP` | Pasta de documentos e backups; quantidade de backups mantidos. |
| `ANTHROPIC_API_KEY` | Habilita o assistente estratégico. |
| `CLAUDE_MODEL` / `CLAUDE_EFFORT` | Modelo (padrão `claude-opus-5-5`) e profundidade de raciocínio. |
| `DATAJUD_API_KEY` | Chave pública do DataJud (<https://datajud-wiki.cnj.jus.br/api-publica/acesso>). |
| `DJEN_OABS` / `DJEN_DATE_FORMAT` | OABs monitoradas no DJEN; formato de data (`iso` ou `br`). |

## Pontos a confirmar antes de usar em produção

1. **MNI:** o endereço e a versão de cada tribunal, e se o tribunal aceita login e senha (ver acima).
2. **DJEN:** o formato de data dos filtros (as fontes divergem entre `AAAA-MM-DD` e `DD/MM/AAAA`) e os nomes dos
   campos da resposta (Swagger do CNJ: <https://app.swaggerhub.com/apis-docs/cnj/pcp/1.0.0>).
3. **DataJud:** o alias do tribunal sugerido pelo número CNJ.

## Privacidade e sigilo (LGPD / sigilo profissional)

Ao usar o assistente, o registro do processo (estratégia, andamentos, notas e discussão) é enviado à API da
Anthropic. Avalie a política de retenção de dados do contrato com a Anthropic e a ciência do cliente,
especialmente para processos em segredo de justiça. Sem `ANTHROPIC_API_KEY`, nada é enviado. As consultas ao PJe,
ao DataJud e ao DJEN vão diretamente do servidor do escritório aos tribunais e ao CNJ.

## Marca

- Paleta: `#2e2e2d` grafite · `#6f7b63` sálvia · `#ccc4b8` areia · `#f1f5f0` gelo (`app/static/style.css`).
- Logotipo: `app/static/logo.svg`, extraído em vetor do manual (sem redesenho).
- A tipografia primária **Boston Angel Bold** é fonte licenciada e não está no repositório. Se o escritório tiver
  licença para web, coloque `BostonAngel-Bold.woff2` em `app/static/fonts/`; até lá, os títulos usam Lato Bold em
  caixa alta (tipografia secundária oficial).
- Única cor fora da paleta: um vermelho sóbrio usado **apenas** para alertas (prazo vencido ou fatal, exclusão,
  registro de ciência). Nunca é aplicado à marca.

## Estrutura

```
app/
  main.py              rotas principais (FastAPI) e tarefas automáticas
  routes_pje.py        intimações, ciência, peças e credenciais do PJe
  routes_admin.py      equipe, endereços do PJe, auditoria, lixeira
  models.py            processos, andamentos, decisões, passos, notas, estratégia, PJe, auditoria
  audit.py             auditoria automática com cadeia de hash
  pje_service.py       regras do PJe: intimações, ciência, documentos, credenciais
  integrations/        mni.py (PJe autenticado), datajud.py, djen.py
  ai.py                assistente estratégico (Claude)
  sync.py              sincronização e deduplicação de andamentos
  migrate.py, backup.py, crypto.py, cnj.py, web.py, auth.py
  templates/, static/  interface
deploy/                serviço systemd (Linux) e script de inicialização (Windows)
tests/                 testes automatizados (inclui resposta real do MNI do TJMG em tests/fixtures)
```
