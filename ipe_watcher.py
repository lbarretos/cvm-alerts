"""CVM IPE filing monitor — hourly run, Telegram + Deepseek LLM alerts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import pdfplumber
import requests
import yaml
from openai import OpenAI

from utils import create_session
from telegram_notifier import send_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class CredentialError(Exception):
    """Raised when the B3 API rejects the CVM credentials permanently.

    Extends Exception (not RuntimeError) so a bare ``except RuntimeError``
    cannot accidentally swallow it — it must be caught explicitly.

    Unlike transient network errors, credential failures will not recover
    on retry — the run should exit non-zero immediately so GitHub Actions
    (and the operator) are notified.
    """

class B3RequestError(Exception):
    """Raised when the B3 API rejects our request parameters (errors 22014,
    22015, 22017). These indicate a bug in this script, not a transient
    failure, so the run must stop and alert like a credential error.
    """

# ── Paths ──────────────────────────────────────────────────────────────────────
PROCESSED_IDS_FILE  = Path("datasets/ipe_processed_ids.json")
LAST_RUN_DATE_FILE  = Path("datasets/ipe_last_run_date.txt")
SKIPPED_LOG_FILE   = Path("datasets/ipe_skipped_log.csv")
INDEX_LATEST_FILE  = Path("datasets/ipe_index_latest.csv")
CNPJ_MAP_FILE      = Path("config/cnpj_map.yaml")
SYSTEM_PROMPT_FILE = Path("config/system_prompt_ipe.txt")

B3_API_URL = "https://seguro.bmfbovespa.com.br/rad/download/SolicitaDownload.asp"

# B3 API error numbers, per the CVM technical note (manual_tecnico.html, §4.2):
#   1     = LOGIN INCORRETO (authentication; SENHA EXPIRADA and USUÁRIO SUSPENSO
#           are observed in practice — matched by description as a fallback)
#   22013 = error generating the XML (server-side, transient)
#   22014 = invalid date, 22015 = invalid time, 22017 = invalid document type
#   22016 = no records found (not an error for us: empty result)
B3_ERR_CREDENTIAL = "1"
B3_ERR_TRANSIENT = "22013"
B3_ERR_NO_RECORDS = "22016"
B3_ERR_BAD_REQUEST = {"22014", "22015", "22017"}
# Descriptions that mean permanent credential/access failure. Matched as
# substrings after accent-stripping and uppercasing.
B3_CREDENTIAL_ERRORS = ("SENHA EXPIRADA", "LOGIN INCORRETO", "USUARIO SUSPENSO")
B3_MAX_ATTEMPTS = 3

# Password maintenance, per the CVM technical note §5.
PASSWORD_HELP = (
    "renovar senha em https://cas.bovespa.com.br/cas/ (menu \"Senhas\") "
    "ou em https://www.rad.cvm.gov.br/ENET (\"Alteração de senha\"); "
    "depois atualizar o secret CVM\\_PASSWORD"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CATEGORIES = frozenset([
    "Fato Relevante",
    "Comunicado ao Mercado",
    "Comunicado aos Acionistas",
    "Aviso aos Acionistas",
])

VLMO_CATEGORY = "Valores Mobiliários Negociados e Detidos"
ALL_WATCHED_CATEGORIES = CATEGORIES | {VLMO_CATEGORY}

# ── VLMO PDF parsing patterns ───────────────────────────────────────────────────

_VLMO_POS_RE = re.compile(
    r'\(\s*[xX]\s*\)\s*'
    r'(Controlador[^\n]*|Conselho\s+de\s+Administra[çc][aã]o[^\n]*|'
    r'Conselho\s+Fiscal[^\n]*|Diret[^\n]*|[Óo]rg[aã]os?\s+T[eé]cnicos?[^\n]*)',
    re.IGNORECASE,
)
_VLMO_OP_START_RE = re.compile(
    r'^(Compra|Venda|Transfer[eê]ncia|Bonifica[çc][aã]o|'
    r'Subscri[çc][aã]o|Doa[çc][aã]o|Heran[çc]a|Exerc[ií]cio)',
    re.IGNORECASE,
)
_VLMO_OP_END_RE = re.compile(
    r'^(à\s+vista|a\s+vista|vista|à\s+termo|a\s+termo|termo|firme)',
    re.IGNORECASE,
)
_VLMO_TXN_RE = re.compile(r'(\d{1,2})\s+([\d.]+)\s+([\d.,]+)\s+([\d.,]+)\s*$')

# ── CNPJ helpers ──────────────────────────────────────────────────────────────

def _digits(cnpj: str) -> str:
    return re.sub(r"\D", "", str(cnpj))


def load_company_map() -> dict[str, dict]:
    """Return dict ccvm → {cnpj, cnpj_digits, denom, ticker} from config/cnpj_map.yaml."""
    with open(CNPJ_MAP_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    result = {}
    for ticker, info in data.get("tickers", {}).items():
        ccvm = str(info["ccvm"])
        cnpj = info["cnpj"]
        result[ccvm] = {
            "ticker":      ticker,
            "cnpj":        cnpj,
            "cnpj_digits": _digits(cnpj),
            "denom":       info["denom"],
        }
    return result


# ── B3 API index download ──────────────────────────────────────────────────────

def _parse_dataref(dataref: str) -> str:
    """Convert B3 DataRef 'dd/mm/yyyy hh:mm:ss' to ISO 'yyyy-mm-dd'."""
    try:
        return datetime.strptime(dataref[:10], "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return dataref


def _normalize(text: str) -> str:
    """Uppercase and strip accents: 'Usuário suspenso' → 'USUARIO SUSPENSO'."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip().upper()


