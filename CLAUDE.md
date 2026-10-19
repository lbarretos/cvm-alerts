# CLAUDE.md — Guia de Desenvolvimento

## Contexto do Projeto

**cvm-alerts** manda alertas no Telegram para as empresas da cobertura (`config/cnpj_map.yaml`):

1. **Documentos IPE** (Fato Relevante, Comunicados, Avisos) com resumo por LLM — `ipe_watcher.py`, de hora em hora.
2. **Negociações de insiders (VLMO)** — aviso diário (PDF via B3, em `ipe_watcher.py`) e resumo semanal de destaques P1/P2 (CSV de dados abertos da CVM, em `br_stock_trading.py`).

### Fluxo do watcher (`ipe_watcher.py`)

```
B3 Download Múltiplo (https://seguro.bmfbovespa.com.br/rad/download/SolicitaDownload.asp)
    ↓ POST txtLogin, txtSenha, txtData (cada dia desde ipe_last_run_date, inclusive),
    ↓      txtHora=00:00, txtDocumento=IPE, txtAssuntoIPE=SIM
XML ISO-8859-1 (<DownloadMultiplo><Link .../></DownloadMultiplo> ou <ERROS>)
    ↓ só Situacao=Liberado; filtro por ccvm da cobertura e categoria
    ↓ dedup por hash (datasets/ipe_processed_ids.json)
    ├─ VLMO: só último mês divulgado → parse do PDF → um aviso em lote por execução
    └─ demais: pre_filter → PDF → texto (12k chars) → Deepseek → Telegram
```

### Fluxo semanal (`br_stock_trading.py`)

```
dados.cvm.gov.br/dados/CIA_ABERTA/DOC/VLMO/DADOS/*.zip (2018 → hoje)
    ↓ arquivos *_con_* ; mantém só a última versão de cada (CNPJ, mês)
datasets/Brazil_Stock_Trading_Consolidated.csv  (artifact do workflow, fora do git)
    ↓ alerts.weekly_summary: cobertura, último mês, compras/vendas/recompras, P1/P2
    ↓ só empresas novas ou com números alterados (datasets/vlmo_summary_state.json)
Telegram (um bloco por empresa) + reports/latest_report.html
```

## Arquitetura

### `ipe_watcher.py`

| Função | Responsabilidade |
|--------|------------------|
| `download_ipe_b3()` | Consulta a B3 para um dia; trata erros pelo `NUMERO_DO_ERRO` (ver Troubleshooting) |
| `_query_b3()` | POST com retry (rede, resposta não-XML, erro 22013); nunca repete erro de credencial |
| `load_company_map()` | ccvm → ticker/CNPJ/nome a partir de `config/cnpj_map.yaml` |
| `pre_filter()` | Regras de descarte para Avisos/Comunicados aos Acionistas; palavras-chave que forçam envio |
| `make_hash()` | SHA256 de CNPJ\|DT_REF\|TIPO\|LINK (dedup) |
| `download_pdf()` / `extract_text()` | PDF → texto (até `LLM_TEXT_CHARS`) |
| `call_llm()` | Deepseek (`deepseek-chat`, timeout 60 s) com `config/system_prompt_ipe.txt` |
| `send_notification()` | Mensagem do documento; retorna se foi entregue |
| `parse_vlmo_pdf()` / `send_vlmo_batch_notification()` | Aviso diário de VLMO |
| `main()` | Orquestração, janela de datas, estado, alertas de falha |

### Outros módulos

| Módulo | Responsabilidade |
|--------|------------------|
| `alerts.py` | Regras e formato do VLMO: `latest_disclosed_month`, P1/P2, `weekly_summary`, `format_trade_line` |
| `br_stock_trading.py` | Download e consolidação dos CSVs da CVM; envia o resumo semanal; gera o relatório |
| `telegram_notifier.py` | Envio: vários destinatários, fallback sem Markdown (HTTP 400), espera `retry_after` (HTTP 429), `send_blocks` em ritmo de 1,5 s |
| `reports/report_generator.py` | Relatório HTML semanal |
| `scripts/build_cnpj_map.py` | Utilitário: busca CNPJs dos tickers de `config/coverage.yaml`; grava em `config/cnpj_map.generated.yaml` |

### Regras de VLMO (`alerts.py`)

