# CVM Alerts — Monitoramento de Fatos Relevantes em Tempo Real

Monitor Fatos Relevantes (FRs) e Comunicados ao Mercado de empresas brasileiras via API da B3/CVM. Recebe alertas via Telegram com análise automática via LLM.

## 🚀 Início Rápido

### 1. Clonar e Configurar

```bash
git clone https://github.com/lbarretos/cvm-alerts.git
cd cvm-alerts
pip install -r requirements.txt
```

### 2. Adicionar Credenciais (GitHub Secrets)

Vá para **Settings → Secrets and variables → Actions** e adicione:

| Secret | Valor | Onde Obter |
|--------|-------|-----------|
| `CVM_USERNAME` | Login CVM | Solicite em [conteudo.cvm.gov.br](https://conteudo.cvm.gov.br/menu/regulados/companhias/download_multiplo/) |
| `CVM_PASSWORD` | Senha CVM | Mesmo portal — a senha expira periodicamente; veja **Renovação de Senha** abaixo |
| `TELEGRAM_BOT_TOKEN` | Token do bot Telegram | `@BotFather` no Telegram |
| `TELEGRAM_CHAT_ID` | ID do chat | `@userinfobot` no Telegram |
| `DEEPSEEK_API_KEY` | API key Deepseek | [platform.deepseek.com](https://platform.deepseek.com) |

### 3. Customizar Empresas Monitoradas

Edite `config/cnpj_map.yaml` — apenas as empresas listadas geram alertas.

**Exemplo:** para monitorar só AZUL4 e GOLL4:

```yaml
tickers:
  AZUL4: {cnpj: "09.305.994/0001-29", ccvm: "24112", denom: "AZUL S.A."}
  GOLL4: {cnpj: "06.164.253/0001-87", ccvm: "19569", denom: "GOL LINHAS AEREAS INTELIGENTES SA"}
```

### 4. Disparar Workflow

Vá para **Actions → IPE Hourly Watcher → Run workflow**.

Ou espere os horários automáticos:
- **06h00, 09h00, 12h00, 18h00, 20h59 BRT** (seg–sex)

## 📊 O Que Você Recebe

- **Fato Relevante (FR)**: Sempre alerta
- **Comunicado ao Mercado**: Sempre alerta
- **Resumo automático**: LLM analisa e extrai pontos-chave
- **Link do PDF**: Documento original da CVM
- **Telegram**: Notificação em tempo real

## 🔧 Estrutura

```
.
├── ipe_watcher.py              # Script principal
├── config/
│   ├── cnpj_map.yaml          # Empresas monitoradas (customize aqui)
│   └── system_prompt_ipe.txt   # Prompt do LLM (customize se quiser)
├── datasets/
│   ├── ipe_processed_ids.json  # Cache de dedup (GitHub Actions)
│   └── ipe_skipped_log.csv     # Log de docs excluídos
└── .github/workflows/
    └── ipe_hourly.yml         # Agendamento automático
```

## 🛠️ Customizações

### Mudar Intervalo de Execução

Edite `.github/workflows/ipe_hourly.yml`:

```yaml
schedule:
  - cron: '0 9,15 * * 1-5'  # 09h e 15h UTC, seg-sex
```

Veja [crontab.guru](https://crontab.guru) para converter.

### Mudar Critério de Filtro

Edite `config/system_prompt_ipe.txt` para ajustar o que o LLM considera importante.

### Adicionar Notificação Extra

Edite `ipe_watcher.py` linha ~268 (`send_notification`) para adicionar Slack, Discord, etc.

## ⚠️ Requisitos

- **Python 3.11+**
- **Conta CVM** com acesso à API Multiple Download
- **Telegram Bot** (criar com `@BotFather`)
- **Deepseek API** (free tier funciona)

## 🔑 Renovação de Senha CVM

A senha B3/CVM expira periodicamente. Quando isso acontece:

1. O workflow fica **vermelho** no GitHub Actions (exit code 1)
2. Você recebe alerta Telegram: _"🚨 CVM-Alerts parou: credencial B3 inválida"_

**Para renovar:**
- E-mail: `suporteexterno@cvm.gov.br`
- Telefone: `0800-944-3535`

Após receber a nova senha, atualize o Secret `CVM_PASSWORD` em **Settings → Secrets**.

> **Nota:** os sistemas da CVM ficam offline para manutenção alguns dias por ano.
> Verifique avisos em [gov.br/cvm](https://www.gov.br/cvm/pt-br) antes de acionar o suporte.

## 📝 Logs e Debugging

Veja os logs no GitHub Actions:
1. **Actions → IPE Hourly Watcher → seu run**
2. **Run IPE watcher** → expanda para ver output

Localmente:
```bash
CVM_USERNAME=seu_login CVM_PASSWORD=sua_senha python ipe_watcher.py
```

## 🔒 Segurança

- ✅ Credenciais em secrets, não no repo
- ✅ Dedup automático (não reprocessa documentos)
- ✅ Cache privado via GitHub Actions
- ✅ Repo público é seguro (secrets mascarados nos logs)

## 📚 Documentação

Veja `CLAUDE.md` para ajuda de desenvolvimento e customizações avançadas.

---

**Precisa ajuda?** Abra uma issue no GitHub.
