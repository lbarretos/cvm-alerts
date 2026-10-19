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
        Especie="Acordo operacional" Situacao="Liberado"
        num_seq_docm="1001" num_vers_docm="1" />
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
    assert df.iloc[0]["DT_REF"] == "2026-04-22"


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

    # LOGIN INCORRETO must raise CredentialError (subclass of RuntimeError)
    with pytest.raises(ipe_watcher.CredentialError, match="LOGIN INCORRETO"):
        ipe_watcher.download_ipe_b3(mock_session, date(2026, 4, 22))


def _make_credential_error_session(desc: str) -> MagicMock:
    """Return a mock session that replies with a B3 credential error XML."""
    error_xml = (
        b'<?xml version="1.0" encoding="ISO-8859-1" ?>'
        b"<ERROS>"
        b"<NUMERO_DO_ERRO>1</NUMERO_DO_ERRO>"
        b"<DESCRICAO_DO_ERRO>" + desc.encode("iso-8859-1") + b"</DESCRICAO_DO_ERRO>"
        b"</ERROS>"
    )
    mock_resp = MagicMock()
    mock_resp.content = error_xml
    mock_resp.raise_for_status = MagicMock()
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp
    return mock_session


@pytest.mark.parametrize("desc", ["SENHA EXPIRADA", "LOGIN INCORRETO", "USUÁRIO SUSPENSO"])
def test_credential_errors_raise_credential_error(monkeypatch, desc):
    """download_ipe_b3 raises CredentialError (not plain RuntimeError) for
    permanent credential failures so callers can distinguish them from
    transient network errors."""
    import ipe_watcher
    monkeypatch.setenv("CVM_USERNAME", "testuser")
    monkeypatch.setenv("CVM_PASSWORD", "testpass")

    with pytest.raises(ipe_watcher.CredentialError, match=desc):
        ipe_watcher.download_ipe_b3(
            _make_credential_error_session(desc), date(2026, 4, 22)
        )


def test_main_exits_nonzero_on_credential_error(monkeypatch):
    """main() must call sys.exit(1) and send a Telegram alert when the B3 API
    returns a permanent credential error.  A transient network error on one
    date must NOT trigger exit(1)."""
    import ipe_watcher

    monkeypatch.setenv("CVM_USERNAME", "testuser")
    monkeypatch.setenv("CVM_PASSWORD", "testpass")

    telegram_calls: list[str] = []

    def fake_send_message(text, **_):
        telegram_calls.append(text)

    def fake_download_raises_credential(_session, _date):
        raise ipe_watcher.CredentialError("SENHA EXPIRADA")

    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", fake_download_raises_credential)
    monkeypatch.setattr(ipe_watcher, "send_message", fake_send_message)

    with pytest.raises(SystemExit) as exc_info:
        ipe_watcher.main()

    assert exc_info.value.code == 1, "main() must exit with code 1 on credential error"
    assert telegram_calls, "main() must send a Telegram alert on credential error"
    assert "credential" in telegram_calls[0].lower() or "credencial" in telegram_calls[0].lower()


def test_non_credential_b3_error_raises_runtime_error(monkeypatch):
    """download_ipe_b3 raises plain RuntimeError (not CredentialError) for
    non-permanent B3 API errors like 'SISTEMA INDISPONIVEL'."""
    import ipe_watcher
    monkeypatch.setenv("CVM_USERNAME", "testuser")
    monkeypatch.setenv("CVM_PASSWORD", "testpass")

    error_xml = (
        b'<?xml version="1.0" encoding="ISO-8859-1" ?>'
        b"<ERROS>"
        b"<NUMERO_DO_ERRO>99</NUMERO_DO_ERRO>"
        b"<DESCRICAO_DO_ERRO>SISTEMA INDISPONIVEL</DESCRICAO_DO_ERRO>"
        b"</ERROS>"
    )
    mock_resp = MagicMock()
    mock_resp.content = error_xml
    mock_resp.raise_for_status = MagicMock()
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp

    # Must be RuntimeError but NOT CredentialError (pytest.raises already confirms type)
    with pytest.raises(RuntimeError, match="SISTEMA INDISPONIVEL") as exc_info:
        ipe_watcher.download_ipe_b3(mock_session, date(2026, 4, 22))

    assert type(exc_info.value) is RuntimeError, "must be plain RuntimeError, not CredentialError subclass"