_EMPTY_COLUMNS = ["ccvm", "DT_REF", "CATEG_DOC", "TIPO_DOC", "LINK_DOC", "ASSUNTO", "Situacao"]


def _query_b3(session, payload: dict) -> ET.Element:
    """POST to the B3 API and return the parsed XML root.

    Retries transport errors, unparseable responses (e.g. an HTML maintenance
    page) and error 22013. Never retries authentication errors, so a wrong
    password cannot lock the account (USUÁRIO SUSPENSO).
    """
    for attempt in range(1, B3_MAX_ATTEMPTS + 1):
        try:
            resp = session.post(B3_API_URL, data=payload, timeout=60)
            resp.raise_for_status()
            # Pass bytes: the parser honours the ISO-8859-1 declaration.
            root = ET.fromstring(resp.content)
            if root.tag == "ERROS" and root.findtext("NUMERO_DO_ERRO", "").strip() == B3_ERR_TRANSIENT:
                raise RuntimeError(f"B3 API error {B3_ERR_TRANSIENT}: "
                                   f"{root.findtext('DESCRICAO_DO_ERRO', '').strip()}")
            return root
        except (requests.RequestException, ET.ParseError, RuntimeError) as exc:
            if attempt == B3_MAX_ATTEMPTS:
                raise
            logger.warning("B3 API attempt %d failed: %s — retrying", attempt, exc)
            time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def download_ipe_b3(session, query_date: date) -> pd.DataFrame:
    """Query B3 Multiple Download API for IPE filings on query_date.

    The API returns links whose *last action* (release, block, cancel) falls
    in the 24h window starting at txtData/txtHora, so a daily query plus
    hash dedup catches every release exactly once.

    Returns DataFrame with columns: ccvm, DT_REF, CATEG_DOC, TIPO_DOC,
    LINK_DOC, ASSUNTO, Situacao. DT_REF is the document's reference date
    (DataRef), not its delivery date.
    """
    login = os.environ.get("CVM_USERNAME", "")
    senha = os.environ.get("CVM_PASSWORD", "")
    if not login or not senha:
        raise EnvironmentError("CVM_USERNAME and CVM_PASSWORD environment variables must be set")

    date_str = query_date.strftime("%d/%m/%Y")
    logger.info("Querying B3 API for IPE filings on %s", date_str)

    payload = {
        "txtLogin":      login,
        "txtSenha":      senha,
        "txtData":       date_str,
        "txtHora":       "00:00",
        "txtDocumento":  "IPE",
        "txtAssuntoIPE": "SIM",
    }
    root = _query_b3(session, payload)

    if root.tag == "ERROS":
        code = root.findtext("NUMERO_DO_ERRO", "").strip()
        desc = root.findtext("DESCRICAO_DO_ERRO", "").strip()
        if code == B3_ERR_NO_RECORDS:
            logger.info("B3 API: no records for %s", date_str)
            return pd.DataFrame(columns=_EMPTY_COLUMNS)
        exc = RuntimeError(f"B3 API error {code}: {desc}")
        norm = _normalize(desc)
        if code == B3_ERR_CREDENTIAL or any(e in norm for e in B3_CREDENTIAL_ERRORS):
            raise CredentialError(desc or f"erro {code}") from exc
        if code in B3_ERR_BAD_REQUEST:
            raise B3RequestError(f"B3 API error {code}: {desc}") from exc
        raise exc

    rows = []
    for link in root.findall("Link"):
        rows.append({
            "ccvm":      link.get("ccvm", ""),
            "DT_REF":    _parse_dataref(link.get("DataRef", "")),
            "CATEG_DOC": link.get("Categoria", ""),
            "TIPO_DOC":  link.get("Tipo", ""),
            "LINK_DOC":  link.get("url", ""),
            "ASSUNTO":   link.get("Especie", ""),
            "Situacao":  link.get("Situacao", ""),
        })

    logger.info("B3 API returned %d IPE records for %s", len(rows), date_str)

    if not rows:
        return pd.DataFrame(columns=_EMPTY_COLUMNS)

    df = pd.DataFrame(rows)

    # Drop cancelled documents
    df = df[df["Situacao"] == "Liberado"].copy()

    return df


