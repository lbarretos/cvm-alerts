import pandas as pd
from pathlib import Path
from typing import Optional

PRIMARY_KEY = ['Company_CNPJ', 'Movement_Date', 'Position_Type', 'Operation_Type', 'Quantity', 'Unit_Price']

_BACKUP_GLOB = 'Brazil_Stock_Trading_Consolidated_*.csv'


def latest_backup(backup_dir: Path) -> Optional[Path]:
    """Return the most recent backup CSV in backup_dir, or None if none exist."""
    files = sorted(Path(backup_dir).glob(_BACKUP_GLOB))
    return files[-1] if files else None


def _normalize_keys(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of PRIMARY_KEY columns with consistent dtypes for safe merging."""
    keys = df[PRIMARY_KEY].copy()
    keys['Movement_Date'] = pd.to_datetime(keys['Movement_Date'])
    keys['Quantity'] = pd.to_numeric(keys['Quantity'], errors='coerce')
    keys['Unit_Price'] = pd.to_numeric(keys['Unit_Price'], errors='coerce')
    for col in ('Company_CNPJ', 'Position_Type', 'Operation_Type'):
        keys[col] = keys[col].astype(str).str.strip()
    return keys


def detect_new_records(current_df: pd.DataFrame, previous_csv_path: Optional[Path]) -> pd.DataFrame:
    """Return rows in current_df whose primary key is absent in previous_csv_path.

    First-run semantics: if previous_csv_path is None or does not exist, the full
    current_df is returned (every record is considered new).
    Does not mutate current_df.
    """
    if previous_csv_path is None or not Path(previous_csv_path).exists():
        return current_df.copy()

    prev = pd.read_csv(previous_csv_path, encoding='utf-8-sig')

    curr_keys = _normalize_keys(current_df)
    prev_keys = _normalize_keys(prev).drop_duplicates()

    merged = curr_keys.merge(prev_keys, on=PRIMARY_KEY, how='left', indicator=True)
    new_mask = (merged['_merge'] == 'left_only').values

    return current_df[new_mask].reset_index(drop=True)