def test_credential_error_tolerates_surrounding_whitespace(monkeypatch):
    """B3 error descriptions may have leading/trailing whitespace; the strip()
    call in download_ipe_b3 must still match against B3_CREDENTIAL_ERRORS."""
    import ipe_watcher
    monkeypatch.setenv("CVM_USERNAME", "testuser")
    monkeypatch.setenv("CVM_PASSWORD", "testpass")

    with pytest.raises(ipe_watcher.CredentialError):
        ipe_watcher.download_ipe_b3(
            _make_credential_error_session(" SENHA EXPIRADA "), date(2026, 4, 22)
        )


def test_main_exits_nonzero_even_when_telegram_fails(monkeypatch):
    """main() must still call sys.exit(1) even if send_message() raises while
    handling a CredentialError (best-effort Telegram alert)."""
    import ipe_watcher

    monkeypatch.setenv("CVM_USERNAME", "testuser")
    monkeypatch.setenv("CVM_PASSWORD", "testpass")

    def fake_download_raises_credential(_session, _date):
        raise ipe_watcher.CredentialError("SENHA EXPIRADA")

    def fake_send_message_fails(text, **_):
        raise OSError("Telegram unreachable")

    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", fake_download_raises_credential)
    monkeypatch.setattr(ipe_watcher, "send_message", fake_send_message_fails)

    with pytest.raises(SystemExit) as exc_info:
        ipe_watcher.main()

    assert exc_info.value.code == 1, "sys.exit(1) must fire even when Telegram is down"


# ── Task 7: pre_filter ────────────────────────────────────────────────────────

import ipe_watcher


def _row(categ: str, assunto: str) -> pd.Series:
    return pd.Series({
        "CATEG_DOC": categ,
        "TIPO_DOC": categ,
        "ASSUNTO": assunto,
        "DENOM_CIA": "TEST CO",
        "DT_REF": "2026-04-22",
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
        "DT_REF": "2026-04-22",
        "TIPO_DOC": "Fato Relevante",
        "LINK_DOC": "http://example.com/doc.pdf",
    })
    h1 = ipe_watcher.make_hash(row)
    h2 = ipe_watcher.make_hash(row)
    assert h1 == h2
    assert len(h1) == 64


def test_make_hash_differs_by_link():
    row1 = pd.Series({"CNPJ_CIA": "X", "DT_REF": "Y", "TIPO_DOC": "Z", "LINK_DOC": "A"})
    row2 = pd.Series({"CNPJ_CIA": "X", "DT_REF": "Y", "TIPO_DOC": "Z", "LINK_DOC": "B"})
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
        "DT_REF": "2026-04-22",
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
        "DT_REF": today,
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
    """On Monday (2026-04-20), last run was Friday (2026-04-17): must query
    Fri (again — the last run may have missed its evening), Sat, Sun, Mon."""
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

    empty_df = pd.DataFrame(columns=["ccvm", "DT_REF", "CATEG_DOC",
                                      "TIPO_DOC", "LINK_DOC", "ASSUNTO", "Situacao"])

    queried_dates = []

    def fake_download(session, qd):
        queried_dates.append(qd)
        return empty_df.copy()

    with (
        patch("ipe_watcher.download_ipe_b3", side_effect=fake_download),
        patch("ipe_watcher.load_company_map", return_value={}),
        patch("ipe_watcher.create_session", return_value=MagicMock()),
        patch("ipe_watcher.datetime") as mock_dt,
    ):
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value.date.return_value = monday
        mock_dt.utcnow.return_value.strftime.return_value = "2026-04-20 09:00 UTC"
        ipe_watcher.main()

    assert queried_dates == [friday, saturday, sunday, monday]


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

    empty_df = pd.DataFrame(columns=["ccvm", "DT_REF", "CATEG_DOC",
                                      "TIPO_DOC", "LINK_DOC", "ASSUNTO", "Situacao"])
    queried_dates = []

    def fake_download(session, qd):
        queried_dates.append(qd)
        return empty_df.copy()

    with (
        patch("ipe_watcher.download_ipe_b3", side_effect=fake_download),
        patch("ipe_watcher.load_company_map", return_value={}),
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

def _sent_text(mock_blocks) -> str:
    header, blocks = mock_blocks.call_args[0]
    return header + "\n\n".join(blocks)


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
                "quantity":       1000.0,
                "volume":         50000.0,
            }],
        }
    }
    with patch("ipe_watcher.send_blocks", return_value=True) as mock_send:
        assert ipe_watcher.send_vlmo_batch_notification(vlmo_by_company) is True
    msg = _sent_text(mock_send)
    assert "TEST CO" in msg
    assert "• Diretor comprou 1.000 ações, valor R$ 50 mil, preço médio R$ 50,00" in msg
    assert "abril/2026" in msg


