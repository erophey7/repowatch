from repowatch.parsers.gentoo import GentooParser
from repowatch.parsers.slackware import SlackwareParser
from repowatch.parsers.base import IndexParser
from repowatch.parsers.apk import ApkParser
from repowatch.parsers.apt import AptParser
from repowatch.parsers.pacman import PacmanParser
from repowatch.parsers.dnf import DnfParser
from repowatch.parsers.apt_rpm import AptRpmParser
from repowatch.parsers.xbps import XbpsParser
from repowatch.parsers.nix import NixParser

PARSERS: dict[str, type[IndexParser]] = {
    "apt": AptParser,
    "pacman": PacmanParser,
    "apk": ApkParser,
    "dnf": DnfParser,
    "apt-rpm": AptRpmParser,
    "xbps": XbpsParser,
    "gentoo": GentooParser,
    "slackware": SlackwareParser,
    "nix": NixParser,
}

__all__ = ["IndexParser", "AptParser", "PacmanParser", "ApkParser", "DnfParser", "AptRpmParser", "XbpsParser", "NixParser", "GentooParser", "SlackwareParser", "PARSERS"]
