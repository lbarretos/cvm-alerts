"""Insider-trading highlights for the coverage list (CVM VLMO consolidated data).

Weekly summary rules:
  - Only coverage companies (config/cnpj_map.yaml, matched by CNPJ).
  - Only the latest disclosed reference month (see latest_disclosed_month).
  - Only buys, sells and buybacks of shares/units, aggregated per company,
    position and direction.
  - A company is listed when it has a P1 or P2 highlight:
      P1 — Controlador / Conselho de Administração / Diretor line > R$ 500k
      P2 — company's total traded volume in the month > R$ 2M
  - A company is sent once per reference month, and again only if its
    figures change (late filing, new version).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

EXEC_POSITIONS = {
    'Controlador ou Vinculado',
    'Conselho de Administração ou Vinculado',
    'Diretor ou Vinculado',
}

P1_VOLUME_THRESHOLD  = 500_000
P2_MONTHLY_THRESHOLD = 2_000_000

EQUITY_ASSETS = {'Ações', 'Units'}

MONTHS_PT = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
             "agosto", "setembro", "outubro", "novembro", "dezembro"]

# Monthly VLMO forms are due 10 days after month end (Res. CVM 44).
FILING_DEADLINE_DAYS = 10


# ── Shared helpers (also used by ipe_watcher for the daily VLMO notice) ──────

def latest_disclosed_month(today: date) -> pd.Period:
    """Reference month whose filings are the latest due as of `today`.

    Until the 10th, last month's forms are still arriving, so the month
    before it is the latest fully disclosed one; from the 11th on, it is
    last month.
    """
    shifted = today - timedelta(days=FILING_DEADLINE_DAYS)
    return pd.Period(shifted, freq='M') - 1


def month_label(period: pd.Period) -> str:
    return f"{MONTHS_PT[period.month - 1]}/{period.year}"


def short_position(position: str) -> str:
    """'Conselho de Administração ou Vinculado' → 'Conselho de Administração'."""
    p = str(position).strip()
    if p.lower().startswith('controlador'):
        return 'Controlador'
    if p.lower().startswith('diret'):
        return 'Diretor'
    return p.removesuffix(' ou Vinculado').strip() or 'Outros'


def _fmt_int(n: float) -> str:
    return f"{n:,.0f}".replace(',', '.')


def _fmt_brl(v: float) -> str:
    v = abs(v)
    if v >= 1_000_000_000:
        return f"R$ {v / 1_000_000_000:.1f} bi".replace('.', ',')
    if v >= 1_000_000:
        return f"R$ {v / 1_000_000:.1f} mi".replace('.', ',')
    if v >= 1_000:
        return f"R$ {v / 1_000:.0f} mil"
    return f"R$ {v:.0f}"


def _fmt_price(p: float) -> str:
    return f"R$ {p:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')


def format_trade_line(position: str, direction: str, quantity: float, volume: float) -> str:
    """'• Controlador comprou 1.000 ações, valor R$ 50 mil, preço médio R$ 50,00'."""
    avg = volume / quantity if quantity else 0.0
    tail = f"{_fmt_int(quantity)} ações, valor {_fmt_brl(volume)}, preço médio {_fmt_price(avg)}"
    if direction == 'recompra':
        return f"• Recompra de {tail}"
    verb = 'comprou' if direction == 'compra' else 'vendeu'
    return f"• {short_position(position)} {verb} {tail}"


# ── Weekly summary ───────────────────────────────────────────────────────────

def load_coverage_map(path: str = 'config/cnpj_map.yaml') -> dict[str, dict]:
    """Return CNPJ → {tickers, denom} for the coverage list."""
    with open(path, encoding='utf-8') as f:
        tickers = yaml.safe_load(f)['tickers']
    result: dict[str, dict] = {}
    for ticker, info in tickers.items():
        entry = result.setdefault(info['cnpj'], {'tickers': [], 'denom': info['denom']})
        entry['tickers'].append(ticker)
    return result


def _direction(row: pd.Series) -> str | None:
    if 'recompra' in str(row.get('Movement_Description', '')).lower():
        return 'recompra'
    mov = str(row.get('Movement_Type', '')).lower()
    if mov.startswith('compra'):
        return 'compra'
    if mov.startswith('venda'):
        return 'venda'
    return None  # loans, grants, inheritance, etc. are not market trades


def trade_lines(df: pd.DataFrame, coverage: dict[str, dict], month: pd.Period) -> pd.DataFrame:
    """Aggregate coverage trades of `month` per company, position and direction."""
    if df.empty:
        return pd.DataFrame(columns=['Company_CNPJ', 'Position_Type', 'direction', 'Quantity', 'Volume'])
    d = df[df['Company_CNPJ'].isin(coverage)].copy()
    d = d[pd.to_datetime(d['Reference_Date']).dt.to_period('M') == month]
    d = d[d['Asset_Type'].isin(EQUITY_ASSETS)]
    d['direction'] = d.apply(_direction, axis=1)
    d = d[d['direction'].notna()]
    d['Quantity'] = pd.to_numeric(d['Quantity'], errors='coerce').fillna(0).abs()
    d['Volume'] = pd.to_numeric(d['Volume'], errors='coerce').fillna(0).abs()
    # Zero-value "purchases" are grants/transfers at no price, not trades.
    d = d[d['Volume'] > 0]
    # Buybacks are reported under the insider who executed them; group them
    # together regardless of position.
    d.loc[d['direction'] == 'recompra', 'Position_Type'] = ''
    return (d.groupby(['Company_CNPJ', 'Position_Type', 'direction'], as_index=False)
             [['Quantity', 'Volume']].sum())


def highlights(lines: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Return CNPJ → its trade lines, for companies with a P1 or P2 highlight."""
    result = {}
    for cnpj, g in lines.groupby('Company_CNPJ'):
        p1 = (g['Position_Type'].isin(EXEC_POSITIONS) & (g['Volume'] > P1_VOLUME_THRESHOLD)).any()
        p2 = g['Volume'].sum() > P2_MONTHLY_THRESHOLD
        if p1 or p2:
            result[cnpj] = g.sort_values('Volume', ascending=False)
    return result


