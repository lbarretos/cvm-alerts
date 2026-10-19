"""Telegram dispatcher shared by the IPE watcher and the weekly VLMO summary."""

import os
import logging
import time

import requests

TELEGRAM_API = 'https://api.telegram.org/bot{token}/sendMessage'
MSG_LIMIT = 3800   # hard limit is 4096; keep margin for headers
SEND_INTERVAL = 1.5      # seconds between messages to one chat (limit ~20/min in groups)
MAX_RETRY_AFTER = 120    # cap on a single 429 back-off

logger = logging.getLogger(__name__)


def _get_all_recipients() -> list[tuple[str, str]]:
    """Return list of (token, chat_id) pairs from environment.

    Supports multiple recipients via:
    - Comma-separated TELEGRAM_CHAT_ID (same bot, multiple chats)
    - TELEGRAM_BOT_TOKEN_2 + TELEGRAM_CHAT_ID_2 (second bot, optional)
    - TELEGRAM_BOT_TOKEN_N + TELEGRAM_CHAT_ID_N (N-th bot, optional)

    Raises KeyError if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are absent.
    """
    recipients: list[tuple[str, str]] = []

    token = os.environ['TELEGRAM_BOT_TOKEN']
    for cid in os.environ['TELEGRAM_CHAT_ID'].split(','):
        cid = cid.strip()
        if cid:
            recipients.append((token, cid))

    i = 2
    while True:
        token2 = os.environ.get(f'TELEGRAM_BOT_TOKEN_{i}')
        cid2   = os.environ.get(f'TELEGRAM_CHAT_ID_{i}')
        if not token2 or not cid2:
            break
        for cid in cid2.split(','):
            cid = cid.strip()
            if cid:
                recipients.append((token2, cid))
        i += 1

    return recipients


def _get_credentials() -> tuple[str, str]:
    """Read primary bot credentials from environment. Raises KeyError if absent."""
    return os.environ['TELEGRAM_BOT_TOKEN'], os.environ['TELEGRAM_CHAT_ID']


def _post(url: str, payload: dict) -> requests.Response:
    """POST once; on HTTP 429 wait the advertised retry_after and retry once."""
    resp = requests.post(url, json=payload, timeout=10)
    if resp.status_code == 429:
        try:
            wait = int(resp.json().get('parameters', {}).get('retry_after', 5))
        except ValueError:
            wait = 5
        wait = min(wait, MAX_RETRY_AFTER)
        logger.warning(f"Telegram rate limit, waiting {wait}s")
        time.sleep(wait)
        resp = requests.post(url, json=payload, timeout=10)
    return resp


def _send(token: str, chat_id: str, text: str) -> bool:
    """Send one message. Returns True on success.

    If Telegram rejects the Markdown (HTTP 400, e.g. an unbalanced '*' or '_'
    in LLM output or a cut at MSG_LIMIT), resend as plain text rather than
    lose the message.
    """
    url = TELEGRAM_API.format(token=token)
    try:
        resp = _post(url, {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'})
        if resp.status_code == 400:
            logger.warning(f"Telegram rejected Markdown, resending as plain text: {resp.text[:200]}")
            resp = _post(url, {'chat_id': chat_id, 'text': text})
        if not resp.ok:
            logger.warning(f"Telegram error {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except requests.RequestException as exc:
        logger.warning(f"Telegram request failed: {exc}")
        return False


def _chunk_messages(header: str, blocks: list, limit: int, separator: str = '\n\n') -> list:
    """Split blocks into messages that fit within the Telegram char limit.

    Every message starts with `header`; a block is never split.
    """
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


def send_blocks(header: str, blocks: list[str]) -> bool:
    """Send blocks as as few messages as fit, paced to stay under Telegram's
    per-chat rate limit. Returns True only if every message reached every
    recipient. Raises KeyError if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are
    not set.
    """
    ok = True
    for i, msg in enumerate(_chunk_messages(header, blocks, MSG_LIMIT)):
        if i:
            time.sleep(SEND_INTERVAL)
        ok = send_message(msg) and ok
    return ok


def send_message(text: str) -> bool:
    """Send a single free-form Markdown message to all configured Telegram recipients.

    Truncates to MSG_LIMIT characters. Network errors are logged, not raised.
    Returns True only if every recipient received the message.
    Raises KeyError if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not set.
    """
    results = [_send(token, chat_id, text[:MSG_LIMIT])
               for token, chat_id in _get_all_recipients()]
    return all(results)
