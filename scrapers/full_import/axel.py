"""AXEL catalogue and exact shop inventory."""
from scrapers.full_import.axel_quint import AxelQuintScraper


class AxelScraper(AxelQuintScraper):
    def __init__(self, **kwargs):
        super().__init__('axel', **kwargs)
