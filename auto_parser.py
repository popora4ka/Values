"""MM2 Supreme Values parser.

The parser keeps normal items and Chroma items as separate value fields.
It also handles Unique items such as Corrupt and can use items.txt to
detect whether an item is a Knife or Gun when the website name does not
contain that information.
"""

import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cf_requests
    HAS_CURL = True
except ImportError:
    HAS_CURL = False


HERE = Path(__file__).resolve().parent
PRICES_PATH = HERE / "prices.json"
META_PATH = HERE / "meta.json"
ALIASES_PATH = HERE / "aliases.txt"
ITEMS_PATH = HERE / "items.txt"

BASE_PREFIX = "https://supremevalues.com/mm2/"

CATEGORIES = [
    "godlies",
    "ancients",
    "chromas",
    "vintages",
    "collectibles",
    "pets",
    "legendaries",
    "rares",
    "uncommons",
    "commons",
    "sets",
    "uniques",
    "evos",
    "misc",
    "untradables",
]

UNTRADABLE_CATEGORIES = {"untradables"}

RARITY_MAP = {
    "commons": "common",
    "uncommons": "uncommon",
    "rares": "rare",
    "legendaries": "legendary",
    "godlies": "godly",
    "chromas": "chroma",
    "vintages": "vintage",
    "ancients": "ancient",
    "evos": "evo",
    "collectibles": "collectible",
    "pets": "pet",
    "sets": "set",
    "uniques": "unique",
    "misc": "misc",
    "untradables": "untradable",
}

MIN_ITEMS_SANITY = 100
MIN_CATEGORIES_SANITY = 5
DELAY_MIN_SECONDS = 3
DELAY_MAX_SECONDS = 6

DEFAULT_ALIASES = {
    "bioblade": "Bio Blade",
}

DEFAULT_ALIASES_FILE = """# Website name = game name
Bioblade = Bio Blade
"""

PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
TRAILING_WEAPON_RE = re.compile(r"[\s\-–—]*(gun|knife)\s*$", re.IGNORECASE)
PAREN_KNIFE_RE = re.compile(r"\(\s*knife\s*\)", re.IGNORECASE)
PAREN_GUN_RE = re.compile(r"\(\s*gun\s*\)", re.IGNORECASE)
END_KNIFE_RE = re.compile(r"\bknife\b\s*$", re.IGNORECASE)
END_GUN_RE = re.compile(r"\bgun\b\s*$", re.IGNORECASE)
KNIFE_ANY_RE = re.compile(r"\bknife\b", re.IGNORECASE)
GUN_ANY_RE = re.compile(r"\bgun\b", re.IGNORECASE)
UNTRADABLE_RE = re.compile(
    r"\buntrad(?:eable|able)\b|\bnot\s+trad(?:eable|able)\b",
    re.IGNORECASE,
)
VALUE_PREFIX_RE = re.compile(r"^value\s*[-–—:]\s*", re.IGNORECASE)
STABILITY_SPLIT_RE = re.compile(r"\s+stability\b", re.IGNORECASE)
RARITY_WORDS_RE = re.compile(
    r"\b(legendaries|legendary|rares|rare|uncommons|uncommon|commons|common)\b",
    re.IGNORECASE,
)
CHROMA_PREFIX_RE = re.compile(
    r"^(?:c\.\s*|chroma\s+)",
    re.IGNORECASE,
)
CHROMA_SUFFIX_RE = re.compile(
    r"\s*\(?chroma\)?$",
    re.IGNORECASE,
)


def make_soup(markup):
    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:
        return BeautifulSoup(markup, "html.parser")


def norm_key(text):
    return " ".join(str(text).lower().split())


