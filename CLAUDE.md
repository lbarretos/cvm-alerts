# CLAUDE.md — Guia de Desenvolvimento

## Contexto do Projeto

**cvm-alerts** monitora Fatos Relevantes (FRs) e Comunicados ao Mercado de empresas brasileiras. 

### Fluxo de Dados

```
B3 API (seguro.bmfbovespa.com/rad/download/SolicitaDownload.asp)
    ↓ [POST: txtData, txtHora, txtDocumento=IPE]
XML (ISO-8859-1)
    ↓ [parse + enrich CNPJ/denom from cnpj_map.yaml]
DataFrame (filtrado por ccvm)
    ↓ [pre_filter: keywords, category rules]
Processados ↓
    ├─ PDF download + extract text
    ├─ Deepseek LLM análise
    ├─ Telegram notificação
    └─ Dedup hash → ipe_processed_ids.json
```

## Arquitetura

### Funções Principais (`ipe_watcher.py`)

| Função | Responsabilidade | Customizável |
|--------|------------------|--------------|
| `download_ipe_b3()` | Query B3 API, parse XML. Trata erros pelo `NUMERO_DO_ERRO` (ver Troubleshooting) | Não (precisa de credenciais) |
| `load_company_map()` | Ler ccvm/CNPJ/denom do config | Sim (YAML) |
| `pre_filter()` | Regras de skip (AGO/AGE, dividendos) | Sim (adicione keywords) |
| `make_hash()` | SHA256 de CNPJ\|DT\|TIPO\|LINK | Não |
| `download_pdf()` | HTTP GET c/ retry | Não |
| `extract_text()` | PDF → text via pdfplumber | Não |
| `call_llm()` | Deepseek prompt + parsing | Sim (system_prompt_ipe.txt) |
| `send_notification()` | Telegram format + send | Sim (editar função) |
| `main()` | Orquestração | Não (toca em tudo) |

### Deps Externas

- **pandas**: Data manipulation
- **pdfplumber**: PDF text extraction
- **openai**: Deepseek API (via OpenAI SDK)
- **requests**: HTTP (sessão com retry)
- **pyyaml**: Config parsing
- **xml.etree**: XML parsing (stdlib)

### Negociações de insiders (VLMO)

Dois avisos, com papéis diferentes:

| Aviso | Fonte | Quando | Conteúdo |
|---|---|---|---|
| Diário (`ipe_watcher.py`) | PDF do formulário VLMO via B3 | a cada execução, quando chega formulário novo | todas as negociações da empresa da cobertura |
| Semanal (`br_stock_trading.py`, `daily.yml`) | CSV consolidado da CVM (dados abertos) | segunda 09h BRT | só destaques P1/P2 da cobertura, por empresa |

Regras comuns (`alerts.py`):
- Só o **último mês divulgado** (`latest_disclosed_month`: o mês anterior a partir do dia 11; antes disso, o mês retrasado). Meses anteriores nunca são avisados.
- Linhas agregadas por empresa, cargo e sentido: `• Controlador comprou X ações, valor Y, preço médio Z`.
- P1: Controlador / Conselho de Administração / Diretor com linha > R$ 500 mil. P2: total negociado pela empresa no mês > R$ 2 mi. Não há P3.
- O semanal envia cada empresa uma vez por mês de referência (estado em `datasets/vlmo_summary_state.json`) e de novo só se os números mudarem, marcada como "atualizado".
- Recompra: o CSV da CVM só traz insiders; recompra pela companhia aparece apenas quando a descrição do movimento cita "recompra". O aviso diário (PDF) traz a seção de tesouraria.
- Envio pelo Telegram em blocos por empresa, 1,5 s entre mensagens, respeitando `retry_after` em HTTP 429.

## Customizações Comuns

### 1. Adicionar Filtro Extra no pre_filter()

**Caso:** Excluir documentos com palavra-chave específica.

```python
# ipe_watcher.py, função pre_filter(), adicione antes de `return True, ""`

if "word-to-skip" in al:  # al = assunto.lower()
    return False, "razão do skip"
```