def test_send_vlmo_batch_empty_transactions():
    vlmo_by_company = {
        "24392": {
            "ticker":       "TEST3",
            "denom":        "TEST CO",
            "ref_month":    "2026-04",
            "primary_link": "",
            "transactions": [],
        }
    }
    with patch("ipe_watcher.send_blocks", return_value=True) as mock_send:
        ipe_watcher.send_vlmo_batch_notification(vlmo_by_company)
    msg = _sent_text(mock_send)
    assert "TEST CO" in msg
    assert "sem transações" in msg.lower()


def test_send_vlmo_batch_aggregates_buybacks():
    txns = [
        {"position_type": "Recompra", "operation_type": "Compra", "quantity": 10_000.0, "volume": 500_000.0},
        {"position_type": "Recompra", "operation_type": "Compra", "quantity": 14_000.0, "volume": 700_000.0},
        {"position_type": "Recompra", "operation_type": "Compra", "quantity": 6_000.0, "volume": 300_000.0},
    ]
    vlmo_by_company = {
        "24392": {
            "ticker": "CO3", "denom": "CO", "ref_month": "2026-04",
            "primary_link": "http://x", "transactions": txns,
        }
    }
    with patch("ipe_watcher.send_blocks", return_value=True) as mock_send:
        ipe_watcher.send_vlmo_batch_notification(vlmo_by_company)
    assert "• Recompra de 30.000 ações, valor R$ 1,5 mi, preço médio R$ 50,00" in _sent_text(mock_send)


def test_send_vlmo_batch_one_block_per_company():
    data = {c: {"ticker": f"T{c}", "denom": f"CO {c}", "ref_month": "2026-04",
                "primary_link": "", "transactions": []} for c in ("1", "2", "3")}
    with patch("ipe_watcher.send_blocks", return_value=True) as mock_send:
        ipe_watcher.send_vlmo_batch_notification(data)
    assert len(mock_send.call_args[0][1]) == 3


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
        "DT_REF": today,
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

    empty_df = pd.DataFrame(columns=["ccvm", "DT_REF", "CATEG_DOC",
                                      "TIPO_DOC", "LINK_DOC", "ASSUNTO", "Situacao"])

    with (
        patch("ipe_watcher.download_ipe_b3", return_value=empty_df),
        patch("ipe_watcher.load_company_map", return_value={}),
        patch("ipe_watcher.create_session", return_value=MagicMock()),
        patch("ipe_watcher.datetime") as mock_dt,
    ):
        mock_dt.now.return_value.date.return_value = today
        mock_dt.utcnow.return_value.strftime.return_value = "2026-04-21 12:00 UTC"
        ipe_watcher.main()

    assert state_file.read_text() == "2026-04-21"


# ── B3 error codes (CVM technical note §4.2) ─────────────────────────────────

def _b3_session(*bodies) -> MagicMock:
    """Mock session whose successive POSTs return the given XML bodies."""
    resps = []
    for body in bodies:
        r = MagicMock()
        r.content = body
        r.raise_for_status = MagicMock()
        resps.append(r)
    session = MagicMock()
    session.post.side_effect = resps
    return session


def _b3_error(code: str, desc: str) -> bytes:
    return (
        b'<?xml version="1.0" encoding="ISO-8859-1" ?><ERROS>'
        b"<NUMERO_DO_ERRO>" + code.encode() + b"</NUMERO_DO_ERRO>"
        b"<DESCRICAO_DO_ERRO>" + desc.encode("iso-8859-1") + b"</DESCRICAO_DO_ERRO>"
        b"</ERROS>"
    )


