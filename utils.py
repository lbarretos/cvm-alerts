from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def ensure_datasets_dir():
    """Create and return the datasets directory path."""
    datasets_dir = Path('datasets')
    datasets_dir.mkdir(exist_ok=True)
    return datasets_dir


def create_session() -> requests.Session:
    """Return a Session with retry on both schemes. TLS certificates are verified."""
    retry = Retry(
        total=3, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=retry))
    session.mount('http://', HTTPAdapter(max_retries=retry))
    return session