def load_aliases():
    aliases = {
        norm_key(key): value for key, value in DEFAULT_ALIASES.items()
    }

    if not ALIASES_PATH.exists():
        try:
            ALIASES_PATH.write_text(
                DEFAULT_ALIASES_FILE,
                encoding="utf8",
            )
        except Exception:
            pass

    try:
        if ALIASES_PATH.exists():
            try:
                text = ALIASES_PATH.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                text = ALIASES_PATH.read_text(encoding="cp1251")

            for line in text.splitlines():
                if "#" in line:
                    line = line.split("#", 1)[0]

                line = line.strip()
                if not line:
                    continue

                if "=" in line:
                    site_name, game_name = line.split("=", 1)
                elif "->" in line:
                    site_name, game_name = line.split("->", 1)
                else:
                    continue

                site_name = site_name.strip()
                game_name = game_name.strip()

                if site_name and game_name:
                    aliases[norm_key(site_name)] = game_name

    except Exception as exc:
        print(f"[aliases] failed to read aliases.txt: {exc}")

    return aliases


def load_item_types():
    """Read ItemName and ItemType from Roblox Lua item data."""
    result = {}
    if not ITEMS_PATH.exists():
        return result
    try:
        text = ITEMS_PATH.read_text(encoding="utf8", errors="ignore")
    except Exception:
        return result

    # The database uses both v1.Name = {...} and ["Name"] = {...}.
    block_re = re.compile(
        r'(?:v\d+\.)?([A-Za-z0-9_]+)\s*=\s*\{(.*?)(?=\n(?:v\d+\.)?[A-Za-z0-9_]+\s*=\s*\{|\Z)',
        re.DOTALL,
    )
    bracket_re = re.compile(
        r'\[\s*["\']([^"\']+)["\']\s*\]\s*=\s*\{(.*?)(?=\n\s*\[\s*["\']|\Z)',
        re.DOTALL,
    )
    name_re = re.compile(
        r'(?:\[\s*["\']ItemName["\']\s*\]|\bItemName)\s*=\s*["\']([^"\']+)["\']'
    )
    type_re = re.compile(
        r'(?:\[\s*["\']ItemType["\']\s*\]|\bItemType)\s*=\s*["\']([^"\']+)["\']'
    )

    for match in list(block_re.finditer(text)) + list(bracket_re.finditer(text)):
        key = match.group(1).strip()
        block = match.group(2)
        nm = name_re.search(block)
        tp = type_re.search(block)
        item_name = nm.group(1).strip() if nm else key
        item_type = tp.group(1).strip().lower() if tp else ""
        if item_type in {"knife", "gun"}:
            result[norm_key(item_name)] = item_type
            result.setdefault(norm_key(key), item_type)

    return result


ALIASES = load_aliases()
ITEM_TYPES = load_item_types()
# Known special item whose Supreme Values name does not contain its
# weapon type. The Roblox database identifies Corrupt as a Knife.
ITEM_TYPES.setdefault("corrupt", "knife")


def remove_paren_suffixes(name):
    if not name:
        return ""

    result = name.strip()
    previous = None

    while previous != result:
        previous = result
        result = PAREN_SUFFIX_RE.sub("", result).strip()

    return result


def remove_trailing_weapon_words(name):
    if not name:
        return ""

    result = TRAILING_WEAPON_RE.sub("", name).strip()
    return result or name.strip()


def normalize_chroma_name(name):
    """Convert Chroma Deathshard to Deathshard.

    This is important because the same item can exist on both the normal
    Godly page and the Chroma page.
    """
    result = name.strip()

    result = CHROMA_PREFIX_RE.sub("", result)
    result = CHROMA_SUFFIX_RE.sub("", result)
    result = result.strip()

    return result


def detect_weapon_from_text(text):
    if not text:
        return None

    if PAREN_KNIFE_RE.search(text):
        return "knife"

    if PAREN_GUN_RE.search(text):
        return "gun"

    if END_KNIFE_RE.search(text):
        return "knife"

    if END_GUN_RE.search(text):
        return "gun"

    return None


def flatten_attrs(col):
    parts = []

    for value in col.attrs.values():
        if isinstance(value, (list, tuple, set)):
            parts.extend(str(x) for x in value)
        else:
            parts.append(str(value))

    return " ".join(parts)


