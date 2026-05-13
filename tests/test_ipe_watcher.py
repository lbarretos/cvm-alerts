import io
import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest


SAMPLE_B3_XML = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<DownloadMultiplo DataSolicitada="22/04/2026 00:00" TipoDocumento="IPE" DataConsulta="22/04/2026 10:00">
  <Link url="http://example.com/doc1.pdf"
        Documento="IPE" ccvm="24392"
        DataRef="22/04/2026 09:00:00" FrmDtRef="22/04/2026 09:00"
        Categoria="Fato Relevante" Tipo="Fato Relevante"
        Especie="Acordo operacional" Situacao="Liberado" />
  <Link url="http://example.com/doc2.pdf"
        Documento="IPE" ccvm="99999"
        DataRef="22/04/2026 09:00:00" FrmDtRef="22/04/2026 09:00"
        Categoria="Fato Relevante" Tipo="Fato Relevante"
        Especie="Outro fato" Situacao="Liberado" />
</DownloadMultiplo>
"""


# ── Task 6: download_ipe_b3 ───────────────────────────────────────────────────

def test_download_ipe_b3_returns_dataframe(monkeypatch):
    import ipe_watcher
    monkeypatch.setenv("CVM_USERNAME", "testuser")
    monkeypatch.setenv("CVM_PASSWORD", "testpass")

    mock_resp = MagicMock()
    mock_resp.content = SAMPLE_B3_XML
    mock_resp.raise_for_status = MagicMock()
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp

    df = ipe_watcher.download_ipe_b3(mock_session, date(2026, 4, 22))

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert "ccvm" in df.columns
    assert "CATEG_DOC" in df.columns
    assert "LINK_DOC" in df.columns
    assert df.iloc[0]["DT_ENTREGA"] == "2026-04-22"


def test_download_ipe_b3_raises_without_credentials(monkeypatch):
    import ipe_watcher
    monkeypatch.delenv("CVM_USERNAME", raising=False)
    monkeypatch.delenv("CVM_PASSWORD", raising=False)

    with pytest.raises(EnvironmentError, match="CVM_USERNAME"):
        ipe_watcher.download_ipe_b3(MagicMock(), date(2026, 4, 22))


def test_download_ipe_b3_raises_on_api_error(monkeypatch):
    import ipe_watcher
    monkeypatch.setenv("CVM_USERNAME", "testuser")
    monkeypatch.setenv("CVM_PASSWORD", "testpass")

    error_xml = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<ERROS DataSolicitada="22/04/2026 00:00" TipoDocumento="IPE" DataConsulta="22/04/2026 10:00">
  <NUMERO_DO_ERRO>1</NUMERO_DO_ERRO>
  <DESCRICAO_DO_ERRO>LOGIN INCORRETO</DESCRICAO_DO_ERRO>
  <FONTE_DO_ERRO>Autenticacao</FONTE_DO_ERRO>
</ERROS>"""
    mock_resp = MagicMock()
    mock_resp.content = error_xml
    mock_resp.raise_for_status = MagicMock()
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp

    with pytest.raises(RuntimeError, match="LOGIN INCORRETO"):
        ipe_watcher.download_ipe_b3(mock_session, date(2026, 4, 22))


# ── Task 7: pre_filter ────────────────────────────────────────────────────────

import ipe_watcher


def _row(categ: str, assunto: str) -> pd.Series:
    return pd.Series({
        "CATEG_DOC": categ,
        "TIPO_DOC": categ,
        "ASSUNTO": assunto,
        "DENOM_CIA": "TEST CO",
        "DT_ENTREGA": "2026-04-22",
    })


def test_fato_relevante_always_processed():
    ok, reason = ipe_watcher.pre_filter(_row("Fato Relevante", "Resultado trimestral"))
    assert ok is True
    assert reason == ""


def test_comunicado_ao_mercado_always_processed():
    ok, _ = ipe_watcher.pre_filter(_row("Comunicado ao Mercado", "Novo contrato"))
    assert ok is True


