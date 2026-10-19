from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import alerts
import telegram_notifier

COVERAGE = {
    "02.286.479/0001-08": {"tickers": ["RENT3"], "denom": "LOCALIZA FLEET S.A."},
    "08.873.873/0001-10": {"tickers": ["ECOR3"], "denom": "ECORODOVIAS"},
}
RENT, ECOR = "02.286.479/0001-08", "08.873.873/0001-10"
OUTSIDE = "00.000.000/0001-91"


def _row(cnpj, ref, pos, mov, qty, vol, asset="Ações", desc=""):
    return {
        "Company_CNPJ": cnpj, "Reference_Date": ref, "Position_Type": pos,
        "Movement_Type": mov, "Movement_Description": desc, "Asset_Type": asset,
        "Quantity": qty, "Volume": vol,
    }


CTRL, DIR, FISCAL = "Controlador ou Vinculado", "Diretor ou Vinculado", "Conselho Fiscal ou Vinculado"


# ── latest_disclosed_month ───────────────────────────────────────────────────

@pytest.mark.parametrize("today, expected", [
    (date(2026, 10, 19), "2026-09"),   # September forms were due by Oct 10
    (date(2026, 11, 5),  "2026-09"),   # October forms still arriving
    (date(2026, 11, 11), "2026-10"),
    (date(2026, 1, 15),  "2025-12"),
])
def test_latest_disclosed_month(today, expected):
    assert str(alerts.latest_disclosed_month(today)) == expected


# ── trade_lines / highlights ─────────────────────────────────────────────────

def test_trade_lines_keep_only_coverage_month_equity_trades():
    df = pd.DataFrame([
        _row(RENT, "2026-09-01", CTRL, "Compra à vista", 1000, 100_000),
        _row(RENT, "2026-09-01", CTRL, "Compra à vista", 3000, 300_000),
        _row(RENT, "2026-08-01", CTRL, "Compra à vista", 1000, 100_000),        # old month
        _row(OUTSIDE, "2026-09-01", CTRL, "Compra à vista", 1000, 100_000),     # not covered
        _row(RENT, "2026-09-01", CTRL, "Contratação de empréstimo (locador)", 9, 9),  # not a trade
        _row(RENT, "2026-09-01", CTRL, "Compra à vista", 9, 9, asset="Debêntures"),
        _row(RENT, "2026-09-01", DIR, "Compra à vista", 500, 0),                # grant at no price
    ])
    lines = alerts.trade_lines(df, COVERAGE, pd.Period("2026-09", freq="M"))
    assert len(lines) == 1
    assert lines.iloc[0]["Quantity"] == 4000
    assert lines.iloc[0]["Volume"] == 400_000


def test_buyback_detected_from_description():
    df = pd.DataFrame([_row(RENT, "2026-09-01", DIR, "Outras Entradas", 100, 1000,
                            desc="RECOMPRA DE AÇÕES PELA COMPANHIA")])
    lines = alerts.trade_lines(df, COVERAGE, pd.Period("2026-09", freq="M"))
    assert lines.iloc[0]["direction"] == "recompra"


def test_highlights_p1_exec_line_above_500k():
    lines = pd.DataFrame([
        {"Company_CNPJ": RENT, "Position_Type": DIR, "direction": "venda", "Quantity": 1, "Volume": 600_000},
        {"Company_CNPJ": ECOR, "Position_Type": FISCAL, "direction": "venda", "Quantity": 1, "Volume": 600_000},
    ])
    assert set(alerts.highlights(lines)) == {RENT}  # Conselho Fiscal is not P1


def test_highlights_p2_company_total_above_2m():
    lines = pd.DataFrame([
        {"Company_CNPJ": ECOR, "Position_Type": FISCAL, "direction": "venda", "Quantity": 1, "Volume": 1_200_000},
        {"Company_CNPJ": ECOR, "Position_Type": FISCAL, "direction": "compra", "Quantity": 1, "Volume": 900_000},
    ])
    assert set(alerts.highlights(lines)) == {ECOR}


def test_small_trades_produce_no_highlight():
    lines = pd.DataFrame([
        {"Company_CNPJ": RENT, "Position_Type": CTRL, "direction": "compra", "Quantity": 1, "Volume": 400_000},
    ])
    assert alerts.highlights(lines) == {}


# ── weekly_summary: format and state ─────────────────────────────────────────

def _sept_df():
    return pd.DataFrame([
        _row(RENT, "2026-09-01", CTRL, "Compra à vista", 10_000, 600_000),
        _row(RENT, "2026-09-01", DIR, "Venda à vista", 2_000, 100_000),
        _row(ECOR, "2026-09-01", CTRL, "Venda à vista", 100_000, 2_500_000),
    ])


