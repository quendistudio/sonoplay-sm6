import os

from logging_config import configure_logging
from settings import settings
from version import print_banner

configure_logging(os.environ.get("LOG_LEVEL", settings.log_level))
print_banner()

from plex.plexserver import start_plex_server

if __name__ == "__main__":
    start_plex_server()