def detect_weapon_from_col(col):
    parts = list(col.get("class", []))
    parts.append(flatten_attrs(col))
    haystack = " ".join(parts).lower()

    if KNIFE_ANY_RE.search(haystack):
        return "knife"

    if GUN_ANY_RE.search(haystack):
        return "gun"

    return None


def detect_weapon_from_database(name):
    return ITEM_TYPES.get(norm_key(name))


def extract_value(col):
    lines = [
        line.strip()
        for line in col.get_text("\n").splitlines()
        if line.strip()
    ]

    for index, line in enumerate(lines):
        match = VALUE_PREFIX_RE.match(line)

        if match:
            rest = line[match.end():].strip()
        elif line.lower() == "value":
            rest = ""
        else:
            continue

        if not rest and index + 1 < len(lines):
            rest = lines[index + 1].strip()

        rest = STABILITY_SPLIT_RE.split(rest, maxsplit=1)[0].strip()
        rest = RARITY_WORDS_RE.sub("", rest).strip()
        rest = " ".join(rest.split())

        return rest or None

    return None


def is_untradable(col):
    text = " ".join(
        [
            col.get_text(" ", strip=True),
            flatten_attrs(col),
            " ".join(col.get("class", [])),
        ]
    ).lower()

    return bool(UNTRADABLE_RE.search(text))


def _parse_col(col, category, untradable=False):
    name_el = col.select_one(".itemhead")
    raw_name = (
        name_el.get_text(" ", strip=True)
        if name_el
        else None
    )

    if not raw_name:
        return None

    weapon_type = detect_weapon_from_text(raw_name)

    name_no_paren = raw_name.strip()

    if weapon_type is None:
        weapon_type = detect_weapon_from_text(name_no_paren)

    if weapon_type is None:
        weapon_type = detect_weapon_from_col(col)

    name = name_no_paren

    if category == "chromas":
        name = normalize_chroma_name(name)

    for candidate in (
        raw_name,
        name_no_paren,
        name,
    ):
        key = norm_key(candidate)

        if key in ALIASES:
            name = ALIASES[key]
            break

    if category == "chromas":
        name = normalize_chroma_name(name)

    name_before_weapon_cleanup = name
    if weapon_type is None:
        weapon_type = detect_weapon_from_text(
            name_before_weapon_cleanup
        )

    if weapon_type is None:
        weapon_type = detect_weapon_from_database(name)

    if not name:
        name = name_no_paren.strip() or raw_name.strip()

    if untradable or is_untradable(col):
        value = "untradable"
    else:
        value = extract_value(col)

        if value is None:
            value = None

    return {
        "name": " ".join(name.split()).strip(),
        "value": value,
        "type": weapon_type,
        "category": category,
    }


def empty_result():
    return {
        "Оружие": {
            "Ножи": {},
            "Пистолеты": {},
        },
        "Прочее": {},
    }


def get_rarity(category):
    """Return a real category instead of silently using unknown."""
    return RARITY_MAP.get(
        norm_key(category),
        norm_key(category) or "unknown",
    )


def add_leaf(container, name, value, category):
    rarity = get_rarity(category)

    if name not in container:
        container[name] = {}

    container[name][rarity] = value


def add_item(result, item):
    name = item.get("name")

    if not name:
        return

    item_type = item.get("type")
    category = item.get("category")
    value = item.get("value")

    if item_type == "knife":
        add_leaf(
            result["Оружие"]["Ножи"],
            name,
            value,
            category,
        )
    elif item_type == "gun":
        add_leaf(
            result["Оружие"]["Пистолеты"],
            name,
            value,
            category,
        )
    else:
        add_leaf(
            result["Прочее"],
            name,
            value,
            category,
        )


def count_result(result):
    count = 0

    for category in (
        result["Оружие"]["Ножи"],
        result["Оружие"]["Пистолеты"],
        result["Прочее"],
    ):
        for item_values in category.values():
            count += len(item_values)

    return count


