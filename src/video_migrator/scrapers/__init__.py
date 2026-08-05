"""Website scrapers that discover the videos to migrate.

Modules here are named after the *site software* they parse, not the site: a
GnuBoard scraper serves every GnuBoard site. Which site is scraped comes from
the profile in :mod:`video_migrator.config`, so a second GnuBoard site needs a
new profile, not a new module — either registered here in :data:`SCRAPERS`, or,
for a profile that lives outside the package, by naming the software in its own
``site.scraper`` field.

Add a new kind of site by writing a module here that returns
:class:`~video_migrator.models.Video` objects and registering its scraper class
in :data:`SCRAPERS`, keyed by the profile name.
"""

from ..config import Profile
from .churchlove import ChurchLoveScraper
from .gnuboard import GnuBoardScraper, board_url

#: Profile name -> the scraper that can parse that site.
SCRAPERS = {
    "example": GnuBoardScraper,
    "churchlove": ChurchLoveScraper,
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


def scraper_for(profile: Profile) -> type:
    """
    Look up the scraper class a profile's site needs.

    A real profile lives under ``config/`` rather than in the package, so it
    cannot be registered in :data:`SCRAPERS`; it names the software its site
    runs in ``site.scraper`` instead, pointing at a profile that is.

    :param profile: The loaded site profile
    :return: The scraper class for that profile's site
    :raises KeyError: If neither the declared scraper nor the profile's own name
        is registered

    >>> from ..config import load_profile
    >>> scraper_for(load_profile("churchlove")).__name__
    'ChurchLoveScraper'
    """
    return get_scraper(profile.scraper or profile.name)


__all__ = [
    "SCRAPERS",
    "ChurchLoveScraper",
    "GnuBoardScraper",
    "board_url",
    "get_scraper",
    "scraper_for",
]
