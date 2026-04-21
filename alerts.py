"""Insider-trading alert classification for CVM VLMO consolidated data."""

import pandas as pd
import yaml
from pathlib import Path
from typing import List

# Position types considered C-level / control-related (P1 eligible)
EXEC_POSITIONS = {
    'Controlador ou Vinculado',
    'Conselho de Administração ou Vinculado',
    'Diretor ou Vinculado',
}

# Movement types that constitute a sell (used for blackout rule)
# Matches "Venda à vista", "Venda à termo", "Venda" but not "Compra à venda"
_SELL_PREFIX = 'Venda'

P1_VOLUME_THRESHOLD  = 500_000
P2_MONTHLY_THRESHOLD = 2_000_000
BLACKOUT_DAYS        = 30


def load_coverage(config_path: str = 'config/coverage.yaml') -> List[str]:
    """Load the list of monitored company name substrings from YAML."""
    with open(config_path) as f:
        return yaml.safe_load(f)['companies']


def _in_coverage(company_name: str, coverage: List[str]) -> bool:
    upper = str(company_name).upper()
    return any(c.upper() in upper for c in coverage)


def classify_alerts(delta_df: pd.DataFrame, coverage_tickers: List[str]) -> pd.DataFrame:
    """Classify delta records into alert priorities.

    Rules (highest wins; reasons concatenated when multiple same-priority rules match):
      P1 — Controlador/C-level/Conselheiro AND volume > R$ 500k
      P1 — Sell by any of the above within 30 days before Reference_Date (blackout proxy)
      P2 — Monthly aggregate volume by insider > R$ 2M
      P3 — Any operation in a coverage ticker

    Parameters
    ----------
    delta_df : DataFrame
        Output of delta.detect_new_records(); expected columns from the VLMO consolidated schema.
    coverage_tickers : list[str]
        Substrings of Company_Name to match (case-insensitive). Load via load_coverage().

    Returns
    -------
    DataFrame with the same columns as delta_df plus 'priority' ('P1'/'P2'/'P3') and 'reason'.
    Only rows that triggered at least one rule are returned, sorted by priority then Company_Name.
    """
    if delta_df.empty:
        return delta_df.assign(priority=pd.Series(dtype=str), reason=pd.Series(dtype=str))

    df = delta_df.copy()
    df['Movement_Date']  = pd.to_datetime(df['Movement_Date'])
    df['Reference_Date'] = pd.to_datetime(df['Reference_Date'])
    df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce').fillna(0)

    vol_abs   = df['Volume'].abs()
    days_gap  = (df['Reference_Date'] - df['Movement_Date']).dt.days  # positive = Mov before Ref

    # Monthly volume per insider vehicle + listed company
    df['_ym'] = df['Movement_Date'].dt.to_period('M')
    monthly_vol = df.groupby(['Company_CNPJ', 'Company', '_ym'])['Volume'].transform(
        lambda x: x.abs().sum()
    )

    # Boolean masks
    is_exec       = df['Position_Type'].isin(EXEC_POSITIONS)
    is_sell       = df['Movement_Type'].str.startswith(_SELL_PREFIX, na=False)
    high_vol      = vol_abs > P1_VOLUME_THRESHOLD
    in_blackout   = days_gap.between(0, BLACKOUT_DAYS)
    high_monthly  = monthly_vol > P2_MONTHLY_THRESHOLD
    in_coverage   = df['Company_Name'].apply(lambda n: _in_coverage(n, coverage_tickers))

    # Per-row reason strings for P1 rules (need row-level data)
    p1a_reason = (
        'Insider ' + df['Position_Type'].astype(str)
        + ' com volume R$ ' + vol_abs.map(lambda v: f'{v:,.0f}')
        + f' > {P1_VOLUME_THRESHOLD:,.0f}'
    )
    p1b_reason = (
        'Venda em blackout ≤' + str(BLACKOUT_DAYS)
        + 'd antes de Reference_Date por ' + df['Position_Type'].astype(str)
    )

    # Rule registry: (priority_int, reason_series_or_str, mask)
    # Processed in ascending priority order so higher-priority rules can override lower ones.
    rules = [
        (3, pd.Series('Operação em empresa da cobertura', index=df.index), in_coverage),
        (2, pd.Series(f'Volume mensal por insider > R$ {P2_MONTHLY_THRESHOLD:,.0f}', index=df.index), high_monthly),
        (1, p1a_reason, is_exec & high_vol),
        (1, p1b_reason, is_exec & is_sell & in_blackout),
    ]

    prio_int = pd.Series(99, index=df.index)   # 99 = no alert
    reason   = pd.Series('', index=df.index, dtype=object)

    for p, r_series, mask in rules:
        affected = df.index[mask]
        if affected.empty:
            continue

        curr = prio_int[affected]
        better = curr > p   # new rule has higher priority
        equal  = curr == p  # same priority — append reason

        better_idx = affected[better.values]
        equal_idx  = affected[equal.values]

        prio_int[better_idx] = p
        reason[better_idx]   = r_series[better_idx]

        # Append reason when two same-priority rules fire on the same row
        for i in equal_idx:
            reason[i] = f"{reason[i]}; {r_series[i]}"

    # Build result: only rows that got at least one alert
    has_alert = prio_int < 99
    int_to_str = {1: 'P1', 2: 'P2', 3: 'P3'}

    result = df[has_alert].drop(columns=['_ym']).copy()
    result['priority'] = prio_int[has_alert].map(int_to_str).values
    result['reason']   = reason[has_alert].values

    return result.sort_values(['priority', 'Company_Name']).reset_index(drop=True)