# ── Pre-filter ────────────────────────────────────────────────────────────────

_FORCE_KEYWORDS = frozenset([
    "aquisição", "aquisicao", "fusão", "fusao", "incorporação", "incorporacao",
    "desinvestimento", "recuperação judicial", "recuperacao judicial",
    "inadimplemento", "waiver", "covenant", "guidance", "revisão", "revisao",
    "cancelamento", "oferta", "opa", "follow-on", "emissão", "emissao",
    "captação", "captacao", "ceo", "cfo", "diretor", "renúncia", "renuncia",
    "afastamento", "investigação", "investigacao", "cvm", "autuação", "autuacao",
])


def _has_force_keyword(assunto_lower: str) -> bool:
    return any(kw in assunto_lower for kw in _FORCE_KEYWORDS)


def pre_filter(row: pd.Series) -> tuple[bool, str]:
    """Return (should_process, skip_reason). Force rules take precedence."""
    categ   = str(row.get("CATEG_DOC", "")).strip()
    assunto = str(row.get("ASSUNTO", "")).strip()
    al      = assunto.lower()

    # Force: always process
    if categ in ("Fato Relevante", "Comunicado ao Mercado"):
        return True, ""
    if _has_force_keyword(al):
        return True, ""

    # Skip: Ata AGO/AGE without material agenda
    if re.search(r"\bata\b", al) and re.search(r"\b(ago|age)\b", al):
        return False, "ata de AGO/AGE sem pauta material"

    # Skip: Aviso sobre pagamento/crédito ou calendário
    if categ == "Aviso aos Acionistas":
        if "pagamento" in al or "crédito" in al or "credito" in al:
            return False, "pagamento/crédito de JCP/dividendo já anunciado"
        if "calendário de eventos" in al or "calendario de eventos" in al:
            return False, "calendário de eventos corporativos"

    # Skip: Política de divulgação
    if "política de divulgação" in al or "politica de divulgacao" in al:
        return False, "política de divulgação — revisão anual"

    # Skip: Código de conduta
    if "código de conduta" in al or "codigo de conduta" in al:
        return False, "código de conduta — revisão anual"

    # Skip: Estatuto social sem alteração de capital/controle
    if "estatuto social" in al:
        if not ("capital" in al or "controle" in al):
            return False, "estatuto social — atualização sem alteração de capital/controle"

    return True, ""