**Teste:**
```bash
python3 -m pytest tests/test_ipe_watcher.py::test_skip_XXX -v
```

### 2. Customizar Prompt LLM

**Arquivo:** `config/system_prompt_ipe.txt`

Edite a instrução do sistema. O prompt recebe:
- `{doc_type}`: Fato Relevante, Comunicado, etc.
- `{company}`: AZUL S.A., GOL, etc.
- `{date_str}`: 2026-04-23
- `{text}`: Texto extraído do PDF

### 3. Adicionar Notificação Extra (Slack, Discord)

**Onde:** `ipe_watcher.py`, função `send_notification()` (linha ~268)

```python
def send_notification(row, summary):
    # ... existing Telegram code ...
    
    # Adicione:
    if summary:
        send_to_slack(row, summary)  # sua função aqui
```

### 4. Filtrar por Setor

**Caso:** Monitorar só healthcare.

```yaml
# config/cnpj_map.yaml — delete todos os tickers exceto:
tickers:
  RDOR3: {cnpj: "06.047.087/0001-39", ccvm: "24821", denom: "REDE D'OR SÃO LUIZ S.A."}
  HAPV3: {cnpj: "05.197.443/0001-38", ccvm: "24392", denom: "HAPVIDA..."}
  HYPE3: {cnpj: "02.932.074/0001-91", ccvm: "21431", denom: "HYPERA S/A"}
  # ... mais healthcare ...
```

### 5. Mudar Horário de Execução

**Arquivo:** `.github/workflows/ipe_hourly.yml`

```yaml
schedule:
  - cron: '0 9,12,15,18,21 * * 1-5'  # 06h, 09h, 12h, 15h, 18h BRT, seg-sex
  - cron: '59 23 * * 1-5'            # 20h59 BRT, seg-sex
```

O GitHub atrasa execuções agendadas (às vezes horas). Não há perda: cada
execução reconsulta desde `ipe_last_run_date.txt` (inclusive).

