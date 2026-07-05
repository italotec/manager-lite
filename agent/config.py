"""Config loader for the Card Client. Reads defaults from .env (if present) so
the GUI fields can be pre-filled — the user can still edit them before connecting."""
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    if getattr(sys, "frozen", False):
        _env_path = Path(sys._MEIPASS) / ".env"
    else:
        _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass

DEFAULT_SERVER_URL = os.getenv("SERVER_URL", "https://manager-lite.verifywaba.store")
DEFAULT_API_KEY = os.getenv("API_KEY", "")