def _log_skipped(row: pd.Series, reason: str) -> None:
    SKIPPED_LOG_FILE.parent.mkdir(exist_ok=True)
    write_header = not SKIPPED_LOG_FILE.exists()
    with open(SKIPPED_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["data", "empresa", "tipo", "motivo"])
        writer.writerow([
            row.get("DT_REF", ""),
            row.get("DENOM_CIA", ""),
            row.get("CATEG_DOC", ""),
            reason,
        ])


# ── Dedup ─────────────────────────────────────────────────────────────────────

def make_hash(row: pd.Series) -> str:
    # Field values (not names) feed the hash, so the DT_ENTREGA → DT_REF
    # rename keeps hashes of already-processed documents unchanged.
    raw = "|".join([
        str(row.get("CNPJ_CIA", "")),
        str(row.get("DT_REF", "")),
        str(row.get("TIPO_DOC", "")),
        str(row.get("LINK_DOC", "")),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


def load_processed_ids() -> set[str]:
    if not PROCESSED_IDS_FILE.exists():
        return set()
    with open(PROCESSED_IDS_FILE, encoding="utf-8") as f:
        return set(json.load(f))


def save_processed_ids(ids: set[str]) -> None:
    PROCESSED_IDS_FILE.parent.mkdir(exist_ok=True)
    with open(PROCESSED_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)


def load_last_run_date() -> date:
    """Return date of last successful run, or yesterday if no record exists."""
    if LAST_RUN_DATE_FILE.exists():
        return date.fromisoformat(LAST_RUN_DATE_FILE.read_text().strip())
    return datetime.now(ZoneInfo("America/Sao_Paulo")).date() - timedelta(days=1)


def save_last_run_date(d: date) -> None:
    LAST_RUN_DATE_FILE.parent.mkdir(exist_ok=True)
    LAST_RUN_DATE_FILE.write_text(d.isoformat())


# ── PDF download + text extraction ────────────────────────────────────────────

def download_pdf(session, url: str) -> bytes | None:
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(2):
        try:
            resp = session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            logger.warning("PDF download attempt %d failed: %s — %s", attempt + 1, url, exc)
            if attempt == 0:
                time.sleep(2)
    return None


def extract_text(pdf_bytes: bytes) -> str:
    """Extract up to 4000 characters from the first pages of a PDF."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            texts = []
            total = 0
            for page in pdf.pages:
                t = page.extract_text() or ""
                texts.append(t)
                total += len(t)
                if total >= 4000:
                    break
            return "".join(texts)[:4000]
    except Exception as exc:
        logger.warning("pdfplumber failed: %s", exc)
        return ""


# ── VLMO parsing ──────────────────────────────────────────────────────────────

def _parse_brl_number(s: str) -> float:
    """Parse Brazilian-format number (e.g. '1.480.783,68') to float."""
    return float(s.strip().replace(".", "").replace(",", "."))


def _infer_pos_type(pos_type: str, asset: str) -> str:
    """Return position type, inferring from asset name when pos_type is empty."""
    if pos_type:
        return pos_type
    asset_up = asset.upper()
    if "RECOMPRA" in asset_up:
        return "Recompra"
    if "SUBSCRI" in asset_up:
        return "Subscrição"
    return "Outros"


def _format_amount(v: float) -> str:
    """Format BRL amount compactly: R$ 1.4M, R$ 500K, R$ 13K."""
    abs_v = abs(v)
    if abs_v >= 1_000_000:
        return f"R$ {abs_v / 1_000_000:.1f}M"
    if abs_v >= 1_000:
        return f"R$ {abs_v / 1_000:.0f}K"
    return f"R$ {abs_v:,.0f}"


def _parse_vlmo_page(text: str) -> list[dict]:
    """Parse one VLMO PDF page into a list of transaction dicts."""
    lines = [l.rstrip() for l in text.splitlines()]

    pos_type = ""
    for line in lines:
        m = _VLMO_POS_RE.search(line)
        if m:
            # Strip trailing unchecked checkbox groups on the same line
            # e.g. "Controlador ( ) Conselho de Adm..." → "Controlador"
            pos_type = re.sub(r"\s*\(.*", "", m.group(1)).strip()
            break

    transactions: list[dict] = []
    pending_op = ""

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        if _VLMO_OP_START_RE.match(stripped):
            pending_op = stripped
            continue

        m = _VLMO_TXN_RE.search(line)
        if m:
            day_str, qty_str, price_str, vol_str = m.groups()
            asset = line[:m.start()].strip()

            op_end = ""
            for j in range(i + 1, min(i + 3, len(lines))):
                nxt = lines[j].strip()
                if nxt and _VLMO_OP_END_RE.match(nxt):
                    op_end = nxt
                    break

            op_type = (pending_op + (" " + op_end if op_end else "")).strip() or "N/A"

            try:
                qty = _parse_brl_number(qty_str)
                vol = _parse_brl_number(vol_str)
                # Skip spurious matches: zero volume or unparsed operation
                if vol == 0 or op_type == "N/A":
                    pending_op = ""
                    continue
                transactions.append({
                    "position_type": _infer_pos_type(pos_type, asset),
                    "operation_type": op_type,
                    "asset":          asset,
                    "day":            int(day_str),
                    "quantity":       qty,
                    "unit_price":     _parse_brl_number(price_str),
                    "volume":         vol,
                })
            except ValueError:
                pass

            pending_op = ""

    return transactions


def parse_vlmo_pdf(pdf_bytes: bytes) -> list[dict]:
    """Parse all pages of a VLMO PDF; return list of transaction dicts."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            txns: list[dict] = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    txns.extend(_parse_vlmo_page(text))
            return txns
    except Exception as exc:
        logger.warning("VLMO PDF parsing failed: %s", exc)
        return []


# ── LLM + notification ────────────────────────────────────────────────────────

def call_llm(doc_type: str, company: str, date_str: str, text: str) -> str | None:
    try:
        system_prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
        client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
            timeout=60,
        )
        user_msg = (
            f"Tipo: {doc_type}\n"
            f"Empresa: {company}\n"
            f"Data: {date_str}\n\n"
            f"{text}"
        )
        completion = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            max_tokens=512,
        )
        return completion.choices[0].message.content
    except Exception as exc:
        logger.error("Deepseek API error: %s", exc)
        return None


