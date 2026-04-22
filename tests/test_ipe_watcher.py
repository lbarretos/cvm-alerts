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
