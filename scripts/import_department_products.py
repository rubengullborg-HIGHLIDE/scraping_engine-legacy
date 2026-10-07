"""Bounded department-store imports; dry runs never connect to Supabase."""
from __future__ import annotations
import logging
from scrapers.full_import.department_stores import make_scraper
from scripts.import_axel_quint_products import import_products, parse_args


def main(store):
    args = parse_args(store)
    logging.basicConfig(level=args.log_level, format='%(asctime)s %(levelname)s %(message)s')
    return import_products(store, args, scraper_factory=make_scraper)