def test_weekly_summary_format_matches_requested_layout():
    header, blocks, _ = alerts.weekly_summary(_sept_df(), COVERAGE, date(2026, 10, 19), {})
    assert "setembro/2026" in header
    assert blocks[0] == (
        "*ECORODOVIAS* (ECOR3)\n"
        "• Controlador vendeu 100.000 ações, valor R$ 2,5 mi, preço médio R$ 25,00"
    )
    assert blocks[1] == (
        "*LOCALIZA FLEET S.A.* (RENT3)\n"
        "• Controlador comprou 10.000 ações, valor R$ 600 mil, preço médio R$ 60,00\n"
        "• Diretor vendeu 2.000 ações, valor R$ 100 mil, preço médio R$ 50,00"
    )


def test_weekly_summary_sends_each_company_once_per_month():
    _, blocks, state = alerts.weekly_summary(_sept_df(), COVERAGE, date(2026, 10, 19), {})
    assert len(blocks) == 2
    _, blocks, state2 = alerts.weekly_summary(_sept_df(), COVERAGE, date(2026, 10, 26), state)
    assert blocks == []
    assert state2 == state


def test_weekly_summary_resends_changed_company_marked_updated():
    _, _, state = alerts.weekly_summary(_sept_df(), COVERAGE, date(2026, 10, 19), {})
    df = pd.concat([_sept_df(), pd.DataFrame([
        _row(RENT, "2026-09-01", CTRL, "Compra à vista", 1_000, 60_000)])])
    _, blocks, _ = alerts.weekly_summary(df, COVERAGE, date(2026, 10, 26), state)
    assert len(blocks) == 1
    assert blocks[0].startswith("*LOCALIZA FLEET S.A.* (RENT3) — atualizado")


def test_weekly_summary_ignores_previous_month():
    """On 2026-11-12 the latest month is October: September is never sent."""
    _, blocks, state = alerts.weekly_summary(_sept_df(), COVERAGE, date(2026, 11, 12), {})
    assert blocks == []
    assert state == {}


# ── process_trading_data: latest version per form ───────────────────────────

def test_process_trading_data_keeps_latest_version_and_all_its_rows(tmp_path):
    import br_stock_trading
    cols = {
        "CNPJ_Companhia": RENT, "Nome_Companhia": "LOCALIZA", "Data_Referencia": "2026-09-01",
        "Tipo_Empresa": "Companhia", "Empresa": "LOCALIZA", "Descricao_Movimentacao": "",
        "Tipo_Operacao": "Crédito", "Tipo_Ativo": "Ações", "Caracteristica_Valor_Mobiliario": "ON",
        "Intermediario": "X", "Data_Movimentacao": "2026-09-10", "Tipo_Movimentacao": "Compra à vista",
        "Preco_Unitario": "10,0",
    }
    rows = [
        {**cols, "Versao": 1, "Tipo_Cargo": "Diretor ou Vinculado", "Quantidade": 5, "Volume": "50,0"},
        # v2 replaces v1 and has two trades with the same date and movement
        # type (different positions) — both must survive.
        {**cols, "Versao": 2, "Tipo_Cargo": "Diretor ou Vinculado", "Quantidade": 1, "Volume": "10,0"},
        {**cols, "Versao": 2, "Tipo_Cargo": "Controlador ou Vinculado", "Quantidade": 2, "Volume": "20,0"},
    ]
    path = tmp_path / "vlmo_cia_aberta_con_2026.csv"
    pd.DataFrame(rows).to_csv(path, sep=";", index=False, encoding="latin1")

    out = br_stock_trading.process_trading_data([path])

    assert sorted(out["Position_Type"]) == ["Controlador ou Vinculado", "Diretor ou Vinculado"]
    assert set(out["Version"]) == {2}
    assert sorted(out["Quantity"]) == [1, 2]


# ── telegram_notifier pacing and rate limit ──────────────────────────────────

def test_send_waits_retry_after_on_429_then_succeeds():
    limited = MagicMock(status_code=429, ok=False)
    limited.json.return_value = {"parameters": {"retry_after": 7}}
    ok = MagicMock(status_code=200, ok=True)
    with patch("telegram_notifier.requests.post", side_effect=[limited, ok]), \
         patch("telegram_notifier.time.sleep") as sleep:
        assert telegram_notifier._send("t", "c", "hi") is True
    sleep.assert_called_once_with(7)


def test_send_blocks_paces_messages_and_never_splits_a_block(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_notifier, "send_message", lambda m: sent.append(m) or True)
    monkeypatch.setattr(telegram_notifier.time, "sleep", lambda _s: None)
    blocks = [f"*CO {i}*\n" + "x" * 1500 for i in range(5)]

    assert telegram_notifier.send_blocks("H\n\n", blocks) is True

    assert len(sent) == 3
    assert all(m.startswith("H\n\n") and len(m) <= telegram_notifier.MSG_LIMIT for m in sent)
    assert sum(m.count("*CO ") for m in sent) == 5