def _fingerprint(g: pd.DataFrame) -> str:
    rows = sorted(f"{r.Position_Type}|{r.direction}|{r.Quantity:.0f}|{r.Volume:.0f}"
                  for r in g.itertuples())
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:16]


def company_block(cnpj: str, g: pd.DataFrame, coverage: dict[str, dict], updated: bool) -> str:
    info = coverage[cnpj]
    denom = " ".join(info['denom'].split())
    title = f"*{denom}* ({'/'.join(info['tickers'])})"
    if updated:
        title += " — atualizado"
    body = [format_trade_line(r.Position_Type, r.direction, r.Quantity, r.Volume)
            for r in g.itertuples()]
    return "\n".join([title, *body])


def weekly_summary(df: pd.DataFrame, coverage: dict[str, dict], today: date,
                   state: dict[str, str]) -> tuple[str, list[str], dict[str, str]]:
    """Build the weekly summary.

    Returns (header, company_blocks, new_state). Blocks are only produced for
    companies whose highlights are new or changed versus `state`; new_state
    holds only the current month's fingerprints.
    """
    month = latest_disclosed_month(today)
    picked = highlights(trade_lines(df, coverage, month))
    new_state: dict[str, str] = {}
    blocks = []
    for cnpj, g in sorted(picked.items(), key=lambda kv: coverage[kv[0]]['denom']):
        key = f"{cnpj}|{month}"
        fp = _fingerprint(g)
        new_state[key] = fp
        if state.get(key) == fp:
            continue
        blocks.append(company_block(cnpj, g, coverage, updated=key in state))
    header = f"📊 *Insiders — {month_label(month)}* (destaques P1/P2 da cobertura)\n\n"
    return header, blocks, new_state


def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, sort_keys=True)