- Só o **último mês divulgado** (`latest_disclosed_month`: o mês anterior a partir do dia 11; antes disso, o retrasado — os formulários vencem 10 dias após o fim do mês). Meses anteriores nunca são avisados.
- Linhas agregadas por empresa, cargo e sentido: `• Controlador comprou X ações, valor Y, preço médio Z`.
- Semanal: só ações/units, só compra, venda e recompra com valor > 0.
- **P1**: Controlador / Conselho de Administração / Diretor com linha > R$ 500 mil. **P2**: total negociado pela empresa no mês > R$ 2 mi. Não há P3.
- O semanal envia cada empresa uma vez por mês de referência e de novo só se os números mudarem ("atualizado"). O estado só é gravado após a entrega.
- O CSV da CVM só traz insiders (não identifica a pessoa); recompra pela companhia aparece apenas quando a descrição cita "recompra". O aviso diário (PDF) traz a seção de tesouraria.

### Estado

Tudo versionado no git, commitado pelos workflows:

| Arquivo | Dono | Conteúdo |
|---|---|---|
| `datasets/ipe_processed_ids.json` | watcher | hashes já enviados ou descartados |
| `datasets/ipe_last_run_date.txt` | watcher | último dia consultado sem erro |
| `datasets/ipe_skipped_log.csv` | watcher | documentos descartados pelo `pre_filter` |
| `datasets/vlmo_summary_state.json` | semanal | fingerprint por (CNPJ, mês) já enviado |

### Deps

pandas, numpy, requests, pdfplumber, openai (cliente da Deepseek), pyyaml, beautifulsoup4, colorama — versões fixas em `requirements.txt`. Atualize de propósito: rode os testes e só então suba a versão.

## Customizações Comuns

### 1. Empresas monitoradas

Edite `config/cnpj_map.yaml`. Cada ticker precisa de `cnpj`, `ccvm` (código CVM, usado pelo watcher) e `denom`. Tickers da mesma empresa (ex.: POMO3/POMO4) repetem CNPJ e ccvm.

### 2. Filtro extra no `pre_filter()`

```python
# ipe_watcher.py, pre_filter(), antes de `return True, ""`
if "palavra" in al:  # al = assunto (Especie) em minúsculas
    return False, "razão do descarte"
```

Fato Relevante e Comunicado ao Mercado nunca passam pelas regras de descarte.

### 3. Prompt do LLM

`config/system_prompt_ipe.txt` é o prompt de sistema, usado como está. A mensagem do usuário é montada em `call_llm()`:

```
Tipo: <categoria>
Empresa: <nome>
Data: <DT_REF>

<texto do PDF>
```

### 4. Limites de P1/P2

`P1_VOLUME_THRESHOLD` e `P2_MONTHLY_THRESHOLD` em `alerts.py`.

### 5. Horários

`.github/workflows/ipe_watcher.yml` e `vlmo_weekly.yml` (cron em UTC; BRT = UTC−3). Use minuto quebrado: o agendador do GitHub atrasa mais as execuções em hora cheia, às vezes horas. Atraso não causa perda: o watcher reconsulta desde `ipe_last_run_date.txt` (inclusive).

## Estrutura de Dados

### XML da B3 (Nota Técnica §4.1)

```xml
<?xml version="1.0" encoding="ISO-8859-1" ?>
<DownloadMultiplo DataSolicitada="dd/mm/aaaa hh:mm" TipoDocumento="IPE" DataConsulta="dd/mm/aaaa hh:mm">
  <Link url="..." Documento="IPE" ccvm="24392" DataRef="23/04/2026 09:00:00"
        FrmDtRef="..." Categoria="Fato Relevante" Tipo="..." Especie="..." Situacao="Liberado" />
</DownloadMultiplo>
```

O manual não documenta em qual atributo vem o assunto com `txtAssuntoIPE=SIM`; o log `B3 Link attributes` mostra o que a API envia.

### DataFrame do watcher

| Coluna | Fonte |
|--------|-------|
| ccvm | `@ccvm` |
| DT_REF | `@DataRef` — data de **referência** do documento, não de entrega (ISO) |
| CATEG_DOC | `@Categoria` |
| TIPO_DOC | `@Tipo` |
| LINK_DOC | `@url` |
| ASSUNTO | `@Especie` |
| CNPJ_CIA / DENOM_CIA / _cnpj_digits | `config/cnpj_map.yaml` pelo ccvm |

## Testes

```bash
python3 -m pytest tests -q                       # tudo
python3 -m pytest tests/test_weekly.py -v        # VLMO semanal e Telegram
python3 -m pytest tests/test_ipe_watcher.py::test_force_keyword_aquisicao -v
```

