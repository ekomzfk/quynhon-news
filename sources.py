"""Источники новостей. Добавляйте и удаляйте блоки как хотите.

type:         "rss"  — RSS-лента; "html" — главная страница, бот ищет ссылки на статьи по link_pattern
scope:        "local" (Куинён и провинция) или "national" (Вьетнам) — подсказка для ИИ
publish_all:  True — публиковать всё с этого сайта (без отбора по интересности, только без дублей)
"""

SOURCES = [
    # ---------- Куинён / провинция Зялай ----------
    {
        "name": "Gia Lai News",
        "type": "html",
        "url": "https://en.baogialai.com.vn/",
        "link_pattern": r"-post\d+\.html$",
        "scope": "local",
        "publish_all": True,
    },
    {
        "name": "Báo Gia Lai",
        "type": "html",
        "url": "https://baogialai.com.vn/",
        "link_pattern": r"-post\d+\.html$",
        "scope": "local",
    },
    {
        # всё, что вьетнамская пресса пишет про Куинён
        "name": "Google News",
        "type": "rss",
        "url": "https://news.google.com/rss/search?q=%22Quy+Nh%C6%A1n%22+when%3A2d&hl=vi&gl=VN&ceid=VN:vi",
        "scope": "local",
    },

    # ---------- Вьетнам ----------
    {
        "name": "VnExpress International",
        "type": "rss",
        "url": "https://e.vnexpress.net/rss/news.rss",
        "scope": "national",
    },
    {
        "name": "VnExpress International",
        "type": "rss",
        "url": "https://e.vnexpress.net/rss/travel.rss",
        "scope": "national",
    },
    {
        "name": "SGGP News",
        "type": "html",
        "url": "https://en.sggp.org.vn/",
        "link_pattern": r"-post\d+\.html$",
        "scope": "national",
    },
    {
        # законы, налоги, визы — по-английски
        "name": "LuatVietnam",
        "type": "html",
        "url": "https://english.luatvietnam.vn/",
        "link_pattern": r"/(legal-updates|legal-news)/.+-article\.html$",
        "scope": "national",
    },
    {
        "name": "Лаодонг",
        "type": "html",
        "url": "https://ru.laodong.vn/",
        "link_pattern": r"\d{5,}\.ldo$",
        "scope": "national",
    },
    {
        # визы и правила для иностранцев из любых изданий
        "name": "Google News",
        "type": "rss",
        "url": "https://news.google.com/rss/search?q=Vietnam+%28foreigners+OR+expats+OR+visa%29+when%3A2d&hl=en-US&gl=US&ceid=US:en",
        "scope": "national",
    },
]
