# Libraries
import pandas as pd
import requests
import numpy as np
from datetime import datetime
import ssl
import warnings
from bs4 import BeautifulSoup
from io import BytesIO
import logging
from urllib3.util.ssl_ import create_urllib3_context
import re
import os
import tempfile
import utils
import concurrent.futures
from functools import lru_cache
import hashlib
from pathlib import Path
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import zipfile
from reports.report_generator import ReportGenerator
from colorama import init, Fore, Style
import shutil
import json
import threading
import delta
import alerts
import telegram_notifier

# Initialize colorama
init()

# Configure logging with colors
class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors"""
    def format(self, record):
        if record.levelno >= logging.ERROR:
            color = Fore.RED
        elif record.levelno >= logging.WARNING:
            color = Fore.YELLOW
        else:
            color = Fore.GREEN
        record.msg = f"{color}{record.msg}{Style.RESET_ALL}"
        return super().format(record)

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter('%(message)s'))
logger.addHandler(handler)

# Suppress only the specific warning about unverified HTTPS requests
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# Suppress HTTPS verification warnings
requests.packages.urllib3.disable_warnings()

CACHE_DIR  = Path('.cache')
ZIPS_DIR   = CACHE_DIR / 'zips'
ETAGS_FILE = CACHE_DIR / 'etags.json'


def load_etags() -> dict:
    if ETAGS_FILE.exists():
        with open(ETAGS_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_etags(etags: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    with open(ETAGS_FILE, 'w') as f:
        json.dump(etags, f, indent=2)


class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = requests.packages.urllib3.util.ssl_.create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = 0
        kwargs['ssl_context'] = ctx
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

def create_session():
    """Create a session with retry logic and connection pooling."""
    session = requests.Session()
    session.mount('https://', TLSAdapter())
    
    # Configure retry strategy
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    
    # Mount the adapter with retry strategy
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    return session

def download_and_extract_zip(url, temp_dir, etags: dict, etags_lock: threading.Lock):
    """Download and extract a zip file, reusing local cache when Last-Modified is unchanged.

    Returns (csv_files, was_downloaded) where was_downloaded=False means cache hit.
    Falls back to full download if HEAD request fails.
    """
    try:
        session = create_session()
        filename = url.split('/')[-1]
        cached_zip = ZIPS_DIR / filename
        last_modified = None

        # HEAD request — cheap check before any GET
        try:
            head = session.head(url, verify=False, timeout=10)
            last_modified = head.headers.get('Last-Modified')
        except Exception:
            pass  # network issue → proceed with unconditional GET

        # Cache hit: same Last-Modified and zip already stored locally
        if (last_modified
                and url in etags
                and etags[url] == last_modified
                and cached_zip.exists()):
            with zipfile.ZipFile(cached_zip) as zip_ref:
                zip_ref.extractall(temp_dir)
                csv_files = [temp_dir / f for f in zip_ref.namelist() if f.endswith('.csv')]
            return csv_files, False

        # Cache miss: full download
        response = session.get(url, verify=False)
        response.raise_for_status()

        zip_bytes = response.content
        ZIPS_DIR.mkdir(parents=True, exist_ok=True)
        cached_zip.write_bytes(zip_bytes)

        with zipfile.ZipFile(BytesIO(zip_bytes)) as zip_ref:
            zip_ref.extractall(temp_dir)
            csv_files = [temp_dir / f for f in zip_ref.namelist() if f.endswith('.csv')]

        if last_modified:
            with etags_lock:
                etags[url] = last_modified

        return csv_files, True

    except Exception as e:
        logger.error(f"Error downloading or extracting zip file: {str(e)}")
        raise

def _load_vlmo_schema() -> set:
    schema_path = Path('schemas/vlmo_consolidado.json')
    with open(schema_path) as f:
        return set(json.load(f)['required_columns'])


def _validate_schema(df, required_cols: set, filename: str) -> None:
    """Warn on missing/unexpected columns; raise if >30% of required cols are absent."""
    actual_cols = set(df.columns) - {'File_Type'}
    missing = required_cols - actual_cols
    unexpected = actual_cols - required_cols
    if missing:
        missing_pct = len(missing) / len(required_cols)
        logger.warning(
            f"Schema mismatch em {filename}: "
            f"{len(missing)} coluna(s) ausente(s): {sorted(missing)}; "
            f"{len(unexpected)} inesperada(s): {sorted(unexpected)}"
        )
        if missing_pct > 0.30:
            raise ValueError(
                f"Mais de 30% das colunas obrigatórias ausentes em {filename} "
                f"({missing_pct:.0%}): {sorted(missing)}"
            )


def process_trading_data(csv_files):
    """Process the trading data from CSV files."""
    try:
        required_cols = _load_vlmo_schema()
        consolidated_data = []

        for csv_path in csv_files:
            try:
                # Read CSV file with proper encoding for Brazilian Portuguese
                df = pd.read_csv(csv_path, encoding='latin1', sep=';', decimal=',')
                logger.info(f"Processing file: {csv_path.name}")

                # Only process consolidated files
                if '_con_' in str(csv_path).lower():
                    df['File_Type'] = 'Consolidated'
                    _validate_schema(df, required_cols, csv_path.name)
                    # Mapping for consolidated files
                    columns_mapping = {
                        'CNPJ_Companhia': 'Company_CNPJ',
                        'Nome_Companhia': 'Company_Name',
                        'Data_Referencia': 'Reference_Date',
                        'Versao': 'Version',
                        'Tipo_Empresa': 'Company_Type',
                        'Empresa': 'Company',
                        'Tipo_Cargo': 'Position_Type',
                        'Tipo_Movimentacao': 'Movement_Type',
                        'Descricao_Movimentacao': 'Movement_Description',
                        'Tipo_Operacao': 'Operation_Type',
                        'Tipo_Ativo': 'Asset_Type',
                        'Caracteristica_Valor_Mobiliario': 'Security_Characteristic',
                        'Intermediario': 'Intermediary',
                        'Data_Movimentacao': 'Movement_Date',
                        'Quantidade': 'Quantity',
                        'Preco_Unitario': 'Unit_Price',
                        'Volume': 'Volume',
                        'File_Type': 'File_Type'
                    }
                    # Rename columns that exist in the data
                    df = df.rename(columns={k: v for k, v in columns_mapping.items() if k in df.columns})
                    consolidated_data.append(df)
                    
            except Exception as e:
                logger.error(f"Error processing file {csv_path}: {str(e)}")
                continue
        
        # Process consolidated data
        if consolidated_data:
            combined_consolidated = pd.concat(consolidated_data, ignore_index=True)
            # Convert date columns
            combined_consolidated['Reference_Date'] = pd.to_datetime(combined_consolidated['Reference_Date'])
            combined_consolidated['Movement_Date'] = pd.to_datetime(combined_consolidated['Movement_Date'])
            
            # Convert Version to numeric, replacing non-numeric versions with NaN
            combined_consolidated['Version'] = pd.to_numeric(combined_consolidated['Version'], errors='coerce')
            
            # Group by key fields and get the latest version
            key_fields = ['Reference_Date', 'Company_CNPJ', 'Company_Name', 'Movement_Date', 'Movement_Type']
            combined_consolidated = (combined_consolidated
                .sort_values(['Reference_Date', 'Version'], ascending=[True, False], kind='mergesort')
                .groupby(key_fields, as_index=False)
                .first()
                .sort_values('Reference_Date', kind='mergesort')
                .reset_index(drop=True))
            
            # Log version information
            logger.info(f"Consolidated data version range: {combined_consolidated['Version'].min()} to {combined_consolidated['Version'].max()}")
            logger.info(f"Number of unique dates in consolidated data: {combined_consolidated['Reference_Date'].nunique()}")
            
        else:
            combined_consolidated = pd.DataFrame()
            
        return combined_consolidated
    except Exception as e:
        logger.error(f"Error processing trading data: {str(e)}")
        raise

def get_available_files(base_url):
    """Get list of available zip files from the CVM website."""
    try:
        session = create_session()
        response = session.get(base_url, verify=False)
        response.raise_for_status()
        
        # Parse HTML content
        soup = BeautifulSoup(response.content, 'html.parser')
        zip_files = [link.get('href') for link in soup.find_all('a') if link.get('href', '').endswith('.zip')]
        
        return zip_files
    except Exception as e:
        logger.error(f"Error getting available files: {str(e)}")
        raise

def ensure_datasets_dir():
    """Ensure the datasets directory exists and return its path."""
    datasets_dir = Path('datasets')
    datasets_dir.mkdir(exist_ok=True)
    
    # Create backup directory
    backup_dir = datasets_dir / 'history'
    backup_dir.mkdir(exist_ok=True)
    
    return datasets_dir

def backup_dataset(file_path, backup_dir):
    """Create a backup of a dataset with timestamp."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    file_name = file_path.name
    base_name = file_name.rsplit('.', 1)[0]
    extension = file_name.rsplit('.', 1)[1]
    backup_path = backup_dir / f"{base_name}_{timestamp}.{extension}"
    
    # Copy the file to backup
    shutil.copy2(file_path, backup_path)
    
    # Keep only the last 5 backups for each dataset
    pattern = f"{base_name}_*.{extension}"
    backup_files = sorted(backup_dir.glob(pattern))
    if len(backup_files) > 5:
        for old_file in backup_files[:-5]:
            old_file.unlink()