@pytest.fixture
def b3_env(monkeypatch):
    monkeypatch.setenv("CVM_USERNAME", "testuser")
    monkeypatch.setenv("CVM_PASSWORD", "testpass")
    monkeypatch.setattr(ipe_watcher.time, "sleep", lambda _s: None)


def test_no_records_error_returns_empty_dataframe(b3_env):
    session = _b3_session(_b3_error("22016", "Nenhum registro localizado"))
    df = ipe_watcher.download_ipe_b3(session, date(2026, 4, 18))
    assert df.empty
    assert "DT_REF" in df.columns


def test_error_number_1_is_credential_error_whatever_the_text(b3_env):
    session = _b3_session(_b3_error("1", "MENSAGEM NOVA DA B3"))
    with pytest.raises(ipe_watcher.CredentialError):
        ipe_watcher.download_ipe_b3(session, date(2026, 4, 22))


def test_credential_text_matched_without_accents_or_punctuation(b3_env):
    session = _b3_session(_b3_error("99", "Usuario suspenso."))
    with pytest.raises(ipe_watcher.CredentialError):
        ipe_watcher.download_ipe_b3(session, date(2026, 4, 22))


def test_credential_error_is_not_retried(b3_env):
    session = _b3_session(_b3_error("1", "SENHA EXPIRADA"), b"unused")
    with pytest.raises(ipe_watcher.CredentialError):
        ipe_watcher.download_ipe_b3(session, date(2026, 4, 22))
    assert session.post.call_count == 1


@pytest.mark.parametrize("code", ["22014", "22015", "22017"])
def test_bad_request_errors_raise_b3_request_error(b3_env, code):
    session = _b3_session(_b3_error(code, "Parametro invalido"))
    with pytest.raises(ipe_watcher.B3RequestError, match=code):
        ipe_watcher.download_ipe_b3(session, date(2026, 4, 22))


def test_transient_error_22013_is_retried(b3_env):
    session = _b3_session(_b3_error("22013", "Erro ao gerar o XML"), SAMPLE_B3_XML)
    df = ipe_watcher.download_ipe_b3(session, date(2026, 4, 22))
    assert len(df) == 2
    assert session.post.call_count == 2


def test_unparseable_response_is_retried_then_raises(b3_env):
    html = b"<html><body>Sistema em manutencao"
    session = _b3_session(html, html, html)
    with pytest.raises(Exception):
        ipe_watcher.download_ipe_b3(session, date(2026, 4, 22))
    assert session.post.call_count == ipe_watcher.B3_MAX_ATTEMPTS


# ── main(): window, failures, delivery ───────────────────────────────────────

_FAKE_MAP = {
    "24392": {
        "ticker": "HAPV3",
        "cnpj": "05.197.443/0001-38",
        "cnpj_digits": "05197443000138",
        "denom": "HAPVIDA",
    }
}


def _doc_df(dt_ref: str, link: str = "http://example.com/doc.pdf",
            categ: str = "Fato Relevante", assunto: str = "Acordo operacional") -> pd.DataFrame:
    return pd.DataFrame([{
        "ccvm": "24392", "DT_REF": dt_ref, "CATEG_DOC": categ, "TIPO_DOC": categ,
        "LINK_DOC": link, "ASSUNTO": assunto, "Situacao": "Liberado",
    }])


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    state = tmp_path / "last_run_date.txt"
    monkeypatch.setattr(ipe_watcher, "PROCESSED_IDS_FILE", tmp_path / "ids.json")
    monkeypatch.setattr(ipe_watcher, "SKIPPED_LOG_FILE",   tmp_path / "skipped.csv")
    monkeypatch.setattr(ipe_watcher, "INDEX_LATEST_FILE",  tmp_path / "latest.csv")
    monkeypatch.setattr(ipe_watcher, "LAST_RUN_DATE_FILE", state)
    monkeypatch.setattr(ipe_watcher, "load_company_map", lambda: _FAKE_MAP)
    monkeypatch.setattr(ipe_watcher, "create_session", MagicMock)
    monkeypatch.setattr(ipe_watcher, "download_pdf", lambda *_: b"%PDF")
    monkeypatch.setattr(ipe_watcher, "extract_text", lambda _b: "texto")
    monkeypatch.setattr(ipe_watcher, "call_llm", lambda **_: "- Fato: X")
    monkeypatch.setattr(ipe_watcher.time, "sleep", lambda _s: None)
    return state


