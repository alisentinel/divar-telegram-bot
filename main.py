import datetime
import fcntl
import functools
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
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_CHATID = os.environ["BOT_CHATID"]

proxy_config = {}
if os.environ.get("HTTP_PROXY", ""):
    proxy_config["http"] = os.environ["HTTP_PROXY"]
if os.environ.get("HTTPS_PROXY", ""):
    proxy_config["https"] = os.environ["HTTPS_PROXY"]

MIN_FREE_BYTES = 1 << 20  # 1 MiB; tokens.json is a few KB
STORAGE_WARNING = "⚠️ <b>حافظه پر است</b> - آگهی‌های ارسال‌شده ذخیره نمی‌شوند"
STORAGE_FULL = False

_HERE = os.path.dirname(os.path.realpath(__file__))
TOKEN_PATH = os.path.join(_HERE, "tokens.json")
PARAMS_PATH = os.path.join(_HERE, "search_params.json")

# telegram allows ~20 messages/min to a group, ~30/s overall
SEND_INTERVAL = 3 if BOT_CHATID.lstrip().startswith("-") else 1

# ponytail: safety net so an empty tokens.json cannot walk divar forever
PAGE_LIMIT = 30

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


def send_text(text, photo=None):
    method = "sendPhoto" if photo else "sendMessage"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    body = {"chat_id": BOT_CHATID, "parse_mode": "HTML"}
    if photo:
        body["photo"] = photo
        body["caption"] = text
    else:
        body["text"] = text
    for _ in range(5):
        result = requests.post(url, data=body, proxies=proxy_config)
        if photo and result.status_code == 400:
            # telegram could not fetch the thumbnail; the ad still matters
            logging.warning("sendPhoto rejected %s, falling back to text", photo)
            return send_text(text)
        if result.status_code != 429:
            return result.ok
        # telegram flood wait: it tells us exactly how long to back off
        wait = result.json().get("parameters", {}).get("retry_after", 5)
        logging.warning("flood wait %ss", wait)
        time.sleep(wait + random.uniform(0, 1))
    logging.error("giving up on a message after repeated flood waits")
    return False


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


def get_data_page(tokens):
    """Walk pages, oldest ad first, until one holds an ad we already sent."""
    houses = []
    pagination = None
    for _ in range(PAGE_LIMIT):
        data = parse_data(get_data(pagination))
        page = get_houses_list(data)
        houses += page
        if any(extract_house_data(h)["token"] in tokens for h in page):
            break
        page_info = data.get("pagination", {})
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

        if send_telegram_message(house_data):
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