def send_notification(row: pd.Series, summary: str | None) -> bool:
    """Send one document alert. Returns True if every recipient received it."""
    doc_type = row.get("CATEG_DOC", "Documento")
    company  = row.get("DENOM_CIA", "")
    dt       = row.get("DT_REF", "")
    link     = row.get("LINK_DOC", "")
    body     = summary if summary else "_Resumo indisponível_"
    msg = (
        f"*{doc_type} — {company}*\n"
        f"{dt}\n\n"
        f"{body}\n\n"
        f"[PDF]({link})"
    )
    return send_message(msg)


def send_vlmo_batch_notification(vlmo_by_company: dict) -> bool:
    """Send ONE consolidated VLMO Telegram message for all companies in this run.

    Returns True if every recipient received it (or there was nothing to send).
    """
    if not vlmo_by_company:
        return True

    ref_month = next(iter(vlmo_by_company.values())).get("ref_month", "")
    try:
        month_label = datetime.strptime(ref_month, "%Y-%m").strftime("%B/%Y")
    except (ValueError, TypeError):
        month_label = ref_month or "—"

    today_brt = datetime.now(ZoneInfo("America/Sao_Paulo"))
    month_abbrs = ["jan", "fev", "mar", "abr", "mai", "jun",
                   "jul", "ago", "set", "out", "nov", "dez"]
    today_str = f"{today_brt.day}-{month_abbrs[today_brt.month - 1]}"

    lines: list[str] = [f"*Atualização VLMO — {month_label} ({today_str})*", ""]

    for ccvm, data in vlmo_by_company.items():
        ticker = data["ticker"]
        denom = data["denom"]
        transactions = data.get("transactions", [])
        primary_link = data.get("primary_link", "")

        link_part = f", [doc]({primary_link})" if primary_link else ""
        company_header = f"• {ticker} ({denom}{link_part})"

        if not transactions:
            lines.append(company_header + ": sem transações")
            continue

        lines.append(company_header + ":")

        groups: dict[tuple, float] = {}
        group_counts: dict[tuple, int] = {}
        for txn in transactions:
            pos = txn.get("position_type", "Outros")
            op = txn.get("operation_type", "—").title()
            key = (pos, op)
            groups[key] = groups.get(key, 0.0) + txn.get("volume", 0.0)
            group_counts[key] = group_counts.get(key, 0) + 1

        for (pos, op), total_vol in groups.items():
            count = group_counts[(pos, op)]
            count_str = f" ({count}x)" if count > 1 else ""
            lines.append(f"  · {pos} · {op} · {_format_amount(total_vol)}{count_str}")

    return send_message("\n".join(lines))


