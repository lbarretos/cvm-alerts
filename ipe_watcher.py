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

CVM_BASE   = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/"
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


def load_cnpj_map() -> set[str]:
    """Return set of normalised (digits-only) CNPJs from config/cnpj_map.yaml."""
    with open(CNPJ_MAP_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {_digits(v) for v in data.get("tickers", {}).values() if v}


# ── Index download ─────────────────────────────────────────────────────────────

def download_ipe_index(session, year: int) -> pd.DataFrame:
    """Download the annual IPE index ZIP and return its CSV as a DataFrame."""
    url = f"{CVM_BASE}ipe_cia_aberta_{year}.zip"
    logger.info("Downloading IPE index: %s", url)
    resp = session.get(url, timeout=60)
    resp.raise_for_status()

    _RENAMES = {
        "CNPJ_Companhia": "CNPJ_CIA",
        "Nome_Companhia":  "DENOM_CIA",
        "Data_Entrega":    "DT_ENTREGA",
        "Categoria":       "CATEG_DOC",
        "Tipo":            "TIPO_DOC",
        "Link_Download":   "LINK_DOC",
    }

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_name = f"ipe_cia_aberta_{year}.csv"
        with zf.open(csv_name) as f:
            df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)

    df.rename(columns=_RENAMES, inplace=True)

    required = {"CNPJ_CIA", "DENOM_CIA", "DT_ENTREGA", "CATEG_DOC", "LINK_DOC"}
    missing_cols = required - set(df.columns)
    assert not missing_cols, (
        f"IPE CSV missing expected columns after rename: {missing_cols}. "
        f"Actual columns: {list(df.columns)}"
    )
    df["_cnpj_digits"] = df["CNPJ_CIA"].apply(_digits)
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
    """Commit ipe_processed_ids.json and ipe_skipped_log.csv back to the repo."""
    files = [str(PROCESSED_IDS_FILE), str(SKIPPED_LOG_FILE)]
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
    session     = create_session()
    cnpjs       = load_cnpj_map()
    processed   = load_processed_ids()
    year        = date.today().year
    today_str   = date.today().isoformat()
    new_records = []

    try:
        df = download_ipe_index(session, year)
    except Exception as exc:
        logger.error("Failed to download IPE index: %s", exc)
        return

    # Filter: category + CNPJ
    df = df[df["CATEG_DOC"].isin(CATEGORIES)]
    df = df[df["_cnpj_digits"].isin(cnpjs)]
    logger.info("%d rows after category+CNPJ filter", len(df))

    # Filter: today's filings only
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
