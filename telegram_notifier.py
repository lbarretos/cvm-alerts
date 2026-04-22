"""Telegram alert dispatcher for CVM insider-trading alerts."""

import os
import logging
import requests
import pandas as pd

TELEGRAM_API = 'https://api.telegram.org/bot{token}/sendMessage'
MSG_LIMIT = 3800   # hard limit is 4096; keep margin for headers

logger = logging.getLogger(__name__)


def _get_credentials() -> tuple:
    """Read bot credentials from environment. Raises KeyError if absent."""
    token   = os.environ['TELEGRAM_BOT_TOKEN']
    chat_id = os.environ['TELEGRAM_CHAT_ID']
    return token, chat_id


def _format_alert(row: pd.Series) -> str:
    pos_type = row['Position_Type'] if pd.notna(row.get('Position_Type')) else 'N/A'
    try:
        qty = f"{int(row['Quantity']):,}"
    except (ValueError, TypeError):
        qty = str(row['Quantity'])
    return (
        f"*{row['priority']} - {row['Company_CNPJ']}*\n"
        f"{row['Company_Name']}\n"
        f"{pos_type}: {row['Operation_Type']} {qty} @ {row['Unit_Price']:.2f}\n"
        f"Volume: R$ {abs(row['Volume']):,.0f}\n"
        f"Razão: {row['reason']}"
    )


def _send(token: str, chat_id: str, text: str) -> None:
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
            timeout=10,
        )
        if not resp.ok:
            logger.warning(f"Telegram error {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as exc:
        logger.warning(f"Telegram request failed: {exc}")


def _chunk_messages(header: str, blocks: list, limit: int) -> list:
    """Split alert blocks into messages that fit within the Telegram char limit."""
    separator = '\n\n---\n\n'
    messages = []
    current = header
    for block in blocks:
        candidate = current + (separator if current != header else '') + block
        if len(candidate) > limit and current != header:
            messages.append(current)
            current = header + block
        else:
            current = candidate
    if current != header:
        messages.append(current)
    return messages


def send_alerts(alerts_df: pd.DataFrame) -> None:
    """Send Telegram messages grouped by priority (P1 → P2 → P3).

    Does nothing if alerts_df is empty or has no rows.
    Network errors are logged as warnings and do not raise.
    Raises KeyError if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not set.
    """
    if alerts_df is None or alerts_df.empty:
        return

    token, chat_id = _get_credentials()

    for priority in ('P1', 'P2', 'P3'):
        group = alerts_df[alerts_df['priority'] == priority]
        if group.empty:
            continue

        header = f"⚠️ *Alertas {priority} ({len(group)} registro(s))*\n\n"
        blocks = [_format_alert(row) for _, row in group.iterrows()]

        for msg in _chunk_messages(header, blocks, MSG_LIMIT):
            _send(token, chat_id, msg)


def send_message(text: str) -> None:
    """Send a single free-form Markdown message to the configured Telegram chat.

    Truncates to MSG_LIMIT characters. Network errors are logged, not raised.
    Raises KeyError if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not set.
    """
    token, chat_id = _get_credentials()
    _send(token, chat_id, text[:MSG_LIMIT])