def test_force_keyword_opa_overrides_skip():
    ok, _ = ipe_watcher.pre_filter(_row("Aviso aos Acionistas", "OPA pelo controlador"))
    assert ok is True


def test_force_keyword_aquisicao():
    ok, _ = ipe_watcher.pre_filter(
        _row("Comunicado aos Acionistas", "Aquisição de participação")
    )
    assert ok is True


def test_skip_pagamento_jcp():
    ok, reason = ipe_watcher.pre_filter(
        _row("Aviso aos Acionistas", "Pagamento de JCP - 1T26")
    )
    assert ok is False
    assert "pagamento/crédito" in reason.lower()


def test_skip_credito_dividendo():
    ok, reason = ipe_watcher.pre_filter(
        _row("Aviso aos Acionistas", "Crédito de dividendos aos acionistas")
    )
    assert ok is False


def test_skip_calendario_eventos():
    ok, reason = ipe_watcher.pre_filter(
        _row("Aviso aos Acionistas", "Calendário de Eventos Corporativos 2026")
    )
    assert ok is False
    assert "calendário" in reason.lower()


def test_skip_politica_divulgacao():
    ok, reason = ipe_watcher.pre_filter(
        _row("Comunicado aos Acionistas", "Política de Divulgação de Ato ou Fato Relevante")
    )
    assert ok is False


def test_skip_codigo_conduta():
    ok, reason = ipe_watcher.pre_filter(
        _row("Comunicado aos Acionistas", "Código de Conduta e Ética 2026")
    )
    assert ok is False


def test_skip_estatuto_sem_capital():
    ok, reason = ipe_watcher.pre_filter(
        _row("Comunicado aos Acionistas", "Estatuto Social atualizado")
    )
    assert ok is False
    assert "estatuto" in reason.lower()


def test_estatuto_com_capital_processado():
    ok, _ = ipe_watcher.pre_filter(
        _row("Comunicado aos Acionistas", "Estatuto Social — alteração de capital autorizado")
    )
    assert ok is True


def test_skip_ata_ago():
    ok, reason = ipe_watcher.pre_filter(
        _row("Comunicado aos Acionistas", "Ata da AGO realizada em 22/04/2026")
    )
    assert ok is False
    assert "ata" in reason.lower()


def test_ata_age_with_force_keyword():
    ok, _ = ipe_watcher.pre_filter(
        _row("Comunicado aos Acionistas", "Ata AGE — aprovação de incorporação")
    )
    assert ok is True


# ── Task 8: dedup + PDF download ──────────────────────────────────────────────

def test_make_hash_deterministic():
    row = pd.Series({
        "CNPJ_CIA": "00.494.205/0001-31",
        "DT_ENTREGA": "2026-04-22",
        "TIPO_DOC": "Fato Relevante",
        "LINK_DOC": "http://example.com/doc.pdf",
    })
    h1 = ipe_watcher.make_hash(row)
    h2 = ipe_watcher.make_hash(row)
    assert h1 == h2
    assert len(h1) == 64


def test_make_hash_differs_by_link():
    row1 = pd.Series({"CNPJ_CIA": "X", "DT_ENTREGA": "Y", "TIPO_DOC": "Z", "LINK_DOC": "A"})
    row2 = pd.Series({"CNPJ_CIA": "X", "DT_ENTREGA": "Y", "TIPO_DOC": "Z", "LINK_DOC": "B"})
    assert ipe_watcher.make_hash(row1) != ipe_watcher.make_hash(row2)


def test_load_processed_ids_empty_if_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ipe_watcher, "PROCESSED_IDS_FILE", tmp_path / "ids.json")
    ids = ipe_watcher.load_processed_ids()
    assert isinstance(ids, set)
    assert len(ids) == 0


def test_save_and_reload_processed_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(ipe_watcher, "PROCESSED_IDS_FILE", tmp_path / "ids.json")
    ids = {"abc123", "def456"}
    ipe_watcher.save_processed_ids(ids)
    reloaded = ipe_watcher.load_processed_ids()
    assert reloaded == ids


