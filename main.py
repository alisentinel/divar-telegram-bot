import datetime
import functools
import json
import logging
import os
import random
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

PAGE_URL = "https://divar.ir/s/" + os.environ["SEARCH_CONDITIONS"]
API_URL = "https://api.divar.ir/v8/postlist/w/search"
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_CHATID = os.environ["BOT_CHATID"]

proxy_config = {}
if os.environ.get("HTTP_PROXY", ""):
    proxy_config["http"] = os.environ["HTTP_PROXY"]
if os.environ.get("HTTPS_PROXY", ""):
    proxy_config["https"] = os.environ["HTTPS_PROXY"]

TOKENS = list()

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


@functools.lru_cache(maxsize=1)
def get_search_params():
    """divar.ir/s/<conditions> renders the filters the JSON API wants; steal them."""
    html = requests.get(
        PAGE_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        proxies=proxy_config,
    ).text
    marker = "window.__PRELOADED_STATE__ = "
    state, _ = json.JSONDecoder().raw_decode(html[html.index(marker) + len(marker) :])
    info = find_key(state, "search_data")
    return find_key(state, "cities"), json.loads(info["form_data_json"])


def get_data(page=None):
    cities, form_data = get_search_params()
    body = {
        "city_ids": cities,
        "search_data": {"form_data": form_data},
    }
    if page:
        body["pagination_data"] = {
            "@type": "type.googleapis.com/post_list.PaginationData",
            "page": int(page),
            "layer_page": int(page),
        }
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
        "hasImage": data.get("image_count", 0) > 0,
        "token": payload["token"],
    }


def send_telegram_message(house):
    url = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"
    text = f"<b>{house['title']}</b>" + "\n"
    text += f"<i>{house['district']}</i>" + "\n"
    text += f"{house['description']}" + "\n"
    text += f'<i>تصویر : </i> {"✅" if house["hasImage"] else "❌"}\n\n'
    text += f"https://divar.ir/v/a/{house['token']}"
    body = {"chat_id": BOT_CHATID, "parse_mode": "HTML", "text": text}
    result = requests.post(url, data=body, proxies=proxy_config)
    if result.status_code == 429:
        time.sleep(random.randint(3, 7))
        send_telegram_message(house)


def load_tokens():
    token_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "tokens.json"
    )
    with open(token_path, "r") as content:
        if content == "":
            return []
        return json.load(content)


def save_tokns(tokens):
    token_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "tokens.json"
    )
    with open(token_path, "w") as outfile:
        json.dump(tokens, outfile)


def get_data_page(page=None):
    data = get_data(page)
    data = parse_data(data)
    data = get_houses_list(data)
    data = data[::-1]
    return data


def process_data(data, tokens):
    for house in data:
        house_data = extract_house_data(house)
        if house_data is None:
            continue
        if house_data["token"] in tokens:
            continue
        if any(w in house_data["title"] for w in EXCLUDE_TITLE):
            continue

        tokens.append(house_data["token"])
        send_telegram_message(house_data)
        time.sleep(1)
    return tokens


if __name__ == "__main__":
    logging.info(datetime.datetime.now())
    tokens = load_tokens()
    logging.info(len(tokens))
    pages = [2, ""]
    for page in pages:
        data = get_data_page(page)
        tokens = process_data(data, tokens)

    save_tokns(tokens)
