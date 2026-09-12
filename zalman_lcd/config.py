# -*- coding: utf-8 -*-
"""zalman-display configuration (JSON in ~/.config/zalman-lcd/config.json)."""

import copy
import glob
import json
import os
import shutil

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "zalman-lcd")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CACHE_DIR = os.path.join(CONFIG_DIR, "media")   # exactly one background file

DEFAULTS = {
    "background": None,        # cached image/GIF/video path (None => black)
    "bg_name": None,           # original filename (shown in status)
    "brightness": 80,          # 0..100
    "rotate": 0,               # 0/90/180/270
    "fps": 20,                 # video/GIF frame rate
    "show_stats": True,        # show the metrics line
    "text_color": "FFFFFF",    # metrics text color, HEX
    "position": "down",        # up / down
    "stats_bg": "off",         # text backing: off / white / black (30% alpha)
    "gpu": "auto",             # GPU to display: auto / nvidia / cardN
}


def load():
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except Exception:
        data = {}
    cfg = copy.deepcopy(DEFAULTS)
    for k in DEFAULTS:                 # known keys only
        if k in data:
            cfg[k] = data[k]
    return cfg


def save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def update(**kw):
    cfg = load()
    cfg.update(kw)
    save(cfg)
    return cfg


def mtime():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0.0


def cache_background(src):
    """Copy the background into the cache as its only file (remove the old one).
    Return the cached copy's path. The original is no longer needed."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(CACHE_DIR, "*")):
        try:
            os.remove(old)
        except OSError:
            pass
    ext = os.path.splitext(src)[1].lower() or ".img"
    dst = os.path.join(CACHE_DIR, "background" + ext)
    shutil.copy2(src, dst)
    return dst


def clear_background_cache():
    for old in glob.glob(os.path.join(CACHE_DIR, "*")):
        try:
            os.remove(old)
        except OSError:
            pass
