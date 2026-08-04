"""Website scrapers that discover the videos to migrate.

Modules here are named after the *site software* they parse, not the site: a
GnuBoard scraper serves every GnuBoard site. Which site is scraped comes from
the profile in :mod:`video_migrator.config`, so a second GnuBoard site needs a
new profile, not a new module.

Add a new kind of site by writing a module here that returns
:class:`~video_migrator.models.Video` objects and registering its scraper class
in :data:`SCRAPERS`, keyed by the profile name.
"""

from .gnuboard import GnuBoardScraper, board_url

#: Profile name -> the scraper that can parse that site.
SCRAPERS = {
    "example": GnuBoardScraper,
}


def get_scraper(site: str) -> type:
    """
    Look up a scraper class by site name.

    :param site: Site key registered in :data:`SCRAPERS` (e.g. 'example')
    :return: The scraper class for that site
    :raises KeyError: If no scraper is registered for the site
    """
    if site not in SCRAPERS:
        raise KeyError(f"No scraper registered for site '{site}'. Known sites: {', '.join(sorted(SCRAPERS))}")
    return SCRAPERS[site]


__all__ = [
    "SCRAPERS",
    "GnuBoardScraper",
    "board_url",
    "get_scraper",
]
