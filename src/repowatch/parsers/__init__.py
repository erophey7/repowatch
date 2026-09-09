from repowatch.parsers.base import IndexParser
from repowatch.parsers.apk import ApkParser
from repowatch.parsers.apt import AptParser
from repowatch.parsers.pacman import PacmanParser
from repowatch.parsers.dnf import DnfParser
from repowatch.parsers.apt_rpm import AptRpmParser

PARSERS: dict[str, type[IndexParser]] = {
    "apt": AptParser,
    "pacman": PacmanParser,
    "apk": ApkParser,
    "dnf": DnfParser,
    "apt-rpm": AptRpmParser,
}

__all__ = ["IndexParser", "AptParser", "PacmanParser", "ApkParser", "DnfParser", "AptRpmParser", "PARSERS"]
