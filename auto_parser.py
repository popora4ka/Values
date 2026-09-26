"""Парсер цен MM2 с supremevalues.com.

Значение — ТОЧНО видимая строка с карточки после "Value - ":
    "Value - x4 T1 Legendaries" -> "x4 T1 Legendaries"
    "Value - x3 T1 Rares"       -> "x3 T1 Rares"
    "Value - Priceless"         -> "Priceless"
Скрытые data-value/data-price НЕ читаются — оттуда брались лишние числа (625, 0.6...).
Если строки "Value - ..." нет или предмет не торгуется — значение "untradable".

Названия: хвостовые Gun/Knife и суффиксы в скобках вырезаются;
ножи и пистолеты хранятся раздельно, чтобы "Palms" не конфликтовал.
aliases.txt — словарик-памятка (регистр НЕ важен): Сайтовое имя = Игровое имя.

Результат: prices.json =
{
    "Оружие": {"Ножи": {"Palms": {"rare": 2, "godly_chroma": 50}}, ...}
}
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

BASE_PREFIX = "https://supremevalues.com/mm2/"

CATEGORIES = [
    "godlies", "ancients", "chromas", "vintages",
    "collectibles", "pets", "legendaries", "rares",
    "uncommons", "commons", "sets", "uniques", "evos",
    "misc", "untradables",
]
UNTRADABLE_CATEGORIES = {"untradables"}

RARITY_MAP = {
    "commons": "common", "uncommons": "uncommon", "rares": "rare",
    "legendaries": "legendary", "godlies": "godly_chroma", "chromas": "godly_chroma",
    "vintages": "vintage", "ancients": "ancient_evo", "evos": "ancient_evo"
}

MIN_ITEMS_SANITY = 100
MIN_CATEGORIES_SANITY = 5
DELAY_MIN_SECONDS = 3
DELAY_MAX_SECONDS = 6

DEFAULT_ALIASES = {"bioblade": "Bio Blade"}
DEFAULT_ALIASES_FILE = """# Памятка исключений: Сайтовое имя = Игровое имя
# Регистр НЕ важен: "battleaxe ii = Battleaxe II" сработает.
# Строки с # игнорируются. Можно также писать "Сайтовое -> Игровое".
Bioblade = Bio Blade
"""

# ---------- чистка названий ----------
PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
TRAILING_WEAPON_RE = re.compile(r"[\s\-–—]*(gun|knife)\s*$", re.IGNORECASE)
PAREN_KNIFE_RE = re.compile(r"\(\s*knife\s*\)", re.IGNORECASE)
PAREN_GUN_RE = re.compile(r"\(\s*gun\s*\)", re.IGNORECASE)
END_KNIFE_RE = re.compile(r"\bknife\b\s*$", re.IGNORECASE)
END_GUN_RE = re.compile(r"\bgun\b\s*$", re.IGNORECASE)
KNIFE_ANY_RE = re.compile(r"\bknife\b", re.IGNORECASE)
GUN_ANY_RE = re.compile(r"\bgun\b", re.IGNORECASE)
UNTRADABLE_RE = re.compile(r"\buntrad(?:eable|able)\b|\bnot\s+trad(?:eable|able)\b", re.IGNORECASE)

# ---------- строка значения ----------
VALUE_PREFIX_RE = re.compile(r"^value\s*[-–—:]\s*", re.IGNORECASE)
STABILITY_SPLIT_RE = re.compile(r"\s+stability\b", re.IGNORECASE)
RARITY_WORDS_RE = re.compile(
    r"\b(legendaries|legendary|rares|rare|uncommons|uncommon|commons|common)\b",
    re.IGNORECASE
)


def make_soup(markup):
    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:
        return BeautifulSoup(markup, "html.parser")


def norm_key(text):
    return " ".join(str(text).lower().split())


def load_aliases():
    aliases = dict(DEFAULT_ALIASES)
    if not ALIASES_PATH.exists():
        try:
            ALIASES_PATH.write_text(DEFAULT_ALIASES_FILE, encoding="utf-8")
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
                site_name, game_name = site_name.strip(), game_name.strip()
                if site_name and game_name:
                    aliases[norm_key(site_name)] = game_name
    except Exception as exc:
        print(f"[aliases] не удалось прочитать aliases.txt: {exc}")
    return aliases


ALIASES = load_aliases()


def remove_paren_suffixes(name):
    if not name:
        return ""
    result = name.strip()
    prev = None
    while prev != result:
        prev = result
        result = PAREN_SUFFIX_RE.sub("", result).strip()
    return result


def remove_trailing_weapon_words(name):
    if not name:
        return ""
    result = TRAILING_WEAPON_RE.sub("", name).strip()
    return result or name.strip()


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


def extract_value(col):
    lines = [ln.strip() for ln in col.get_text("\n").splitlines()]
    for idx, line in enumerate(lines):
        m = VALUE_PREFIX_RE.match(line)
        if m:
            rest = line[m.end():].strip()
        elif line.lower() == "value":
            rest = ""
        else:
            continue
        if not rest and idx + 1 < len(lines):
            rest = lines[idx + 1].strip()
        rest = STABILITY_SPLIT_RE.split(rest, maxsplit=1)[0].strip()
        rest = RARITY_WORDS_RE.sub("", rest).strip()
        rest = " ".join(rest.split())
        return rest or None
    return None


def is_untradable(col):
    text = " ".join([col.get_text(" ", strip=True), flatten_attrs(col),
                     " ".join(col.get("class", []))]).lower()
    return bool(UNTRADABLE_RE.search(text))


def _parse_col(col, untradable=False):
    name_el = col.select_one(".itemhead")
    raw_name = name_el.get_text(" ", strip=True) if name_el else None
    if not raw_name:
        return None

    weapon_type = detect_weapon_from_text(raw_name)
    name_no_paren = remove_paren_suffixes(raw_name)
    if weapon_type is None:
        weapon_type = detect_weapon_from_text(name_no_paren)
    if weapon_type is None:
        weapon_type = detect_weapon_from_col(col)

    name = remove_trailing_weapon_words(name_no_paren)

    for candidate in (raw_name, name_no_paren, name):
        key = norm_key(candidate)
        if key in ALIASES:
            name = ALIASES[key]
            break

    name = remove_paren_suffixes(name)
    name_before = name
    name = remove_trailing_weapon_words(name)
    if weapon_type is None:
        weapon_type = detect_weapon_from_text(name_before)
    name = " ".join(name.split()).strip()
    if not name:
        name = name_no_paren.strip() or raw_name.strip()

    if untradable or is_untradable(col):
        value = "untradable"
    else:
        value = extract_value(col)
        if value is None:
            value = "untradable"

    return {"name": name, "value": value, "type": weapon_type}


def empty_result():
    return {"Оружие": {"Ножи": {}, "Пистолеты": {}}, "Прочее": {}}


def add_leaf(container, name, value, category):
    rarity = RARITY_MAP.get(category, "unknown")
    if name not in container:
        container[name] = {}
    
    # Если значение уже есть, не перезаписываем втупую, ищем свободный слот
    if rarity in container[name] and container[name][rarity] != value:
        base, n = rarity, 2
        while f"{base}_{n}" in container[name]:
            n += 1
        container[name][f"{base}_{n}"] = value
    else:
        container[name][rarity] = value


def add_item(result, item):
    name = item.get("name")
    if not name:
        return
    cat = item.get("category")
    if item.get("type") == "knife":
        add_leaf(result["Оружие"]["Ножи"], name, item["value"], cat)
    elif item.get("type") == "gun":
        add_leaf(result["Оружие"]["Пистолеты"], name, item["value"], cat)
    else:
        add_leaf(result["Прочее"], name, item["value"], cat)


def count_result(result):
    count = 0
    for cat in (result["Оружие"]["Ножи"], result["Оружие"]["Пистолеты"], result["Прочее"]):
        for item_rarities in cat.values():
            count += len(item_rarities)
    return count


def scrape_fast():
    if not HAS_CURL:
        print("[fast] curl_cffi не установлен — пропуск")
        return []
    items = []
    categories_with_data = 0
    with cf_requests.Session() as session:
        for i, category in enumerate(CATEGORIES):
            category = category.strip()
            if i > 0:
                time.sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
            url = f"{BASE_PREFIX}{category}"
            untradable = category in UNTRADABLE_CATEGORIES
            try:
                resp = session.get(url, impersonate="chrome", timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                print(f"[fast] не получилось '{category}': {exc}")
                continue
            soup = make_soup(resp.text)
            count = 0
            for col in soup.select(".itemcolumn"):
                item = _parse_col(col, untradable=untradable)
                if item:
                    item["category"] = category  # Прокидываем категорию
                    items.append(item)
                    count += 1
            if count:
                categories_with_data += 1
            print(f"[fast] {category}: {count}")
    if len(items) < MIN_ITEMS_SANITY or categories_with_data < MIN_CATEGORIES_SANITY:
        print(f"[fast] мало данных: {len(items)} предметов из {categories_with_data} категорий")
        return []
    return items


def scrape_browser():
    try:
        import undetected_chromedriver as uc
    except ImportError:
        print("[browser] undetected_chromedriver не установлен — пропуск")
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
        for i, category in enumerate(CATEGORIES):
            category = category.strip()
            if i > 0:
                time.sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
            url = f"{BASE_PREFIX}{category}"
            untradable = category in UNTRADABLE_CATEGORIES
            print(f"[browser] парсим: {url}")
            try:
                driver.get(url)
                time.sleep(5)
                soup = make_soup(driver.page_source)
            except Exception as exc:
                print(f"[browser] не получилось '{category}': {exc}")
                continue
            count = 0
            for col in soup.select(".itemcolumn"):
                item = _parse_col(col, untradable=untradable)
                if item:
                    item["category"] = category  # Прокидываем категорию
                    items.append(item)
                    count += 1
            if count:
                categories_with_data += 1
    except Exception as exc:
        print(f"[browser] ошибка: {exc}")
    finally:
        if driver is not None:
            driver.quit()
    if len(items) < MIN_ITEMS_SANITY or categories_with_data < MIN_CATEGORIES_SANITY:
        print(f"[browser] мало данных: {len(items)} предметов")
        return []
    return items


def main():
    items = scrape_fast()
    if not items:
        items = scrape_browser()

    result = empty_result()
    for item in items:
        add_item(result, item)

    total = count_result(result)
    if total == 0:
        print("НЕ УДАЛОСЬ собрать цены. Проверьте доступ к supremevalues.com")
        return

    now = datetime.now(timezone.utc)
    PRICES_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=4), encoding="utf-8")
    META_PATH.write_text(json.dumps({
        "updatedAt": now.isoformat(),
        "count": total,
        "knives": count_result({"Оружие": {"Ножи": result["Оружие"]["Ножи"], "Пистолеты": {}}, "Прочее": {}}),
        "guns": count_result({"Оружие": {"Ножи": {}, "Пистолеты": result["Оружие"]["Пистолеты"]}, "Прочее": {}}),
        "other": count_result({"Оружие": {"Ножи": {}, "Пистолеты": {}}, "Прочее": result["Прочее"]}),
    }, ensure_ascii=False, indent=4), encoding="utf-8")
    print(f"[done] успешно обновлено: {total} предметов -> {PRICES_PATH}")


if __name__ == "__main__":
    main()