def _run_main(today: date) -> None:
    with patch("ipe_watcher.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = today
        mock_dt.utcnow.return_value.strftime.return_value = "x"
        ipe_watcher.main()


def test_main_processes_doc_whose_reference_date_precedes_window(main_env, monkeypatch):
    """DataRef is the reference date, not the delivery date: a doc delivered
    today with an older DataRef must still be notified."""
    today = date(2026, 4, 21)
    main_env.write_text(today.isoformat())
    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", lambda _s, _d: _doc_df("2026-03-31"))
    sent = []
    monkeypatch.setattr(ipe_watcher, "send_notification", lambda row, _s: sent.append(row) or True)

    _run_main(today)

    assert len(sent) == 1


def test_main_all_dates_failing_alerts_and_exits_nonzero(main_env, monkeypatch):
    today = date(2026, 4, 21)
    main_env.write_text("2026-04-20")

    def fail(_s, _d):
        raise RuntimeError("B3 API error 22013: Erro ao gerar o XML")

    alerts = []
    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", fail)
    monkeypatch.setattr(ipe_watcher, "send_message", lambda text: alerts.append(text) or True)

    with pytest.raises(SystemExit) as exc_info:
        _run_main(today)

    assert exc_info.value.code == 1
    assert alerts and "indisponível" in alerts[0]
    assert main_env.read_text() == "2026-04-20", "last_run_date must not advance"


def test_main_failed_date_stops_last_run_date_and_exits_nonzero(main_env, monkeypatch):
    """Sat fails, Fri/Sun/Mon succeed: docs still processed, last_run_date
    stays at Fri so Sat is queried again, run exits 1 for visibility."""
    friday, saturday, monday = date(2026, 4, 17), date(2026, 4, 18), date(2026, 4, 20)
    main_env.write_text(friday.isoformat())

    def download(_s, qd):
        if qd == saturday:
            raise RuntimeError("timeout")
        if qd == monday:
            return _doc_df("2026-04-20")
        return _doc_df("2026-04-01").iloc[0:0]

    sent = []
    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", download)
    monkeypatch.setattr(ipe_watcher, "send_notification", lambda row, _s: sent.append(row) or True)

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monday)

    assert exc_info.value.code == 1
    assert len(sent) == 1
    assert main_env.read_text() == friday.isoformat()


def test_main_request_error_aborts_with_alert(main_env, monkeypatch):
    main_env.write_text("2026-04-21")

    def bad(_s, _d):
        raise ipe_watcher.B3RequestError("B3 API error 22014: Data informada inválida")

    alerts = []
    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", bad)
    monkeypatch.setattr(ipe_watcher, "send_message", lambda text: alerts.append(text) or True)

    with pytest.raises(SystemExit) as exc_info:
        _run_main(date(2026, 4, 21))

    assert exc_info.value.code == 1
    assert "22014" in alerts[0]


def test_credential_alert_points_to_cas_and_enet(main_env, monkeypatch):
    main_env.write_text("2026-04-21")

    def cred(_s, _d):
        raise ipe_watcher.CredentialError("SENHA EXPIRADA")

    alerts = []
    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", cred)
    monkeypatch.setattr(ipe_watcher, "send_message", lambda text: alerts.append(text) or True)

    with pytest.raises(SystemExit):
        _run_main(date(2026, 4, 21))

    assert "cas.bovespa.com.br" in alerts[0]
    assert "rad.cvm.gov.br/ENET" in alerts[0]
    assert "conteudo.cvm.gov.br" not in alerts[0]


