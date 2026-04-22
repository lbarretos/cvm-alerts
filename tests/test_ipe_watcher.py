import io
import zipfile
import json
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def _make_zip_with_csv(csv_text: str, year: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"ipe_cia_aberta_{year}.csv", csv_text.encode("latin-1"))
    return buf.getvalue()


SAMPLE_CSV = (
    "CNPJ_CIA;DENOM_CIA;DT_REFER;DT_ENTREGA;CATEG_DOC;TIPO_DOC;ASSUNTO;LINK_DOC\n"
    "00.494.205/0001-31;HAPVIDA;2026-04-22;2026-04-22;"
    "Fato Relevante;Fato Relevante;Acordo operacional;http://example.com/doc1.pdf\n"
    "99.999.999/0001-00;OTHER CO;2026-04-22;2026-04-22;"
    "Fato Relevante;Fato Relevante;Outro fato;http://example.com/doc2.pdf\n"
)


# ── Task 6: download_ipe_index ────────────────────────────────────────────────

def test_download_ipe_index_returns_dataframe():
    import ipe_watcher
    zip_bytes = _make_zip_with_csv(SAMPLE_CSV, 2026)
    mock_resp = MagicMock()
    mock_resp.content = zip_bytes
    mock_resp.raise_for_status = MagicMock()
    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    df = ipe_watcher.download_ipe_index(mock_session, 2026)

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert "CNPJ_CIA" in df.columns
    assert "CATEG_DOC" in df.columns


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
    monkeypatch.setattr(ipe_watcher, "PROCESSED_IDS_FILE", tmp_path / "ids.json")
    monkeypatch.setattr(ipe_watcher, "SKIPPED_LOG_FILE",   tmp_path / "skipped.csv")
    monkeypatch.setattr(ipe_watcher, "INDEX_LATEST_FILE",  tmp_path / "latest.csv")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    today = date.today().isoformat()
    sample_csv = (
        "CNPJ_CIA;DENOM_CIA;DT_REFER;DT_ENTREGA;CATEG_DOC;TIPO_DOC;ASSUNTO;LINK_DOC\n"
        f"00494205000131;HAPVIDA PARTICIPACOES;{today};{today};"
        "Fato Relevante;Fato Relevante;Acordo operacional;http://example.com/doc.pdf\n"
    )

    with (
        patch("ipe_watcher.download_ipe_index") as mock_idx,
        patch("ipe_watcher.load_cnpj_map", return_value={"00494205000131"}),
        patch("ipe_watcher.download_pdf", return_value=b"%PDF-fake"),
        patch("ipe_watcher.extract_text", return_value="Texto do documento"),
        patch("ipe_watcher.call_llm", return_value="- Fato: X\n- Impacto: Y\n- Risco: Z"),
        patch("ipe_watcher.send_notification") as mock_notify,
        patch("ipe_watcher.git_commit_back"),
        patch("ipe_watcher.create_session", return_value=MagicMock()),
    ):
        df = pd.read_csv(io.StringIO(sample_csv), sep=";", dtype=str)
        df["_cnpj_digits"] = df["CNPJ_CIA"].apply(ipe_watcher._digits)
        mock_idx.return_value = df

        ipe_watcher.main()

    mock_notify.assert_called_once()
    assert ipe_watcher.PROCESSED_IDS_FILE.exists()
    ids = ipe_watcher.load_processed_ids()
    assert len(ids) == 1