def test_download_pdf_returns_bytes_on_success():
    mock_resp = MagicMock()
    mock_resp.content = b"%PDF-1.4 fake"
    mock_resp.raise_for_status = MagicMock()
    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    result = ipe_watcher.download_pdf(mock_session, "http://example.com/doc.pdf")
    assert result == b"%PDF-1.4 fake"


def test_download_pdf_returns_none_after_two_failures():
    mock_session = MagicMock()
    mock_session.get.side_effect = Exception("connection error")

    result = ipe_watcher.download_pdf(mock_session, "http://example.com/doc.pdf")
    assert result is None
    assert mock_session.get.call_count == 2


# ── Task 9: LLM + notification ────────────────────────────────────────────────

def test_call_llm_returns_string_on_success(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    fake_choice = MagicMock()
    fake_choice.message.content = "- Fato: X\n- Impacto: Y\n- Risco: Z"
    fake_completion = MagicMock()
    fake_completion.choices = [fake_choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = fake_completion

    with patch("ipe_watcher.OpenAI", return_value=mock_client):
        result = ipe_watcher.call_llm("Fato Relevante", "HAPVIDA", "2026-04-22", "texto")

    assert "Fato" in result


def test_call_llm_returns_none_on_exception():
    with patch("ipe_watcher.OpenAI", side_effect=Exception("API error")):
        result = ipe_watcher.call_llm("Fato Relevante", "HAPVIDA", "2026-04-22", "texto")
    assert result is None


def test_send_notification_formats_message():
    row = pd.Series({
        "CATEG_DOC": "Fato Relevante",
        "DENOM_CIA": "HAPVIDA",
        "DT_ENTREGA": "2026-04-22",
        "LINK_DOC": "http://example.com/doc.pdf",
    })
    with patch("ipe_watcher.send_message") as mock_send:
        ipe_watcher.send_notification(row, "- Fato: X\n- Impacto: Y")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "HAPVIDA" in msg
        assert "Fato Relevante" in msg
        assert "http://example.com/doc.pdf" in msg
        assert "Fato: X" in msg


# ── Task 11: integration smoke test ──────────────────────────────────────────

def test_main_processes_one_new_document(tmp_path, monkeypatch):
    """Integration: main() finds 1 new doc, calls LLM, sends notification."""
    monkeypatch.setattr(ipe_watcher, "PROCESSED_IDS_FILE",  tmp_path / "ids.json")
    monkeypatch.setattr(ipe_watcher, "SKIPPED_LOG_FILE",    tmp_path / "skipped.csv")
    monkeypatch.setattr(ipe_watcher, "INDEX_LATEST_FILE",   tmp_path / "latest.csv")
    monkeypatch.setattr(ipe_watcher, "LAST_RUN_DATE_FILE",  tmp_path / "last_run_date.txt")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("CVM_USERNAME", "fake-login")
    monkeypatch.setenv("CVM_PASSWORD", "fake-senha")

    today = date.today().isoformat()

    fake_company_map = {
        "24392": {
            "ticker": "HAPV3",
            "cnpj": "05.197.443/0001-38",
            "cnpj_digits": "05197443000138",
            "denom": "HAPVIDA PARTICIPAÇÕES E INVESTIMENTOS S.A.",
        }
    }

    fake_df = pd.DataFrame([{
        "ccvm":       "24392",
        "DT_ENTREGA": today,
        "CATEG_DOC":  "Fato Relevante",
        "TIPO_DOC":   "Fato Relevante",
        "LINK_DOC":   "http://example.com/doc.pdf",
        "ASSUNTO":    "Acordo operacional",
        "Situacao":   "Liberado",
    }])

    with (
        patch("ipe_watcher.download_ipe_b3", return_value=fake_df),
        patch("ipe_watcher.load_company_map", return_value=fake_company_map),
        patch("ipe_watcher.download_pdf", return_value=b"%PDF-fake"),
        patch("ipe_watcher.extract_text", return_value="Texto do documento"),
        patch("ipe_watcher.call_llm", return_value="- Fato: X\n- Impacto: Y\n- Risco: Z"),
        patch("ipe_watcher.send_notification") as mock_notify,
        patch("ipe_watcher.git_commit_back"),
        patch("ipe_watcher.create_session", return_value=MagicMock()),
    ):
        ipe_watcher.main()

    mock_notify.assert_called_once()
    assert ipe_watcher.PROCESSED_IDS_FILE.exists()
    ids = ipe_watcher.load_processed_ids()
    assert len(ids) == 1


# ── last_run_date state helpers ───────────────────────────────────────────────

def test_load_last_run_date_returns_yesterday_when_missing(tmp_path, monkeypatch):
    import ipe_watcher
    monkeypatch.setattr(ipe_watcher, "LAST_RUN_DATE_FILE", tmp_path / "last_run_date.txt")
    result = ipe_watcher.load_last_run_date()
    assert result == date.today() - timedelta(days=1)


def test_load_last_run_date_reads_existing_file(tmp_path, monkeypatch):
    import ipe_watcher
    state_file = tmp_path / "last_run_date.txt"
    state_file.write_text("2026-04-18")  # a Friday
    monkeypatch.setattr(ipe_watcher, "LAST_RUN_DATE_FILE", state_file)
    assert ipe_watcher.load_last_run_date() == date(2026, 4, 18)


def test_save_last_run_date_writes_iso_string(tmp_path, monkeypatch):
    import ipe_watcher
    state_file = tmp_path / "last_run_date.txt"
    monkeypatch.setattr(ipe_watcher, "LAST_RUN_DATE_FILE", state_file)
    ipe_watcher.save_last_run_date(date(2026, 4, 21))
    assert state_file.read_text() == "2026-04-21"


# ── query_dates window logic ──────────────────────────────────────────────────

def test_main_queries_weekend_dates_on_monday(tmp_path, monkeypatch):
    """On Monday (2026-04-20), last run was Friday (2026-04-17): must query Sat, Sun, Mon."""
    import ipe_watcher

    monday   = date(2026, 4, 20)
    friday   = date(2026, 4, 17)
    saturday = date(2026, 4, 18)
    sunday   = date(2026, 4, 19)

    state_file = tmp_path / "last_run_date.txt"
    state_file.write_text(friday.isoformat())

    monkeypatch.setattr(ipe_watcher, "PROCESSED_IDS_FILE",  tmp_path / "ids.json")
    monkeypatch.setattr(ipe_watcher, "SKIPPED_LOG_FILE",    tmp_path / "skipped.csv")
    monkeypatch.setattr(ipe_watcher, "INDEX_LATEST_FILE",   tmp_path / "latest.csv")
    monkeypatch.setattr(ipe_watcher, "LAST_RUN_DATE_FILE",  state_file)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("CVM_USERNAME", "fake-login")
    monkeypatch.setenv("CVM_PASSWORD", "fake-senha")

    empty_df = pd.DataFrame(columns=["ccvm", "DT_ENTREGA", "CATEG_DOC",
                                      "TIPO_DOC", "LINK_DOC", "ASSUNTO", "Situacao"])

    queried_dates = []

    def fake_download(session, qd):
        queried_dates.append(qd)
        return empty_df.copy()

    with (
        patch("ipe_watcher.download_ipe_b3", side_effect=fake_download),
        patch("ipe_watcher.load_company_map", return_value={}),
        patch("ipe_watcher.git_commit_back"),
        patch("ipe_watcher.create_session", return_value=MagicMock()),
        patch("ipe_watcher.datetime") as mock_dt,
    ):
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value.date.return_value = monday
        mock_dt.utcnow.return_value.strftime.return_value = "2026-04-20 09:00 UTC"
        ipe_watcher.main()

    assert queried_dates == [saturday, sunday, monday]


def test_main_queries_only_today_on_intraday_rerun(tmp_path, monkeypatch):
    """When last_run_date == today, query only today (intra-day re-run)."""
    import ipe_watcher

    today = date(2026, 4, 21)
    state_file = tmp_path / "last_run_date.txt"
    state_file.write_text(today.isoformat())

    monkeypatch.setattr(ipe_watcher, "PROCESSED_IDS_FILE",  tmp_path / "ids.json")
    monkeypatch.setattr(ipe_watcher, "SKIPPED_LOG_FILE",    tmp_path / "skipped.csv")
    monkeypatch.setattr(ipe_watcher, "INDEX_LATEST_FILE",   tmp_path / "latest.csv")
    monkeypatch.setattr(ipe_watcher, "LAST_RUN_DATE_FILE",  state_file)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("CVM_USERNAME", "fake-login")
    monkeypatch.setenv("CVM_PASSWORD", "fake-senha")

    empty_df = pd.DataFrame(columns=["ccvm", "DT_ENTREGA", "CATEG_DOC",
                                      "TIPO_DOC", "LINK_DOC", "ASSUNTO", "Situacao"])
    queried_dates = []

    def fake_download(session, qd):
        queried_dates.append(qd)
        return empty_df.copy()

    with (
        patch("ipe_watcher.download_ipe_b3", side_effect=fake_download),
        patch("ipe_watcher.load_company_map", return_value={}),
        patch("ipe_watcher.git_commit_back"),
        patch("ipe_watcher.create_session", return_value=MagicMock()),
        patch("ipe_watcher.datetime") as mock_dt,
    ):
        mock_dt.now.return_value.date.return_value = today
        mock_dt.utcnow.return_value.strftime.return_value = "2026-04-21 12:00 UTC"
        ipe_watcher.main()

    assert queried_dates == [today]


# ── VLMO: _parse_brl_number ──────────────────────────────────────────────────

def test_parse_brl_number_integer():
    assert ipe_watcher._parse_brl_number("64.800") == 64800.0


def test_parse_brl_number_decimal():
    import pytest
    assert ipe_watcher._parse_brl_number("22,85160") == pytest.approx(22.8516)


def test_parse_brl_number_full():
    import pytest
    assert ipe_watcher._parse_brl_number("1.480.783,68") == pytest.approx(1480783.68)


def test_parse_brl_number_no_thousands():
    assert ipe_watcher._parse_brl_number("50,00") == 50.0


# ── VLMO: _parse_vlmo_page ───────────────────────────────────────────────────

SAMPLE_VLMO_PAGE = """\
Formulário de Posição Consolidada
Nome do Controlador/Administrador/Acionista:
JOAO DA SILVA SANTOS

Denominação da Companhia: TEST EMPRESA S.A.

( ) Controlador ou Vinculado
( X ) Diretor ou Vinculado
( ) Conselho de Administração ou Vinculado
( ) Conselho Fiscal ou Vinculado

Quadro 2 – Negociações de Valores Mobiliários

Compra à
Ações ON TEST 5 1.000 50,00 50.000,00
vista
Venda à
Ações PN TEST 15 2.500 45,50 113.750,00
vista
"""


def test_parse_vlmo_page_extracts_two_transactions():
    import pytest
    txns = ipe_watcher._parse_vlmo_page(SAMPLE_VLMO_PAGE)
    assert len(txns) == 2


def test_parse_vlmo_page_extracts_position_type():
    txns = ipe_watcher._parse_vlmo_page(SAMPLE_VLMO_PAGE)
    assert txns[0]["position_type"] == "Diretor ou Vinculado"
    assert txns[1]["position_type"] == "Diretor ou Vinculado"


def test_parse_vlmo_page_extracts_operation_types():
    txns = ipe_watcher._parse_vlmo_page(SAMPLE_VLMO_PAGE)
    assert "Compra" in txns[0]["operation_type"]
    assert "Venda" in txns[1]["operation_type"]


def test_parse_vlmo_page_extracts_numeric_fields():
    import pytest
    txns = ipe_watcher._parse_vlmo_page(SAMPLE_VLMO_PAGE)
    assert txns[0]["quantity"] == 1000.0
    assert txns[0]["volume"] == pytest.approx(50000.0)
    assert txns[1]["quantity"] == 2500.0
    assert txns[1]["volume"] == pytest.approx(113750.0)


def test_parse_vlmo_page_empty_text_returns_empty():
    txns = ipe_watcher._parse_vlmo_page("")
    assert txns == []


def test_parse_vlmo_page_no_transactions_returns_empty():
    txns = ipe_watcher._parse_vlmo_page("Denominação da Companhia: EMPRESA\n( X ) Diretor ou Vinculado\n")
    assert txns == []


# ── VLMO: parse_vlmo_pdf ─────────────────────────────────────────────────────

def test_parse_vlmo_pdf_calls_pdfplumber():
    mock_page = MagicMock()
    mock_page.extract_text.return_value = SAMPLE_VLMO_PAGE
    mock_pdf_ctx = MagicMock()
    mock_pdf_ctx.__enter__ = MagicMock(return_value=mock_pdf_ctx)
    mock_pdf_ctx.__exit__ = MagicMock(return_value=False)
    mock_pdf_ctx.pages = [mock_page]

    with patch("ipe_watcher.pdfplumber.open", return_value=mock_pdf_ctx):
        result = ipe_watcher.parse_vlmo_pdf(b"%PDF-fake")

    assert len(result) == 2


def test_parse_vlmo_pdf_returns_empty_on_exception():
    with patch("ipe_watcher.pdfplumber.open", side_effect=Exception("bad PDF")):
        result = ipe_watcher.parse_vlmo_pdf(b"not a pdf")
    assert result == []


# ── VLMO: send_vlmo_batch_notification ───────────────────────────────────────

def test_send_vlmo_batch_with_transactions():
    vlmo_by_company = {
        "24392": {
            "ticker":       "TEST3",
            "denom":        "TEST CO",
            "ref_month":    "2026-04",
            "primary_link": "http://example.com/vlmo.pdf",
            "transactions": [{
                "position_type": "Diretor ou Vinculado",
                "operation_type": "Compra à vista",
                "asset":          "Ações ON",
                "volume":         50000.0,
            }],
        }
    }
    with patch("ipe_watcher.send_message") as mock_send:
        ipe_watcher.send_vlmo_batch_notification(vlmo_by_company)
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "TEST CO" in msg
        assert "Diretor" in msg
        assert "Compra" in msg
        assert "50K" in msg


def test_send_vlmo_batch_empty_transactions():
    vlmo_by_company = {
        "24392": {
            "ticker":       "TEST3",
            "denom":        "TEST CO",
            "ref_month":    "2026-04",
            "primary_link": "http://example.com/vlmo.pdf",
            "transactions": [],
        }
    }
    with patch("ipe_watcher.send_message") as mock_send:
        ipe_watcher.send_vlmo_batch_notification(vlmo_by_company)
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "TEST CO" in msg
        assert "sem transações" in msg.lower()


def test_send_vlmo_batch_aggregates_same_type():
    txns = [
        {"position_type": "Recompra", "operation_type": "Compra", "volume": 500_000.0},
        {"position_type": "Recompra", "operation_type": "Compra", "volume": 700_000.0},
        {"position_type": "Recompra", "operation_type": "Compra", "volume": 300_000.0},
    ]
    vlmo_by_company = {
        "24392": {
            "ticker": "CO3", "denom": "CO", "ref_month": "2026-04",
            "primary_link": "http://x", "transactions": txns,
        }
    }
    with patch("ipe_watcher.send_message") as mock_send:
        ipe_watcher.send_vlmo_batch_notification(vlmo_by_company)
        msg = mock_send.call_args[0][0]
        assert "(3x)" in msg
        assert "1.5M" in msg


# ── VLMO: integration in main() ──────────────────────────────────────────────

def test_main_processes_vlmo_document(tmp_path, monkeypatch):
    """main() routes VLMO to parse_vlmo_pdf + send_vlmo_batch_notification, skips LLM."""
    monkeypatch.setattr(ipe_watcher, "PROCESSED_IDS_FILE",  tmp_path / "ids.json")
    monkeypatch.setattr(ipe_watcher, "SKIPPED_LOG_FILE",    tmp_path / "skipped.csv")
    monkeypatch.setattr(ipe_watcher, "INDEX_LATEST_FILE",   tmp_path / "latest.csv")
    monkeypatch.setattr(ipe_watcher, "LAST_RUN_DATE_FILE",  tmp_path / "last_run_date.txt")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("CVM_USERNAME", "fake-login")
    monkeypatch.setenv("CVM_PASSWORD", "fake-senha")

    today = date.today().isoformat()
    fake_company_map = {
        "24392": {
            "ticker":       "HAPV3",
            "cnpj":         "05.197.443/0001-38",
            "cnpj_digits":  "05197443000138",
            "denom":        "HAPVIDA",
        }
    }
    fake_df = pd.DataFrame([{
        "ccvm":       "24392",
        "DT_ENTREGA": today,
        "CATEG_DOC":  ipe_watcher.VLMO_CATEGORY,
        "TIPO_DOC":   "Valores Mobiliários",
        "LINK_DOC":   "http://example.com/vlmo.pdf",
        "ASSUNTO":    "Posição Consolidada",
        "Situacao":   "Liberado",
    }])

    fake_txns = [{
        "position_type": "Diretor ou Vinculado",
        "operation_type": "Compra à vista",
        "asset":          "Ações ON",
        "day":            5,
        "quantity":       1000.0,
        "unit_price":     50.0,
        "volume":         50000.0,
    }]

    with (
        patch("ipe_watcher.download_ipe_b3",              return_value=fake_df),
        patch("ipe_watcher.load_company_map",              return_value=fake_company_map),
        patch("ipe_watcher.download_pdf",                  return_value=b"%PDF-fake"),
        patch("ipe_watcher.parse_vlmo_pdf",                return_value=fake_txns),
        patch("ipe_watcher.send_vlmo_batch_notification") as mock_vlmo_notify,
        patch("ipe_watcher.call_llm")                      as mock_llm,
        patch("ipe_watcher.send_notification")             as mock_notify,
        patch("ipe_watcher.git_commit_back"),
        patch("ipe_watcher.create_session",                return_value=MagicMock()),
    ):
        ipe_watcher.main()

    mock_vlmo_notify.assert_called_once()
    mock_notify.assert_not_called()
    mock_llm.assert_not_called()
    ids = ipe_watcher.load_processed_ids()
    assert len(ids) == 1


def test_main_saves_last_run_date_after_run(tmp_path, monkeypatch):
    """main() must persist today's date to LAST_RUN_DATE_FILE after completing."""
    import ipe_watcher

    today = date(2026, 4, 21)
    state_file = tmp_path / "last_run_date.txt"

    monkeypatch.setattr(ipe_watcher, "PROCESSED_IDS_FILE",  tmp_path / "ids.json")
    monkeypatch.setattr(ipe_watcher, "SKIPPED_LOG_FILE",    tmp_path / "skipped.csv")
    monkeypatch.setattr(ipe_watcher, "INDEX_LATEST_FILE",   tmp_path / "latest.csv")
    monkeypatch.setattr(ipe_watcher, "LAST_RUN_DATE_FILE",  state_file)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("CVM_USERNAME", "fake-login")
    monkeypatch.setenv("CVM_PASSWORD", "fake-senha")

    empty_df = pd.DataFrame(columns=["ccvm", "DT_ENTREGA", "CATEG_DOC",
                                      "TIPO_DOC", "LINK_DOC", "ASSUNTO", "Situacao"])

    with (
        patch("ipe_watcher.download_ipe_b3", return_value=empty_df),
        patch("ipe_watcher.load_company_map", return_value={}),
        patch("ipe_watcher.git_commit_back"),
        patch("ipe_watcher.create_session", return_value=MagicMock()),
        patch("ipe_watcher.datetime") as mock_dt,
    ):
        mock_dt.now.return_value.date.return_value = today
        mock_dt.utcnow.return_value.strftime.return_value = "2026-04-21 12:00 UTC"
        ipe_watcher.main()

    assert state_file.read_text() == "2026-04-21"
