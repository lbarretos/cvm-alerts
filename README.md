# CVM Alerts

Alertas no Telegram para as empresas da sua cobertura na B3:

- **Fatos Relevantes e Comunicados ao Mercado**, com resumo automático por LLM (Deepseek), ao longo do dia.
- **Negociações de insiders (VLMO)**: aviso quando chega formulário novo e resumo semanal com os destaques.

Tudo roda no GitHub Actions; não há servidor.

## Início rápido

### 1. Clonar

```bash
git clone https://github.com/lbarretos/cvm-alerts.git
cd cvm-alerts
pip install -r requirements.txt
```

### 2. Secrets

Em **Settings → Secrets and variables → Actions**:

| Secret | Valor | Onde obter |
|--------|-------|-----------|
| `CVM_USERNAME` | Login do Download Múltiplo | Cadastro na CVM ([Download Múltiplo](https://conteudo.cvm.gov.br/menu/regulados/companhias/download_multiplo/index.html)); login e senha chegam por e-mail |
| `CVM_PASSWORD` | Senha do Download Múltiplo | Expira periodicamente — veja [Senha](#senha-expirada-ou-login-incorreto) |
| `TELEGRAM_BOT_TOKEN` | Token do bot | `@BotFather` |
| `TELEGRAM_CHAT_ID` | ID do chat (vários separados por vírgula) | `@userinfobot` |
| `DEEPSEEK_API_KEY` | API key | [platform.deepseek.com](https://platform.deepseek.com) |
| `TELEGRAM_BOT_TOKEN_2` / `TELEGRAM_CHAT_ID_2` | Opcional: segundo bot | `@BotFather` |

### 3. Empresas monitoradas

`config/cnpj_map.yaml` é a lista que gera alertas. Cada empresa precisa de CNPJ, código CVM (`ccvm`) e nome:

```yaml
tickers:
  AZUL4: {cnpj: "09.305.994/0001-29", ccvm: "24112", denom: "AZUL S.A."}
  GOLL4: {cnpj: "06.164.253/0001-87", ccvm: "19569", denom: "GOL LINHAS AEREAS INTELIGENTES SA"}
```

`config/coverage.yaml` é só a lista de referência por setor. `scripts/build_cnpj_map.py` busca os CNPJs dela no cadastro da CVM e grava em `config/cnpj_map.generated.yaml`; o `ccvm` é preenchido à mão.

## O que você recebe

### Fatos Relevantes e Comunicados (`ipe_watcher.py`)

A cada 3 h, das 09h às 21h BRT (09h, 12h, 15h, 18h, 21h), seg–sex. Para cada documento novo de uma empresa da cobertura:

- **Fato Relevante** e **Comunicado ao Mercado**: sempre.
- **Aviso / Comunicado aos Acionistas**: exceto rotina (pagamento de dividendos/JCP já anunciado, calendário de eventos, atas de AGO/AGE sem pauta material, política de divulgação, código de conduta, estatuto sem mudança de capital ou controle). Palavras-chave como aquisição, OPA, recuperação judicial, renúncia ou CVM forçam o envio.
- Mensagem com resumo do LLM em 3 tópicos (Fato / Impacto / Risco-Catalisador) e link do PDF.

Cada documento é enviado uma vez (dedup por hash em `datasets/ipe_processed_ids.json`).

### Negociações de insiders (VLMO)

| Aviso | Quando | Conteúdo |
|---|---|---|
| Diário | quando chega formulário VLMO novo (via `ipe_watcher.py`) | todas as negociações da empresa no mês |
| Semanal | segunda, 09h BRT (`br_stock_trading.py`) | só destaques P1/P2, por empresa |

Formato:

```
*LOCALIZA FLEET S.A.* (RENT3)
• Controlador comprou 10.000 ações, valor R$ 600 mil, preço médio R$ 60,00
• Diretor vendeu 2.000 ações, valor R$ 100 mil, preço médio R$ 50,00
```

- Só o **último mês divulgado** (os formulários vencem 10 dias após o fim do mês; meses anteriores nunca são avisados).
- **P1**: Controlador, Conselho de Administração ou Diretor com mais de R$ 500 mil. **P2**: empresa com mais de R$ 2 mi negociados no mês.
- O resumo semanal manda cada empresa uma vez por mês; se os números mudarem (entrega atrasada, nova versão), ela volta marcada "atualizado".
- O dado semanal vem do CSV de dados abertos da CVM, que só traz insiders: recompra pela própria companhia só aparece quando a descrição do movimento cita "recompra". O aviso diário, lido do PDF, traz a seção de tesouraria.

## Workflows

| Workflow | Arquivo | Quando |
|---|---|---|
| B3 IPE & VLMO Monitor | `ipe_watcher.yml` | a cada 3 h, 09h–21h BRT, seg–sex |
| CVM Weekly VLMO Pipeline | `vlmo_weekly.yml` | segunda, 09h07 BRT |
| Tests | `tests.yml` | todo PR e push na `main`; auditoria de dependências toda segunda |

Rodar manualmente: **Actions → [workflow] → Run workflow**.

Se um workflow falhar, chega um aviso no Telegram com o link da execução (o watcher não repete o aviso quando ele mesmo já alertou, como em senha expirada).

O CSV consolidado do VLMO (~20 MB) fica como artifact da execução semanal (`vlmo-consolidated`, 90 dias), não no git.

## Estrutura

```
.
├── ipe_watcher.py            # Fatos Relevantes, Comunicados e aviso diário de VLMO
├── br_stock_trading.py       # Pipeline semanal de VLMO (dados abertos da CVM)
├── alerts.py                 # Regras e formato do VLMO (P1/P2, mês de referência)
├── telegram_notifier.py      # Envio ao Telegram (ritmo, 429, fallback sem Markdown)
├── config/
│   ├── cnpj_map.yaml         # Empresas que geram alertas
│   ├── coverage.yaml         # Cobertura por setor (referência)
│   └── system_prompt_ipe.txt # Prompt do LLM
├── datasets/                 # Estado versionado no git
│   ├── ipe_processed_ids.json
│   ├── ipe_last_run_date.txt
│   ├── ipe_skipped_log.csv
│   └── vlmo_summary_state.json
├── reports/                  # Relatório HTML semanal
└── tests/
```

## Senha expirada ou login incorreto

O watcher para na hora (workflow vermelho) e manda alerta no Telegram. A B3 responde com o erro `1`; as mensagens observadas são `LOGIN INCORRETO`, `SENHA EXPIRADA` e `USUÁRIO SUSPENSO`.

**Trocar a senha** (Nota Técnica do Download Múltiplo, §5):
- [cas.bovespa.com.br/cas](https://cas.bovespa.com.br/cas/) → login → menu **Senhas** → senha atual e nova → **Alterar**; ou
- [rad.cvm.gov.br/ENET](https://www.rad.cvm.gov.br/ENET) → login → ícone do triângulo verde → **Alteração de senha**.

Depois atualize o secret `CVM_PASSWORD`.

**Conta suspensa:** `suporteexterno@cvm.gov.br` · `0800-944-3535`.

> Os sistemas da CVM saem do ar para manutenção alguns dias por ano. Verifique avisos em [gov.br/cvm](https://www.gov.br/cvm/pt-br) antes de acionar o suporte.

## Rodar localmente

```bash
python -m pytest tests -q

CVM_USERNAME=... CVM_PASSWORD=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
DEEPSEEK_API_KEY=... python ipe_watcher.py
```

Atenção: rodar localmente envia mensagens reais e grava estado em `datasets/`.

## Documentação

`CLAUDE.md` traz arquitetura, regras e customizações em detalhe.