def test_main_undelivered_notification_is_retried_next_run(main_env, monkeypatch):
    today = date(2026, 4, 21)
    main_env.write_text(today.isoformat())
    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", lambda _s, _d: _doc_df("2026-04-21"))

    monkeypatch.setattr(ipe_watcher, "send_notification", lambda _r, _s: False)
    with pytest.raises(SystemExit) as exc_info:
        _run_main(today)
    assert exc_info.value.code == 1
    assert ipe_watcher.load_processed_ids() == set()

    sent = []
    monkeypatch.setattr(ipe_watcher, "send_notification", lambda row, _s: sent.append(row) or True)
    _run_main(today)
    assert len(sent) == 1
    assert len(ipe_watcher.load_processed_ids()) == 1


def test_main_skipped_doc_logged_once_across_runs(main_env, monkeypatch):
    today = date(2026, 4, 21)
    main_env.write_text(today.isoformat())
    df = _doc_df("2026-04-21", categ="Aviso aos Acionistas", assunto="Pagamento de dividendos")
    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", lambda _s, _d: df)

    _run_main(today)
    _run_main(today)

    lines = ipe_watcher.SKIPPED_LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # header + one row


def test_main_vlmo_not_marked_processed_when_batch_undelivered(main_env, monkeypatch):
    today = date(2026, 4, 21)
    main_env.write_text(today.isoformat())
    df = _doc_df("2026-03-01", categ=ipe_watcher.VLMO_CATEGORY)
    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", lambda _s, _d: df)
    monkeypatch.setattr(ipe_watcher, "parse_vlmo_pdf", lambda _b: [])
    monkeypatch.setattr(ipe_watcher, "send_vlmo_batch_notification", lambda _v: False)

    with pytest.raises(SystemExit):
        _run_main(today)

    assert ipe_watcher.load_processed_ids() == set()


# ── telegram_notifier delivery result ────────────────────────────────────────

def test_send_falls_back_to_plain_text_on_markdown_rejection():
    import telegram_notifier
    bad, ok = MagicMock(status_code=400, ok=False, text="can't parse entities"), \
        MagicMock(status_code=200, ok=True)
    with patch("telegram_notifier.requests.post", side_effect=[bad, ok]) as post:
        assert telegram_notifier._send("t", "c", "*unbalanced") is True
    assert "parse_mode" not in post.call_args_list[1].kwargs["json"]


def test_send_message_returns_false_when_delivery_fails(monkeypatch):
    import telegram_notifier
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    fail = MagicMock(status_code=500, ok=False, text="err")
    with patch("telegram_notifier.requests.post", return_value=fail):
        assert telegram_notifier.send_message("hi") is False


def test_vlmo_batch_month_label_in_portuguese():
    data = {"24392": {"ticker": "HAPV3", "denom": "HAPVIDA", "ref_month": "2026-03",
                      "transactions": [], "primary_link": ""}}
    with patch("ipe_watcher.send_blocks", return_value=True) as mock_send:
        assert ipe_watcher.send_vlmo_batch_notification(data) is True
    assert "março/2026" in _sent_text(mock_send)


def test_main_drops_vlmo_older_than_latest_disclosed_month(main_env, monkeypatch):
    """On 2026-10-19 the latest disclosed month is September: an August form
    (late filing or new version) is not notified, and is not retried."""
    today = date(2026, 10, 19)
    main_env.write_text(today.isoformat())
    df = pd.concat([_doc_df("2026-08-01", link="http://x/ago.pdf", categ=ipe_watcher.VLMO_CATEGORY),
                    _doc_df("2026-09-01", link="http://x/set.pdf", categ=ipe_watcher.VLMO_CATEGORY)])
    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", lambda _s, _d: df)
    monkeypatch.setattr(ipe_watcher, "parse_vlmo_pdf", lambda _b: [])
    sent = []
    monkeypatch.setattr(ipe_watcher, "send_vlmo_batch_notification",
                        lambda v: sent.append(v) or True)

    _run_main(today)

    assert len(sent) == 1
    assert [d["ref_month"] for d in sent[0].values()] == ["2026-09"]
    assert len(ipe_watcher.load_processed_ids()) == 2


def test_download_logs_link_attribute_names(b3_env, caplog):
    caplog.set_level("INFO", logger="ipe_watcher")
    ipe_watcher.download_ipe_b3(_b3_session(SAMPLE_B3_XML), date(2026, 4, 22))
    assert "B3 Link attributes" in caplog.text
    assert "Especie" in caplog.text