Converter: [crontab.guru](https://crontab.guru)

## Estrutura de Dados

### XML da B3 (entrada)

```xml
<?xml version="1.0" encoding="ISO-8859-1" ?>
<DownloadMultiplo DataSolicitada="dd/mm/yyyy hh:mm" TipoDocumento="IPE">
  <Link url="http://..."
        ccvm="24392"
        DataRef="23/04/2026 09:00:00"
        Categoria="Fato Relevante"
        Tipo="Fato Relevante"
        Especie="Acordo operacional"
        Situacao="Liberado" />
</DownloadMultiplo>
```

### DataFrame (processado)

| Coluna | Fonte | Tipo |
|--------|-------|------|
| ccvm | XML `@ccvm` | str |
| DT_REF | XML `@DataRef` — data de **referência** do documento, não de entrega (converter para ISO) | str (YYYY-MM-DD) |
| CATEG_DOC | XML `@Categoria` | str |
| TIPO_DOC | XML `@Tipo` | str |
| LINK_DOC | XML `@url` | str (direct PDF URL) |
| ASSUNTO | XML `@Especie` | str |
| CNPJ_CIA | lookup ccvm em cnpj_map.yaml | str (XX.XXX.XXX/XXXX-XX) |
| DENOM_CIA | lookup ccvm em cnpj_map.yaml | str |
| _cnpj_digits | regex remove \D | str (digits only) |

## Testes

### Rodar Todos

```bash
python3 -m pytest tests/test_ipe_watcher.py -v
```

### Teste Específico

```bash
python3 -m pytest tests/test_ipe_watcher.py::test_force_keyword_aquisicao -v
```

### Mock da B3 API

Veja `tests/test_ipe_watcher.py:SAMPLE_B3_XML` para exemplo de resposta.

## GitHub Actions

### Workflow: `ipe_hourly.yml`

| Step | O Que Faz |
|------|-----------|
| checkout | Baixa código |
| cache/restore | Restaura `ipe_processed_ids.json` |
| setup-python | Python 3.11 |
| pip install | Deps |
| run ipe_watcher.py | Executa script |
| cache/save | Salva dedup |

### Secrets Necessários

- `CVM_USERNAME`: Login CVM
- `CVM_PASSWORD`: Senha CVM
- `TELEGRAM_BOT_TOKEN`: Bot Telegram
- `TELEGRAM_CHAT_ID`: Chat ID
- `DEEPSEEK_API_KEY`: Deepseek token

### Debug

GitHub Actions → Actions → IPE Hourly Watcher → [seu run] → Run IPE watcher

Logs mostram:
```
INFO Querying B3 API for IPE filings on 23/04/2026
INFO B3 API returned 561 IPE records for 23/04/2026
INFO 1 rows after category+ccvm filter
INFO 1 rows for today (2026-04-23)
INFO HTTP Request: POST https://api.deepseek.com/chat/completions "HTTP/1.1 200 OK"
INFO Done. Processed 1 new documents.
```

## Troubleshooting

### "CVM_USERNAME and CVM_PASSWORD environment variables must be set"

Credenciais não passadas. Verifique GitHub Secrets.

### Erros da API B3 (Nota Técnica do Download Múltiplo, §4.2)

| `NUMERO_DO_ERRO` | Significado | Tratamento |
|---|---|---|
| `1` | LOGIN INCORRETO (na prática também SENHA EXPIRADA, USUÁRIO SUSPENSO) | `CredentialError` → alerta Telegram + `exit(1)`, sem retry |
| `22013` | Erro ao gerar o XML | retry (3 tentativas) |
| `22014` / `22015` / `22017` | Data / hora / tipo de documento inválido | `B3RequestError` → alerta + `exit(1)` (bug no script) |
| `22016` | Nenhum registro localizado | DataFrame vazio (normal em fim de semana/feriado) |
| outro | — | `RuntimeError`; a data fica pendente e o run termina com `exit(1)` |

Semântica (§4.3): a API devolve o link na data da **última ação** sobre o
documento (liberação, bloqueio, cancelamento), numa janela de até 24h a partir
de `txtData`/`txtHora`.

### Senha expirada / login incorreto (erro 1)

O watcher **para imediatamente** com `sys.exit(1)` e envia alerta Telegram.

Para trocar a senha:
1. [cas.bovespa.com.br/cas](https://cas.bovespa.com.br/cas/) → menu "Senhas", **ou**
   [rad.cvm.gov.br/ENET](https://www.rad.cvm.gov.br/ENET) → "Alteração de senha"
2. Atualize o GitHub Secret `CVM_PASSWORD`
3. Conta suspensa: `suporteexterno@cvm.gov.br` | `0800-944-3535`

> ⚠️ Os sistemas CVM ficam offline para manutenção periodicamente (verificar
> aviso em gov.br/cvm). Durante a janela de manutenção, aguardar normalização
> antes de contatar o suporte.

### "0 rows after category+ccvm filter"

Nenhum documento encontrado para suas empresas no dia. Normal (market closes, feriado).

### Dedup não funciona

`ipe_processed_ids.json` não foi salvo pelo cache. Verifique `cache/save` step no Actions.

## Performance

- B3 API query: ~10s
- Parse + enrich: <1s
- Filtro + pre_filter: <1s
- LLM (Deepseek): ~4s
- Total: ~15-20s

## Segurança

- ✅ Secrets em GitHub Secrets, não no código
- ✅ `.gitignore` bloqueia `.env`
- ✅ Dedup via hash (não reprocessa)
- ✅ Cache privado (GitHub Actions)
- ✅ XML parseado seguramente (ElementTree)

## Próximos Passos (Ideias)

- [ ] Webhook HTTP em vez de Telegram
- [ ] Banco de dados (SQLite/Postgres) em vez de CSV
- [ ] Dashboard HTML (histórico de FRs)
- [ ] Notificação por email
- [ ] Análise de sentimento do LLM
- [ ] Rate limiting (honrar limite de Deepseek)
- [ ] Integração com seu próprio LLM local

---

**Precisa expandir algo?** Abra uma issue ou edite este arquivo.
