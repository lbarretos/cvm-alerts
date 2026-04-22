import warnings
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

warnings.filterwarnings('ignore', message='Unverified HTTPS request')
requests.packages.urllib3.disable_warnings()


def ensure_datasets_dir():
    """Create and return the datasets directory path."""
    datasets_dir = Path('datasets')
    datasets_dir.mkdir(exist_ok=True)
    return datasets_dir


class TLSAdapter(HTTPAdapter):
    """HTTPAdapter that disables SSL verification (required for dados.cvm.gov.br)."""

    def __init__(self, *args, **kwargs):
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        kwargs.setdefault('max_retries', retry)
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        ctx = requests.packages.urllib3.util.ssl_.create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = 0
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


def create_session() -> requests.Session:
    """Return a Session with TLS bypass + retry on both schemes."""
    session = requests.Session()
    session.mount('https://', TLSAdapter())
    session.mount('http://', HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )))
    return session 