def parse_category_page(markup, category):
    soup = make_soup(markup)
    result = []

    untradable = category in UNTRADABLE_CATEGORIES

    for col in soup.select(".itemcolumn"):
        item = _parse_col(
            col,
            category=category,
            untradable=untradable,
        )

        if item:
            result.append(item)

    return result


def scrape_fast():
    if not HAS_CURL:
        print("[fast] curl_cffi is not installed")
        return []

    items = []
    categories_with_data = 0

    with cf_requests.Session() as session:
        for index, category in enumerate(CATEGORIES):
            if index > 0:
                time.sleep(
                    random.uniform(
                        DELAY_MIN_SECONDS,
                        DELAY_MAX_SECONDS,
                    )
                )

            url = f"{BASE_PREFIX}{category}"

            try:
                response = session.get(
                    url,
                    impersonate="chrome",
                    timeout=30,
                )
                response.raise_for_status()
            except Exception as exc:
                print(
                    f"[fast] failed {category}: {exc}"
                )
                continue

            category_items = parse_category_page(
                response.text,
                category,
            )

            if category_items:
                categories_with_data += 1
                items.extend(category_items)

            print(
                f"[fast] {category}: "
                f"{len(category_items)}"
            )

    if (
        len(items) < MIN_ITEMS_SANITY
        or categories_with_data < MIN_CATEGORIES_SANITY
    ):
        print(
            "[fast] sanity check failed: "
            f"{len(items)} items from "
            f"{categories_with_data} categories"
        )
        return []

    return items


def scrape_browser():
    try:
        import undetected_chromedriver as uc
    except ImportError:
        print(
            "[browser] undetected_chromedriver is not installed"
        )
        return []

    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = None
    items = []
    categories_with_data = 0

    try:
        driver = uc.Chrome(options=options)

        for index, category in enumerate(CATEGORIES):
            if index > 0:
                time.sleep(
                    random.uniform(
                        DELAY_MIN_SECONDS,
                        DELAY_MAX_SECONDS,
                    )
                )

            url = f"{BASE_PREFIX}{category}"

            print(f"[browser] parsing {url}")

            try:
                driver.get(url)
                time.sleep(5)

                category_items = parse_category_page(
                    driver.page_source,
                    category,
                )

            except Exception as exc:
                print(
                    f"[browser] failed {category}: {exc}"
                )
                continue

            if category_items:
                categories_with_data += 1
                items.extend(category_items)

            print(
                f"[browser] {category}: "
                f"{len(category_items)}"
            )

    except Exception as exc:
        print(f"[browser] error: {exc}")

    finally:
        if driver is not None:
            driver.quit()

    if (
        len(items) < MIN_ITEMS_SANITY
        or categories_with_data < MIN_CATEGORIES_SANITY
    ):
        print(
            "[browser] sanity check failed: "
            f"{len(items)} items from "
            f"{categories_with_data} categories"
        )
        return []

    return items


def build_result(items):
    result = empty_result()

    for item in items:
        add_item(result, item)

    return result


def write_meta(result):
    now = datetime.now(timezone.utc)

    knives = len(result["Оружие"]["Ножи"])
    guns = len(result["Оружие"]["Пистолеты"])
    other = len(result["Прочее"])
    total = count_result(result)

    meta = {
        "updatedAt": now.isoformat(),
        "count": total,
        "knives": knives,
        "guns": guns,
        "other": other,
    }

    META_PATH.write_text(
        json.dumps(
            meta,
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf8",
    )


def main():
    items = scrape_fast()

    if not items:
        items = scrape_browser()

    if not items:
        print(
            "FAILED: no data was collected. "
            "Check access to supremevalues.com"
        )
        return

    result = build_result(items)
    total = count_result(result)

    if total == 0:
        print(
            "FAILED: the parser produced zero items"
        )
        return

    PRICES_PATH.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf8",
    )

    write_meta(result)

    print(
        f"[done] updated {total} values "
        f"into {PRICES_PATH}"
    )


if __name__ == "__main__":
    main()
