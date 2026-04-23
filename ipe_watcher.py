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
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import pdfplumber
import yaml
from openai import OpenAI

from utils import create_session
from telegram_notifier import send_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
PROCESSED_IDS_FILE = Path("datasets/ipe_processed_ids.json")
SKIPPED_LOG_FILE   = Path("datasets/ipe_skipped_log.csv")
INDEX_LATEST_FILE  = Path("datasets/ipe_index_latest.csv")
CNPJ_MAP_FILE      = Path("config/cnpj_map.yaml")
SYSTEM_PROMPT_FILE = Path("config/system_prompt_ipe.txt")

B3_API_URL = "https://seguro.bmfbovespa.com.br/rad/download/SolicitaDownload.asp"
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


def download_ipe_b3(session, query_date: date) -> pd.DataFrame:
    """Query B3 Multiple Download API for IPE filings on query_date.

    Returns DataFrame with same schema as the old download_ipe_index():
    CNPJ_CIA, DENOM_CIA, DT_ENTREGA, CATEG_DOC, TIPO_DOC, LINK_DOC, ASSUNTO, _cnpj_digits
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
    resp = session.post(B3_API_URL, data=payload, timeout=60)
    resp.raise_for_status()

    # Response is ISO-8859-1 encoded XML
    root = ET.fromstring(resp.content.decode("iso-8859-1"))

    if root.tag == "ERROS":
        code = root.findtext("NUMERO_DO_ERRO", "")
        desc = root.findtext("DESCRICAO_DO_ERRO", "")
        raise RuntimeError(f"B3 API error {code}: {desc}")

    rows = []
    for link in root.findall("Link"):
        rows.append({
            "ccvm":     link.get("ccvm", ""),
            "DT_ENTREGA": _parse_dataref(link.get("DataRef", "")),
            "CATEG_DOC":  link.get("Categoria", ""),
            "TIPO_DOC":   link.get("Tipo", ""),
            "LINK_DOC":   link.get("url", ""),
            "ASSUNTO":    link.get("Especie", ""),
            "Situacao":   link.get("Situacao", ""),
        })

    logger.info("B3 API returned %d IPE records for %s", len(rows), date_str)

    if not rows:
        return pd.DataFrame(columns=["CNPJ_CIA","DENOM_CIA","DT_ENTREGA","CATEG_DOC","TIPO_DOC","LINK_DOC","ASSUNTO","_cnpj_digits"])

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
            row.get("DT_ENTREGA", ""),
            row.get("DENOM_CIA", ""),
            row.get("CATEG_DOC", ""),
            reason,
        ])


# ── Dedup ─────────────────────────────────────────────────────────────────────

def make_hash(row: pd.Series) -> str:
    raw = "|".join([
        str(row.get("CNPJ_CIA", "")),
        str(row.get("DT_ENTREGA", "")),
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


# ── LLM + notification ────────────────────────────────────────────────────────

def call_llm(doc_type: str, company: str, date_str: str, text: str) -> str | None:
    try:
        system_prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
        client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
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


def send_notification(row: pd.Series, summary: str | None) -> None:
    doc_type = row.get("CATEG_DOC", "Documento")
    company  = row.get("DENOM_CIA", "")
    dt       = row.get("DT_ENTREGA", "")
    link     = row.get("LINK_DOC", "")
    body     = summary if summary else "_Resumo indisponível_"
    msg = (
        f"*{doc_type} — {company}*\n"
        f"{dt}\n\n"
        f"{body}\n\n"
        f"[PDF]({link})"
    )
    send_message(msg)


# ── Git commit-back ───────────────────────────────────────────────────────────

def git_commit_back() -> None:
    """Commit ipe_skipped_log.csv back to the repo."""
    files = [str(SKIPPED_LOG_FILE)]
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
        subprocess.run(["git", "push"], check=True)
        logger.info("Committed and pushed: %s", existing)
    except subprocess.CalledProcessError as exc:
        logger.error("git commit-back failed: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    session      = create_session()
    company_map  = load_company_map()
    watched_ccvm = set(company_map.keys())
    processed    = load_processed_ids()
    today        = date.today()
    today_str    = today.isoformat()
    new_records  = []

    try:
        df = download_ipe_b3(session, today)
    except Exception as exc:
        logger.error("Failed to query B3 API: %s", exc)
        return

    # Filter: watched companies (by ccvm) + relevant categories
    df = df[df["ccvm"].isin(watched_ccvm)]
    df = df[df["CATEG_DOC"].isin(CATEGORIES)]
    logger.info("%d rows after category+ccvm filter", len(df))

    # Enrich with CNPJ and company name from local map
    df["CNPJ_CIA"]    = df["ccvm"].map(lambda c: company_map[c]["cnpj"])
    df["DENOM_CIA"]   = df["ccvm"].map(lambda c: company_map[c]["denom"])
    df["_cnpj_digits"] = df["ccvm"].map(lambda c: company_map[c]["cnpj_digits"])

    # B3 API already filters by date, but guard against edge cases
    df = df[df["DT_ENTREGA"].str.startswith(today_str, na=False)]
    logger.info("%d rows for today (%s)", len(df), today_str)

    for _, row in df.iterrows():
        should_process, skip_reason = pre_filter(row)
        if not should_process:
            _log_skipped(row, skip_reason)
            continue

        doc_hash = make_hash(row)
        if doc_hash in processed:
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
                    date_str=str(row.get("DT_ENTREGA", "")),
                    text=text,
                )

        send_notification(row, summary)
        processed.add(doc_hash)
        new_records.append(row)

    save_processed_ids(processed)

    if new_records:
        pd.DataFrame(new_records).to_csv(INDEX_LATEST_FILE, index=False)

    git_commit_back()
    logger.info("Done. Processed %d new documents.", len(new_records))


if __name__ == "__main__":
    main()
