import datetime
import fcntl
import functools
import re
import json
import logging
import os
import random
import shutil
import time

import requests

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _value = _line.partition("=")
                os.environ.setdefault(_key.strip(), _value.strip())

SEARCH_CONDITIONS = os.environ["SEARCH_CONDITIONS"]
PAGE_URL = "https://divar.ir/s/" + SEARCH_CONDITIONS
API_URL = "https://api.divar.ir/v8/postlist/w/search"
AD_URL = "https://api.divar.ir/v8/posts-v2/web/"
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_CHATID = os.environ["BOT_CHATID"]

proxy_config = {}
if os.environ.get("HTTP_PROXY", ""):
    proxy_config["http"] = os.environ["HTTP_PROXY"]
if os.environ.get("HTTPS_PROXY", ""):
    proxy_config["https"] = os.environ["HTTPS_PROXY"]

MIN_FREE_BYTES = 1 << 20  # 1 MiB; tokens.json is a few KB
# shown in the main table, in this order; everything else goes in the dropdown
KEY_SPECS = (
    "متراژ",
    "ساخت",
    "اتاق",
    "متراژ زمین",
    "نوع بنا",
    "قیمت کل",
    "وام",
    "پارکینگ",
    "آسانسور",
    "انباری",
)

MAX_PHOTOS = 10  # rich messages allow 50; an ad rarely has more than 10 useful ones
STORAGE_WARNING_MD = "⚠️ **حافظه پر است** - آگهی‌های ارسال‌شده ذخیره نمی‌شوند"
STORAGE_WARNING = "⚠️ <b>حافظه پر است</b> - آگهی‌های ارسال‌شده ذخیره نمی‌شوند"
STORAGE_FULL = False

_HERE = os.path.dirname(os.path.realpath(__file__))
TOKEN_PATH = os.path.join(_HERE, "tokens.json")
PARAMS_PATH = os.path.join(_HERE, "search_params.json")

# telegram allows ~20 messages/min to a group, ~30/s overall
SEND_INTERVAL = 3 if BOT_CHATID.lstrip().startswith("-") else 1

# stop conditions for the page walk, so a cold start cannot run forever
MAX_PAGES = int(os.environ.get("MAX_PAGES", 30))
MAX_AGE_DAYS = float(os.environ.get("MAX_AGE_DAYS", 0))  # 0 disables the age cutoff

# label prefixed to every ad, to tell apart bots posting to the same chat
PRE_TEXT = os.environ.get("PRE_TEXT", "").strip()

# comma-separated words; ads whose title contains any of them are skipped
EXCLUDE_TITLE = [
    w.strip() for w in os.environ.get("EXCLUDE_TITLE", "").split(",") if w.strip()
]


