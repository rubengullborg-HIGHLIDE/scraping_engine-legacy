"""qUINT catalogue and exact shop inventory."""
from scrapers.full_import.axel_quint import AxelQuintScraper


class QuintScraper(AxelQuintScraper):
    def __init__(self, **kwargs):
        super().__init__('quint', **kwargs)