def main():
    """Main function to extract and process Brazilian stock trading data."""
    try:
        start_time = time.time()
        print(f"\n{Fore.CYAN}🚀 Starting CVM358 Data Extraction{Style.RESET_ALL}\n")
        
        # Create temporary directory
        temp_dir = Path(tempfile.mkdtemp())
        logger.info(f"📁 Created temporary directory: {temp_dir}")
        
        # Base URL for the data
        base_url = 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/VLMO/DADOS/'
        
        # Get available files
        download_start = time.time()
        zip_files = get_available_files(base_url)
        logger.info(f"📦 Found {len(zip_files)} zip files")

        etags = load_etags()
        etags_lock = threading.Lock()
        ZIPS_DIR.mkdir(parents=True, exist_ok=True)
        downloaded_count = 0
        skipped_count = 0

        all_csv_files = []
        # Use ThreadPoolExecutor for parallel downloads
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            # Create future tasks for each zip file
            future_to_zip = {
                executor.submit(download_and_extract_zip, base_url + zip_file, temp_dir, etags, etags_lock): zip_file
                for zip_file in zip_files
            }

            # Process completed downloads
            for future in concurrent.futures.as_completed(future_to_zip):
                zip_file = future_to_zip[future]
                try:
                    csv_files, was_downloaded = future.result()
                    all_csv_files.extend(csv_files)
                    if was_downloaded:
                        downloaded_count += 1
                        logger.info(f"📥 Downloaded {zip_file}")
                    else:
                        skipped_count += 1
                        logger.info(f"⏭️  Skipped {zip_file} (sem mudança)")
                except Exception as e:
                    logger.error(f"❌ Error processing {zip_file}: {str(e)}")
                    continue

        save_etags(etags)
        download_time = time.time() - download_start
        logger.info(f"⏱️ Download time: {download_time:.2f} seconds")
        logger.info(f"📊 Cache: {downloaded_count} baixado(s), {skipped_count} ignorado(s) (sem mudança)")
        
        if not all_csv_files:
            raise Exception("❌ No CSV files were successfully downloaded and extracted")
        
        # Process data
        process_start = time.time()
        logger.info("⚙️ Processing trading data...")
        consolidated_data = process_trading_data(all_csv_files)
        process_time = time.time() - process_start
        logger.info(f"⏱️ Processing time: {process_time:.2f} seconds")
        logger.info("✅ Data processing completed")
        
        # Create datasets directory if it doesn't exist
        datasets_dir = ensure_datasets_dir()
        backup_dir = datasets_dir / 'history'
        
        # Save consolidated data
        if not consolidated_data.empty:
            previous_backup = delta.latest_backup(backup_dir)

            output_path = datasets_dir / 'Brazil_Stock_Trading_Consolidated.csv'
            consolidated_data.to_csv(output_path, index=False, encoding='utf-8-sig')
            logger.info(f"💾 Saved Consolidated data to: {output_path}")
            logger.info(f"📊 Consolidated data shape: {consolidated_data.shape}")
            backup_dataset(output_path, backup_dir)
            logger.info(f"📚 Created backup of Consolidated data in: {backup_dir}")

            # Compare the saved CSV (not in-memory) vs previous backup so that
            # floating-point precision is consistent across both sides of the diff.
            delta_df = delta.detect_new_records(
                pd.read_csv(output_path, encoding='utf-8-sig'), previous_backup
            )
            delta_path = datasets_dir / 'delta_latest.csv'
            delta_df.to_csv(delta_path, index=False, encoding='utf-8-sig')
            logger.info(f"🆕 Delta: {len(delta_df)} novo(s) registro(s) → {delta_path}")

            if not delta_df.empty:
                coverage = alerts.load_coverage('config/coverage.yaml')
                alerts_df = alerts.classify_alerts(delta_df, coverage)
                prio_counts = alerts_df['priority'].value_counts().to_dict() if not alerts_df.empty else {}
                logger.info(f"🔔 Alertas: {len(alerts_df)} {prio_counts}")
                telegram_notifier.send_alerts(alerts_df)
            else:
                alerts_df = pd.DataFrame()

        # Generate and print report
        logger.info("📈 Generating reports...")
        report_generator = ReportGenerator()
        report = report_generator.generate_report(consolidated_data, pd.DataFrame())
        report_generator.print_report(report)
        logger.info(f"📄 Latest report generated: reports/latest_report.html")
        logger.info(f"📚 Historical reports stored in: reports/history/")
        
        # Clean up
        for file in temp_dir.glob('*'):
            file.unlink()
        temp_dir.rmdir()
        logger.info("🧹 Cleaned up temporary files")
        
        total_time = time.time() - start_time
        print(f"\n{Fore.GREEN}✨ Data extraction completed successfully!{Style.RESET_ALL}")
        print(f"{Fore.CYAN}📄 A detailed HTML report has been generated in the reports directory.{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}⏱️ Total execution time: {total_time:.2f} seconds{Style.RESET_ALL}\n")
        
    except Exception as e:
        logger.error(f"❌ Error in main function: {str(e)}")
        raise

if __name__ == "__main__":
    main() 