# Libraries
import pandas as pd
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from io import BytesIO
import logging
import tempfile
import concurrent.futures
from pathlib import Path
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import zipfile
from reports.report_generator import ReportGenerator
from colorama import init, Fore, Style
import json
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

SUMMARY_STATE_FILE = Path('datasets/vlmo_summary_state.json')

def create_session():
    """Create a session with retry logic and connection pooling."""
    session = requests.Session()
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

def download_and_extract_zip(url, temp_dir):
    """Download a zip file and extract its CSVs into temp_dir.

    The nine yearly zips total a few MB and download in ~3 s, so there is no
    local cache (an ETag cache used to live here, but .cache/ was never
    persisted between Actions runs, so it never hit).
    """
    try:
        response = create_session().get(url, timeout=60)
        response.raise_for_status()
        with zipfile.ZipFile(BytesIO(response.content)) as zip_ref:
            zip_ref.extractall(temp_dir)
            return [temp_dir / f for f in zip_ref.namelist() if f.endswith('.csv')]
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
            
            # Each new version of a monthly form replaces the whole form: keep
            # only the rows of the latest version per company and month.
            # (A groupby().first() here used to merge distinct trades sharing
            # date and movement type, mixing columns across rows, and kept
            # rows of superseded versions.)
            latest = (combined_consolidated
                .groupby(['Company_CNPJ', 'Reference_Date'])['Version']
                .transform('max'))
            combined_consolidated = (combined_consolidated
                [(combined_consolidated['Version'] == latest) | latest.isna()]
                .sort_values(['Reference_Date', 'Company_CNPJ'], kind='mergesort')
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
        response = session.get(base_url)
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
    return datasets_dir


def send_weekly_summary(consolidated_data: pd.DataFrame) -> None:
    """Send the P1/P2 insider highlights of the coverage list, once per
    company and reference month (again only if the figures change)."""
    coverage = alerts.load_coverage_map()
    state = alerts.load_state(SUMMARY_STATE_FILE)
    header, blocks, new_state = alerts.weekly_summary(
        consolidated_data, coverage, datetime.now().date(), state)
    logger.info(f"🔔 Resumo semanal: {len(blocks)} empresa(s) nova(s) ou atualizada(s)")
    if blocks and not telegram_notifier.send_blocks(header, blocks):
        # Keep the old state so the next run retries delivery.
        raise RuntimeError("Resumo semanal não entregue no Telegram")
    alerts.save_state(SUMMARY_STATE_FILE, new_state)


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

        all_csv_files = []
        # Use ThreadPoolExecutor for parallel downloads
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_zip = {
                executor.submit(download_and_extract_zip, base_url + zip_file, temp_dir): zip_file
                for zip_file in zip_files
            }
            for future in concurrent.futures.as_completed(future_to_zip):
                zip_file = future_to_zip[future]
                try:
                    all_csv_files.extend(future.result())
                    logger.info(f"📥 Downloaded {zip_file}")
                except Exception as e:
                    logger.error(f"❌ Error processing {zip_file}: {str(e)}")

        download_time = time.time() - download_start
        logger.info(f"⏱️ Download time: {download_time:.2f} seconds")

        if not all_csv_files:
            raise Exception("❌ No CSV files were successfully downloaded and extracted")
        
        # Process data
        process_start = time.time()
        logger.info("⚙️ Processing trading data...")
        consolidated_data = process_trading_data(all_csv_files)
        process_time = time.time() - process_start
        logger.info(f"⏱️ Processing time: {process_time:.2f} seconds")
        logger.info("✅ Data processing completed")
        
        datasets_dir = ensure_datasets_dir()

        # Save consolidated data
        if not consolidated_data.empty:
            output_path = datasets_dir / 'Brazil_Stock_Trading_Consolidated.csv'
            consolidated_data.to_csv(output_path, index=False, encoding='utf-8-sig')
            logger.info(f"💾 Saved Consolidated data to: {output_path}")
            logger.info(f"📊 Consolidated data shape: {consolidated_data.shape}")
            send_weekly_summary(consolidated_data)

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