Exemplo de resposta da B3: `SAMPLE_B3_XML` em `tests/test_ipe_watcher.py`. O workflow `tests.yml` roda a suíte em todo PR e push na `main`.

## GitHub Actions

| Workflow | Arquivo | Quando | Faz |
|---|---|---|---|
| B3 IPE & VLMO Monitor | `ipe_watcher.yml` | de hora em hora 06h–22h BRT, seg–sex | roda o watcher; commita o estado (mesmo se falhar) |
| CVM Weekly VLMO Pipeline | `vlmo_weekly.yml` | segunda 09h07 BRT | resumo semanal; artifact `vlmo-consolidated` (90 dias); commita estado e relatório |
| Tests | `tests.yml` | PR, push na `main`, segunda (auditoria) | pytest; `pip-audit`; busca de credenciais no código |

Os dois primeiros mandam aviso no Telegram quando falham. O watcher grava `.alert_sent` quando já alertou ele mesmo (credencial, B3 fora do ar), e o aviso genérico não se repete.

`ipe_watcher.yml` ainda restaura o cache antigo de IDs processados (antes de 2026-10 o dedup vivia só no cache do Actions) e o mescla com a cópia do git. Esses dois passos podem sair depois de algumas semanas.

### Secrets

| Secret | Usado por |
|---|---|
| `CVM_USERNAME`, `CVM_PASSWORD` | watcher |
| `DEEPSEEK_API_KEY` | watcher |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (vários IDs separados por vírgula) | ambos |
| `TELEGRAM_BOT_TOKEN_2`, `TELEGRAM_CHAT_ID_2` (opcional) | ambos |

### Logs do watcher

```
INFO Querying 2 date(s): 2026-10-19, 2026-10-20
INFO Querying B3 API for IPE filings on 19/10/2026
INFO B3 Link attributes: ['Categoria', 'DataRef', ...]
INFO B3 API returned 461 IPE records for 19/10/2026
INFO 2 rows after category+ccvm filter
INFO Done. Processed 1 new documents.
```

## Troubleshooting

### Erros da API B3 (Nota Técnica §4.2)

| `NUMERO_DO_ERRO` | Significado | Tratamento |
|---|---|---|
| `1` | LOGIN INCORRETO (na prática também SENHA EXPIRADA, USUÁRIO SUSPENSO) | `CredentialError` → alerta + `exit(1)`, sem retry |
| `22013` | Erro ao gerar o XML | retry (3 tentativas) |
| `22014` / `22015` / `22017` | Data / hora / tipo de documento inválido | `B3RequestError` → alerta + `exit(1)` (bug no script) |
| `22016` | Nenhum registro localizado | resultado vazio (normal em fim de semana/feriado) |
| outro / rede / resposta não-XML | — | a data fica pendente e o run termina com `exit(1)`; se todas falharem, alerta "B3 API indisponível" |

Semântica (§4.3): a API devolve o link na data da **última ação** sobre o documento (liberação, bloqueio, cancelamento), numa janela de até 24 h a partir de `txtData`/`txtHora`.

### Senha expirada / login incorreto (erro 1)

1. Trocar a senha em [cas.bovespa.com.br/cas](https://cas.bovespa.com.br/cas/) → "Senhas", **ou** [rad.cvm.gov.br/ENET](https://www.rad.cvm.gov.br/ENET) → "Alteração de senha" (Nota Técnica §5).
2. Atualizar o secret `CVM_PASSWORD`.
3. Conta suspensa: `suporteexterno@cvm.gov.br` · `0800-944-3535`.

> Os sistemas da CVM saem do ar para manutenção periodicamente (avisos em gov.br/cvm). Durante a janela, aguarde antes de contatar o suporte.

### Notificação repetida

Confira se o passo **Commit state** do `ipe_watcher.yml` rodou e fez push; sem ele, o próximo run não sabe o que já foi enviado.

### Workflow agendado parou de rodar

O GitHub desativa workflows agendados em alguns casos (ex.: inatividade do repositório). Verifique em **Actions → [workflow]** se aparece "This workflow was disabled" e reative.

## Performance (medida em 2026-10)

- Watcher: ~45 s por execução (~22 s esperando a B3, ~16 s de `pip install`).
- Semanal: download ~3 s, processamento ~2 s; o resto é envio ao Telegram.

## Próximos Passos (Ideias)

- [ ] Resumo diário para Comunicados ao Mercado (Fato Relevante continua imediato)
- [ ] Dedup de documento reapresentado (mesma espécie e data de referência, URL nova)
- [ ] Webhook HTTP / e-mail além do Telegram
