"""站点信息仅从本机配置读取；公开默认值用于离线演示。"""

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SiteConfig:
    origin: str
    venue: str
    club_id: str


DEMO_SITE = SiteConfig("https://tickets.example.org", "示例体育馆", "26")
CONFIG_PATH = Path(__file__).resolve().parent / "site.local.json"


def load_site(path=CONFIG_PATH):
    """无配置时使用演示站点；配置无效时停止，避免请求错误主机。"""
    path = Path(path)
    if not path.exists():
        return DEMO_SITE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        origin = data["origin"].rstrip("/")
        url = urlsplit(origin)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment or url.port not in {None, 443}):
            raise ValueError()
        venue = data["venue"]
        club_id = data["club_id"]
        if (not isinstance(venue, str) or not venue.strip()
                or not isinstance(club_id, str) or not club_id.isdecimal()):
            raise ValueError()
        return SiteConfig(origin, venue, club_id)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("站点配置无效，请检查本机 site.local.json。") from None


SITE = load_site()