def find_key(obj, key):
    """First value for `key` anywhere in a nested dict/list."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = find_key(child, key)
        if found is not None:
            return found
    return None


def scrape_search_params():
    """divar.ir/s/<conditions> renders the filters the JSON API wants; steal them."""
    html = requests.get(
        PAGE_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        proxies=proxy_config,
    ).text
    marker = "window.__PRELOADED_STATE__ = "
    if marker not in html:
        raise ValueError(f"no search state in {len(html)} bytes of html")
    state, _ = json.JSONDecoder().raw_decode(html[html.index(marker) + len(marker) :])
    info = find_key(state, "search_data")
    cities = find_key(state, "cities")
    if not info or not cities:
        raise ValueError("search state has no filters (divar is throttling us)")
    return cities, json.loads(info["form_data_json"])


@functools.lru_cache(maxsize=1)
def get_search_params():
    """The filters only change when SEARCH_CONDITIONS does, so scrape once and keep them."""
    try:
        with open(PARAMS_PATH) as cached:
            params = json.load(cached)
        if params["conditions"] == SEARCH_CONDITIONS:
            return params["cities"], params["form_data"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    cities, form_data = scrape_search_params()
    with open(PARAMS_PATH, "w") as out:
        json.dump(
            {
                "conditions": SEARCH_CONDITIONS,
                "cities": cities,
                "form_data": form_data,
            },
            out,
            ensure_ascii=False,
        )
    logging.info("cached search filters in %s", PARAMS_PATH)
    return cities, form_data


def get_data(pagination=None):
    cities, form_data = get_search_params()
    body = {
        "city_ids": cities,
        "search_data": {"form_data": form_data},
    }
    if pagination:
        body["pagination_data"] = pagination
    return requests.post(API_URL, json=body, proxies=proxy_config)


def parse_data(data):
    return json.loads(data.text)


def get_houses_list(data):
    posts = [w for w in data.get("list_widgets", []) if w["widget_type"] == "POST_ROW"]
    if not posts:
        logging.warning("no posts in response: %s", str(data)[:200])
    return posts


def extract_house_data(house):
    data = house["data"]
    payload = data["action"]["payload"]
    web_info = payload.get("web_info", {})

    return {
        "title": data["title"],
        "description": "\n".join(
            data[k]
            for k in ("middle_description_text", "bottom_description_text")
            if data.get(k)
        ),
        # some ads carry only a city, no district
        "district": web_info.get("district_persian") or web_info.get("city_persian", ""),
        "imageCount": data.get("image_count", 0),
        "imageUrl": data.get("image_url", ""),
        "token": payload["token"],
    }


def iter_widgets(node):
    """Divar nests widgets inside sections and expandable sections; flatten them."""
    if isinstance(node, dict):
        if "widget_type" in node:
            yield node["widget_type"], node.get("data", {})
        for value in node.values():
            yield from iter_widgets(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_widgets(value)


def get_ad_details(token):
    """The list response has one thumbnail; the ad page has every photo and spec."""
    response = requests.get(AD_URL + token, proxies=proxy_config)
    images, specs, features, descriptions = [], [], [], []
    for widget_type, data in iter_widgets(parse_data(response)):
        items = data.get("items", [])
        if widget_type == "IMAGE_CAROUSEL":
            images += [i["image"]["url"] for i in items if "image" in i]
        elif widget_type == "GROUP_INFO_ROW":
            # divar labels these itself, so any category works without a mapping
            specs += [(i["title"], i["value"]) for i in items if i.get("value")]
        elif widget_type == "UNEXPANDABLE_ROW" and data.get("value"):
            specs.append((data["title"], data["value"]))
        elif widget_type == "GROUP_FEATURE_ROW":
            features += [feature_spec(i) for i in items if i.get("title")]
        elif widget_type == "DESCRIPTION_ROW":
            descriptions.append(data.get("text", ""))
    return {
        "images": images[:MAX_PHOTOS],
        "specs": specs,
        "features": features,
        # the first DESCRIPTION_ROW is divar's publish-date block; the ad text is longer
        "description": max(descriptions, key=len, default=""),
    }


def feature_spec(item):
    """A feature carries no value of its own: `available` is false when the ad lacks
    it, and divar sometimes bakes the negation into the title instead."""
    title = item["title"]
    stripped = re.sub(r"\s*(ن?دارد)$", "", title)
    has = item.get("available", True) and not title.endswith("ندارد")
    return stripped, "دارد" if has else "ندارد"


def escape_markdown(text):
    return re.sub(r"([\\`*_~=|\[\]#>!+-])", r"\\\1", text)


def spec_table(specs):
    """Value first, label second: the message is RTL, so the label lands on the left."""
    rows = "\n".join(
        f"| {escape_markdown(v)} | {escape_markdown(k)} |" for k, v in specs
    )
    return f"| مقدار | ویژگی |\n|:---|---:|\n{rows}"


def build_markdown(house, details):
    """One rich message: slideshow, spec table, collapsible description."""
    parts = []
    if PRE_TEXT:
        parts.append(PRE_TEXT)
    parts.append(f"## {escape_markdown(house['title'])}")
    if house["district"]:
        parts.append(f"*{escape_markdown(house['district'])}*")

    if details["images"]:
        photos = "\n".join(f"![]({url})" for url in details["images"])
        parts.append(f"<tg-slideshow>\n{photos}\n</tg-slideshow>")

    specs = details["specs"] + details["features"]
    key_specs = sorted(
        (s for s in specs if s[0] in KEY_SPECS), key=lambda s: KEY_SPECS.index(s[0])
    )
    other_specs = [s for s in specs if s[0] not in KEY_SPECS]

    if key_specs:
        parts.append(spec_table(key_specs))
    if other_specs:
        parts.append(
            "<details><summary>سایر مشخصات</summary>\n\n"
            + spec_table(other_specs)
            + "\n</details>"
        )

    if details["description"]:
        body = escape_markdown(details["description"])
        parts.append(f"<details><summary>توضیحات</summary>\n\n{body}\n</details>")

    parts.append(f"https://divar.ir/v/a/{house['token']}")
    if STORAGE_FULL:
        parts.append(STORAGE_WARNING_MD)
    return "\n\n".join(parts)


def send_rich_message(house):
    """Falls back to the plain photo message if divar or telegram says no."""
    try:
        details = get_ad_details(house["token"])
    except (requests.RequestException, json.JSONDecodeError, KeyError) as err:
        logging.warning("no details for %s: %s", house["token"], err)
        return send_telegram_message(house)

    rich = {"markdown": build_markdown(house, details), "is_rtl": True}
    result = telegram_call("sendRichMessage", {"rich_message": json.dumps(rich)})
    if result is None:
        logging.warning("sendRichMessage rejected %s, sending a plain ad", house["token"])
        return send_telegram_message(house)
    return result


def telegram_call(method, body):
    """True/False on success or failure, None when telegram rejects the request."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    body = {"chat_id": BOT_CHATID, **body}
    for _ in range(5):
        try:
            result = requests.post(url, data=body, proxies=proxy_config)
        except requests.RequestException as err:
            # unreachable proxy or telegram; the ad stays unsent and retries next run
            logging.error("%s failed: %s", method, err)
            return False
        if result.status_code == 400:
            logging.warning("%s refused: %s", method, result.text[:200])
            return None
        if result.status_code != 429:
            return result.ok
        # telegram flood wait: it tells us exactly how long to back off
        wait = result.json().get("parameters", {}).get("retry_after", 5)
        logging.warning("flood wait %ss", wait)
        time.sleep(wait + random.uniform(0, 1))
    logging.error("giving up on a message after repeated flood waits")
    return False