# ── State persistence ────────────────────────────────────────────────────────

def test_load_processed_ids_merges_legacy_cache_copy(tmp_path, monkeypatch):
    git_copy, cache_copy = tmp_path / "ids.json", tmp_path / "ids.cache.json"
    git_copy.write_text('["a", "b"]')
    cache_copy.write_text('["b", "c"]')
    monkeypatch.setattr(ipe_watcher, "PROCESSED_IDS_FILE", git_copy)
    monkeypatch.setattr(ipe_watcher, "LEGACY_CACHE_IDS_FILE", cache_copy)
    assert ipe_watcher.load_processed_ids() == {"a", "b", "c"}


def test_abort_marks_alert_sent_only_when_delivered(tmp_path, monkeypatch):
    marker = tmp_path / ".alert_sent"
    monkeypatch.setattr(ipe_watcher, "ALERT_SENT_MARKER", marker)

    monkeypatch.setattr(ipe_watcher, "send_message", lambda _t: False)
    with pytest.raises(SystemExit):
        ipe_watcher._abort("x", RuntimeError("y"), "z")
    assert not marker.exists()

    monkeypatch.setattr(ipe_watcher, "send_message", lambda _t: True)
    with pytest.raises(SystemExit):
        ipe_watcher._abort("x", RuntimeError("y"), "z")
    assert marker.exists()


# ── Dedup by B3 document number + version ────────────────────────────────────

def _doc_row(link="http://x/a.pdf", seq="1001", vers="1"):
    return pd.Series({"CNPJ_CIA": "X", "DT_REF": "2026-10-19", "TIPO_DOC": "Fato Relevante",
                      "LINK_DOC": link, "SEQ_DOC": seq, "VERS_DOC": vers})


def test_download_ipe_b3_reads_document_number_and_version(b3_env):
    df = ipe_watcher.download_ipe_b3(_b3_session(SAMPLE_B3_XML), date(2026, 4, 22))
    assert df.iloc[0]["SEQ_DOC"] == "1001"
    assert df.iloc[0]["VERS_DOC"] == "1"
    assert df.iloc[1]["SEQ_DOC"] == ""   # attribute absent → legacy key


def test_same_document_new_url_is_a_duplicate():
    assert ipe_watcher.make_hash(_doc_row(link="http://x/a.pdf")) == \
        ipe_watcher.make_hash(_doc_row(link="http://x/b.pdf"))


def test_new_version_is_not_a_duplicate():
    assert ipe_watcher.make_hash(_doc_row(vers="1")) != ipe_watcher.make_hash(_doc_row(vers="2"))


def test_without_document_number_falls_back_to_legacy_key():
    row = _doc_row(seq="")
    assert ipe_watcher.make_hash(row) == ipe_watcher.legacy_hash(row)


def test_main_skips_documents_processed_under_legacy_key(main_env, monkeypatch):
    """IDs saved before the switch are legacy hashes: the same document must
    not be re-sent once it arrives with num_seq_docm."""
    today = date(2026, 4, 21)
    main_env.write_text(today.isoformat())
    df = _doc_df("2026-04-21").assign(SEQ_DOC="1001", VERS_DOC="1")
    legacy = ipe_watcher.legacy_hash(pd.Series({
        "CNPJ_CIA": "05.197.443/0001-38", "DT_REF": "2026-04-21",
        "TIPO_DOC": "Fato Relevante", "LINK_DOC": "http://example.com/doc.pdf"}))
    ipe_watcher.save_processed_ids({legacy})
    monkeypatch.setattr(ipe_watcher, "download_ipe_b3", lambda _s, _d: df)
    sent = []
    monkeypatch.setattr(ipe_watcher, "send_notification", lambda row, _s: sent.append(row) or True)

    _run_main(today)

    assert sent == []


def test_new_version_is_labelled_in_the_alert():
    row = _doc_row(vers="2").copy()
    row["CATEG_DOC"], row["DENOM_CIA"] = "Fato Relevante", "CO"
    with patch("ipe_watcher.send_message", return_value=True) as send:
        ipe_watcher.send_notification(row, "- Fato: X")
    assert "(reapresentação, versão 2)" in send.call_args[0][0]