# ── Git commit-back ───────────────────────────────────────────────────────────

def git_commit_back() -> None:
    """Commit ipe_skipped_log.csv back to the repo."""
    files = [str(SKIPPED_LOG_FILE), str(LAST_RUN_DATE_FILE)]
    existing = [f for f in files if Path(f).exists()]
    if not existing:
        return

    now_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(["git", "add"] + existing, check=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if result.returncode == 0:
            logger.info("No changes to commit.")
            return
        subprocess.run(["git", "commit", "-m", f"ipe update {now_utc}"], check=True)
        # The remote may have moved since checkout (manual pushes, other runs).
        subprocess.run(["git", "pull", "--rebase"], check=True)
        subprocess.run(["git", "push"], check=True)
        logger.info("Committed and pushed: %s", existing)
    except subprocess.CalledProcessError as exc:
        logger.error("git commit-back failed: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────

def _abort(title: str, exc: Exception, action: str) -> None:
    """Send a best-effort Telegram alert and exit non-zero."""
    msg = f"🚨 *CVM-Alerts parou*: {title}\nErro: `{exc}`\nAção: {action}"
    logger.critical("%s — aborting run: %s", title, exc)
    try:
        send_message(msg)
    except Exception:
        pass  # best-effort; don't mask the original error
    sys.exit(1)


def main() -> None:
    session      = create_session()
    company_map  = load_company_map()
    watched_ccvm = set(company_map.keys())
    processed      = load_processed_ids()
    today          = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    last_run_date  = load_last_run_date()
    # Re-query last_run_date itself: a run late in the day (or delayed by the
    # Actions scheduler past midnight) leaves part of it unseen. Hash dedup
    # makes the overlap free.
    start_date     = min(last_run_date, today)
    query_dates    = [start_date + timedelta(days=i)
                      for i in range((today - start_date).days + 1)]
    new_records    = []
    vlmo_by_company: dict[str, dict] = {}
    vlmo_hashes: list[str] = []
    failed_dates: list[date] = []
    undelivered = 0

    logger.info("Querying %d date(s): %s", len(query_dates),
                ", ".join(d.isoformat() for d in query_dates))

    frames = []
    for qd in query_dates:
        try:
            frames.append(download_ipe_b3(session, qd))
        except CredentialError as exc:
            # Permanent failure — no point retrying other dates.
            _abort("credencial B3 inválida", exc,
                   f"{PASSWORD_HELP}. Suporte: suporteexterno@cvm.gov.br")
        except B3RequestError as exc:
            _abort("parâmetro rejeitado pela B3", exc,
                   "corrigir a requisição em ipe\\_watcher.py")
        except Exception as exc:
            logger.error("Failed to query B3 API for %s: %s", qd, exc)
            failed_dates.append(qd)

    if not frames:
        _abort("B3 API indisponível", RuntimeError(
                   f"todas as {len(query_dates)} consulta(s) falharam"),
               "verificar logs do Actions; a próxima execução repete o período")

    df = pd.concat(frames, ignore_index=True)

    # Log categorias e empresas retornadas antes de filtrar (diagnóstico)
    logger.info("Categorias B3 (antes do filtro): %s",
                df["CATEG_DOC"].value_counts().to_dict())
    logger.info("CCVMs cobertura com registros: %s",
                sorted(set(df["ccvm"]) & watched_ccvm))

    # Filter: watched companies (by ccvm) + relevant categories.
    # No date filter: the API already returns only documents whose last action
    # falls in the queried window, and DT_REF is the document's *reference*
    # date, which can precede its delivery.
    df = df[df["ccvm"].isin(watched_ccvm)]
    df = df[df["CATEG_DOC"].isin(ALL_WATCHED_CATEGORIES)].copy()
    logger.info("%d rows after category+ccvm filter", len(df))

    # Enrich with CNPJ and company name from local map
    df["CNPJ_CIA"]     = df["ccvm"].map(lambda c: company_map[c]["cnpj"])
    df["DENOM_CIA"]    = df["ccvm"].map(lambda c: company_map[c]["denom"])
    df["_cnpj_digits"] = df["ccvm"].map(lambda c: company_map[c]["cnpj_digits"])

    for _, row in df.iterrows():
        categ = str(row.get("CATEG_DOC", ""))
        doc_hash = make_hash(row)
        if doc_hash in processed:
            continue

        if categ == VLMO_CATEGORY:
            link = str(row.get("LINK_DOC", ""))
            pdf_bytes = None
            if link:
                time.sleep(2)
                pdf_bytes = download_pdf(session, link)
            transactions = parse_vlmo_pdf(pdf_bytes) if pdf_bytes else []

            ccvm_key = str(row.get("ccvm", ""))
            dt_val = str(row.get("DT_REF", ""))
            if ccvm_key not in vlmo_by_company:
                vlmo_by_company[ccvm_key] = {
                    "ticker":       company_map[ccvm_key]["ticker"],
                    "denom":        str(row.get("DENOM_CIA", "")),
                    "ref_month":    dt_val[:7] if dt_val else "",
                    "transactions": [],
                    "primary_link": "",
                }
            vlmo_by_company[ccvm_key]["transactions"].extend(transactions)
            if link and (transactions or not vlmo_by_company[ccvm_key]["primary_link"]):
                vlmo_by_company[ccvm_key]["primary_link"] = link

            vlmo_hashes.append(doc_hash)
            new_records.append(row)
            continue

        should_process, skip_reason = pre_filter(row)
        if not should_process:
            _log_skipped(row, skip_reason)
            # Remember skipped docs too, so later runs don't log them again.
            processed.add(doc_hash)
            save_processed_ids(processed)
            continue

        pdf_bytes = None
        link = str(row.get("LINK_DOC", ""))
        if link:
            time.sleep(2)
            pdf_bytes = download_pdf(session, link)

        summary = None
        if pdf_bytes:
            text = extract_text(pdf_bytes)
            if text.strip():
                summary = call_llm(
                    doc_type=str(row.get("CATEG_DOC", "")),
                    company=str(row.get("DENOM_CIA", "")),
                    date_str=str(row.get("DT_REF", "")),
                    text=text,
                )

        if not send_notification(row, summary):
            # Leave unprocessed so the next run retries delivery.
            logger.error("Notification not delivered: %s", link)
            undelivered += 1
            continue
        processed.add(doc_hash)
        # Persist after each delivery: a crash later in the run must not
        # cause already-sent alerts to be sent again.
        save_processed_ids(processed)
        new_records.append(row)

    if vlmo_by_company:
        if send_vlmo_batch_notification(vlmo_by_company):
            processed.update(vlmo_hashes)
        else:
            logger.error("VLMO batch notification not delivered")
            undelivered += 1

    save_processed_ids(processed)

    # Advance only through the last date queried without error; a failed
    # date (and everything after it) is queried again next run.
    last_ok = None
    for qd in query_dates:
        if qd in failed_dates:
            break
        last_ok = qd
    if last_ok is not None:
        save_last_run_date(last_ok)

    if new_records:
        pd.DataFrame(new_records).to_csv(INDEX_LATEST_FILE, index=False)

    git_commit_back()
    logger.info("Done. Processed %d new documents.", len(new_records))

    if failed_dates or undelivered:
        logger.error("Run incomplete: %d failed date(s) %s, %d undelivered notification(s)",
                     len(failed_dates), [d.isoformat() for d in failed_dates], undelivered)
        sys.exit(1)


if __name__ == "__main__":
    main()
