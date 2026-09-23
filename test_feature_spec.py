"""python3 test_feature_spec.py"""
import os

os.environ.setdefault("SEARCH_CONDITIONS", "{}")
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("BOT_CHATID", "x")

from main import feature_spec

assert feature_spec({"title": "آسانسور", "available": True}) == ("آسانسور", "دارد")
assert feature_spec({"title": "آسانسور", "available": False}) == ("آسانسور", "ندارد")
assert feature_spec({"title": "آسانسور ندارد"}) == ("آسانسور", "ندارد")
assert feature_spec({"title": "پارکینگ"}) == ("پارکینگ", "دارد")
print("ok")