def send_text(text, photo=None):
    body = {"parse_mode": "HTML"}
    if photo:
        body.update(photo=photo, caption=text)
        result = telegram_call("sendPhoto", body)
        if result is None:
            # telegram could not fetch the thumbnail; the ad still matters
            logging.warning("falling back to a text message")
            return send_text(text)
        return result
    return telegram_call("sendMessage", {**body, "text": text}) is True


def send_telegram_message(house):
    text = f"{PRE_TEXT}\n" if PRE_TEXT else ""
    text += f"<b>{house['title']}</b>" + "\n"
    text += f"<i>{house['district']}</i>" + "\n"
    text += f"{house['description']}" + "\n"
    text += f"<i>تصویر : </i> {house['imageCount']}\n\n"
    text += f"https://divar.ir/v/a/{house['token']}"
    if STORAGE_FULL:
        text += "\n\n" + STORAGE_WARNING
    # captions cap at 1024 chars; text messages get 4096
    photo = house["imageUrl"] if house["imageCount"] and len(text) <= 1024 else None
    return send_text(text, photo)


def load_tokens():
    try:
        with open(TOKEN_PATH) as content:
            return json.load(content)
    except (FileNotFoundError, json.JSONDecodeError):
        # first run, or the file was truncated by a crash mid-write
        logging.info("starting with an empty %s", TOKEN_PATH)
        return []


def save_tokns(tokens):
    """Write via a temp file so a full disk leaves the old tokens.json intact."""
    global STORAGE_FULL
    tmp_path = TOKEN_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as outfile:
            json.dump(tokens, outfile)
            outfile.flush()
            os.fsync(outfile.fileno())
        os.replace(tmp_path, TOKEN_PATH)
        return True
    except OSError as err:
        logging.error("could not save tokens: %s", err)
        if not STORAGE_FULL:
            # the free-space check missed it, so no ad carried the warning
            STORAGE_FULL = True
            send_text(STORAGE_WARNING)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


def storage_full():
    free = shutil.disk_usage(os.path.dirname(TOKEN_PATH)).free
    return free < MIN_FREE_BYTES


def page_too_old(page_info):
    """Divar dates the oldest ad of the page; once that is past the cutoff, stop."""
    last_date = page_info.get("data", {}).get("last_post_date")
    if not MAX_AGE_DAYS or not last_date:
        return False
    oldest = datetime.datetime.fromisoformat(last_date.replace("Z", "+00:00"))
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=MAX_AGE_DAYS
    )
    return oldest < cutoff


def get_data_page(tokens):
    """Walk pages, oldest ad first, until one holds an ad we already sent."""
    houses = []
    pagination = None
    for _ in range(MAX_PAGES):
        data = parse_data(get_data(pagination))
        page = get_houses_list(data)
        houses += page
        if any(extract_house_data(h)["token"] in tokens for h in page):
            break
        page_info = data.get("pagination", {})
        if page_too_old(page_info):
            logging.info("reached ads older than %s days", MAX_AGE_DAYS)
            break
        if not page_info.get("has_next_page"):
            break
        pagination = page_info["data"]
        time.sleep(1)
    logging.info("fetched %s ads", len(houses))
    return houses[::-1]


def lock_or_exit():
    """One run at a time; the cron fires every minute but a backfill takes longer."""
    lock = open(TOKEN_PATH + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.info("another run is still going, skipping this one")
        raise SystemExit(0)
    return lock  # kept open on purpose: closing it drops the lock


def process_data(data, tokens):
    for house in data:
        house_data = extract_house_data(house)
        if house_data is None:
            continue
        if house_data["token"] in tokens:
            continue
        if any(w in house_data["title"] for w in EXCLUDE_TITLE):
            continue

        if send_rich_message(house_data):
            tokens.append(house_data["token"])
        time.sleep(SEND_INTERVAL)
    return tokens


if __name__ == "__main__":
    logging.info(datetime.datetime.now())
    _lock = lock_or_exit()
    STORAGE_FULL = storage_full()
    if STORAGE_FULL:
        logging.error("disk almost full; tokens will not be saved")
    tokens = load_tokens()
    logging.info(len(tokens))
    tokens = process_data(get_data_page(tokens), tokens)

    save_tokns(tokens)
