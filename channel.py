here#!/usr/bin/env python3
"""
CHANNEL STUDIO - an autonomous comedy channel (English, global audience).

Every run it acts like a small studio:
  trends/calendar -> writers' room (many pitches, panel ranks them, best one ships) ->
  director (code decides pacing, camera, sound design) -> frame-by-frame renderer ->
  mix (music flips at the punchline, sfx, ducking, loudness) -> YouTube upload (scheduled).

Nothing is ever "skipped for not being funny": the best candidate always ships. No rewrite loops.
Formats: silent meme micro-skits (~10-15s), sequences, split-screen, fake chats, voiced dialogues,
and a 6-10 minute compilation for the long video.

Secrets (GitHub Actions -> env):
  GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3     text + voice (rotated, models auto-discovered)
  CF_ACCOUNT_ID, CF_API_TOKEN                            images (Cloudflare Workers AI)
  PIXABAY_API_KEY                                        real stock clips + image fallback
  YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN       YouTube upload
  optional: YT_API_KEY (read stats for the learning loop)

Your own assets (all optional, just upload to the repo):
  assets/music/*   any mp3/wav/m4a/ogg - or even an mp4 (audio is extracted). Name hints: sneaky_, sad_, tense_, ...
  assets/sfx/*     vine_boom.mp3, scratch.wav ... overrides the built-in synthesized sound of the same name
  assets/fonts/*   any .ttf you like for captions

Usage
  python channel.py                 full run (fills today's missing videos only)
  python channel.py --dry-run       everything except the upload
  python channel.py --only short    (or: --only long)
  python channel.py --selftest      offline render test (no APIs needed)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
HISTORY_FILE = ROOT / "history.json"
T_START = time.time()
FPS = 30
SR = 44100
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}


def env(name, default=""):
    v = os.environ.get(name)
    return (v if v not in (None, "") else default).strip()


def log(*a):
    print(*a, flush=True)


def elapsed_min():
    return (time.time() - T_START) / 60.0


# --------------------------------------------------------------------------
# Config (all overridable with env vars)
# --------------------------------------------------------------------------
SHORTS_PER_DAY = int(env("SHORTS_PER_DAY", "3"))
LONGS_PER_DAY = int(env("LONGS_PER_DAY", "1"))
LONG_MIN_S = float(env("LONG_MIN_SECONDS", "385"))       # never upload a "long" under ~6:25
LONG_TARGET_S = float(env("LONG_TARGET_SECONDS", "450"))  # aim for 7:30
LONG_MAX_S = float(env("LONG_MAX_SECONDS", "590"))
LONG_HEIGHT = int(env("LONG_HEIGHT", "1080"))            # 720 renders ~2x faster
PUBLISH_MODE = env("PUBLISH_MODE", "schedule")           # schedule | now | private
SHORT_SLOTS_UTC = [int(x) for x in env("SHORT_SLOTS_UTC", "15,19,23").split(",") if x.strip()]
LONG_SLOT_UTC = int(env("LONG_SLOT_UTC", "1"))
RUN_BUDGET_MIN = float(env("RUN_BUDGET_MIN", "105"))     # stop starting new work after this
CF_ACCOUNT, CF_TOKEN = env("CF_ACCOUNT_ID"), env("CF_API_TOKEN")
PIXABAY_KEY = env("PIXABAY_API_KEY")
OFFLINE = env("OFFLINE_TEST") == "1"
CHANNEL_NAME = env("CHANNEL_NAME")                       # optional tiny watermark, e.g. "@mychannel"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def run(cmd, cwd=None, inp=None, timeout=1800):
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, input=inp, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("command failed: " + " ".join(str(c) for c in cmd[:6]) + "\n"
                           + r.stderr.decode("utf8", "ignore")[-1500:])
    return r


def probe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    return float(r.stdout.decode().strip())


def rng_from(*parts):
    h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:12], 16))


def slug(s, n=40):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n] or "x"


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def ease_out(x):
    x = clamp(x, 0, 1)
    return 1 - (1 - x) ** 3


def ease_io(x):
    x = clamp(x, 0, 1)
    return x * x * (3 - 2 * x)


def ease_back(x, k=1.9):
    x = clamp(x, 0, 1) - 1
    return 1 + (k + 1) * x ** 3 + k * x ** 2


def pick(rnd, seq):
    return seq[rnd.randrange(len(seq))]


# --------------------------------------------------------------------------
# Memory: never repeat. Lexical fingerprints of every idea + rotation of formats/domains/assets.
# --------------------------------------------------------------------------
STOP = set("""the a an and or but of to in on at for with my your our their is are was were be been being it this that
these those i you he she we they me him her us them as by from up down out over under again then so just very really not
no yes do does did have has had will would can could should may might when while if than too also what who whom which why
how all any one got get like about into after before""".split())


def toks(s):
    out = set()
    for w in re.findall(r"[a-z0-9']{3,}", (s or "").lower()):
        if w in STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def sim(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class Memory:
    def __init__(self, path=HISTORY_FILE):
        self.path = Path(path)
        try:
            self.items = json.loads(self.path.read_text())
        except Exception:
            self.items = []
        if not isinstance(self.items, list):
            self.items = []

    def save(self):
        try:
            self.path.write_text(json.dumps(self.items[-3000:], ensure_ascii=False, indent=0))
        except Exception as e:
            log("history save failed:", e)

    def add(self, rec):
        self.items.append(rec)
        self.save()

    def real(self):
        return [i for i in self.items if not i.get("dry")]

    def titles(self, n=120):
        return [x.get("title", "") for x in self.real()[-n:] if x.get("title")]

    def recent(self, key, n=30):
        out = []
        for x in self.real()[-n:]:
            v = x.get(key)
            if v:
                out.extend(v if isinstance(v, list) else [v])
        return out

    def is_dup(self, text, thr=0.5, n=900):
        t = toks(text)
        if len(t) < 3:
            return False
        for x in self.real()[-n:]:
            if sim(t, set(x.get("fp", []))) >= thr:
                return True
        return False

    def today(self, kind):
        d = dt.date.today().isoformat()
        return sum(1 for x in self.real() if x.get("date") == d and x.get("kind") == kind)

    def used(self, key):
        s = set()
        for r in self.real():
            v = r.get(key)
            if v:
                s.update(v if isinstance(v, list) else [v])
        return s


# --------------------------------------------------------------------------
# Gemini: models are DISCOVERED from the API (names change; old ones get shut down),
# keys are rotated, 429s are parsed so a dead (model,key) is skipped instantly.
# --------------------------------------------------------------------------
class LLMError(Exception):
    pass


class Gone(Exception):
    """model unusable for this account (404 / permission / zero quota)"""


class Busy(Exception):
    """every key is cooling down for this model"""


class Bad(Exception):
    """400: the request itself is wrong"""


STATIC_TEXT_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                      "gemini-3-flash-preview", "gemini-flash-latest", "gemini-flash-lite-latest",
                      "gemini-2.5-flash", "gemini-2.5-flash-lite"]
STATIC_TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview",
                     "gemini-2.5-flash-preview-tts"]
_NOT_TEXT = re.compile(r"tts|image|embed|live|audio|robot|computer|aqa|imagen|veo|omni|transcribe|lyria|learnlm|gemma"
                       r"|vision|nano|banana|search|research|customtools", re.I)


def _ver(name):
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def rank_text_models(names):
    paid_ok = env("GEMINI_ALLOW_PAID") == "1"
    out = [n for n in names if n.startswith("gemini-") and not _NOT_TEXT.search(n) and (paid_ok or _ver(n) < 4)]

    def key(n):
        v = _ver(n) - (0.05 if re.search(r"preview|exp", n) else 0)
        group = 0 if ("flash" in n and "lite" not in n) else 1 if "lite" in n else 2
        if "latest" in n:
            v = -1
        return (group, -v, n)
    return sorted(set(out), key=key)


def rank_tts_models(names):
    out = [n for n in names if "tts" in n]
    return sorted(set(out), key=lambda n: (-_ver(n), "lite" in n, "preview" in n, n))


class Gemini:
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self):
        ks = []
        for name in ["GEMINI_API_KEY"] + [f"GEMINI_API_KEY_{i}" for i in range(2, 10)] + ["GOOGLE_API_KEY"]:
            v = env(name)
            if v and v not in ks:
                ks.append(v)
        self.keys = ks
        self.cool, self.dead = {}, set()
        self.dead_model, self.dead_key = set(), set()
        self.text_models, self.tts_models = None, None
        self.lock = threading.Lock()
        self.rr = 0

    def discover(self):
        if self.text_models is not None:
            return
        forced = [m.strip() for m in env("GEMINI_MODELS").split(",") if m.strip()]
        names = []
        for k in self.keys:
            try:
                tok, got = None, []
                for _ in range(10):
                    params = {"pageSize": 200}
                    if tok:
                        params["pageToken"] = tok
                    r = requests.get(f"{self.BASE}/models", headers={"x-goog-api-key": k}, params=params, timeout=30)
                    if r.status_code != 200:
                        break
                    j = r.json()
                    got += [m["name"].split("/", 1)[-1] for m in j.get("models", [])
                            if "generateContent" in (m.get("supportedGenerationMethods") or [])]
                    tok = j.get("nextPageToken")
                    if not tok:
                        break
                if got:
                    names = got
                    break
            except Exception as e:
                log("  model discovery error:", str(e)[:100])
        self.text_models = forced or rank_text_models(names) or list(STATIC_TEXT_MODELS)
        self.tts_models = ([m.strip() for m in env("GEMINI_TTS_MODELS").split(",") if m.strip()]
                           or rank_tts_models(names) or list(STATIC_TTS_MODELS))
        log(f"Gemini: {len(names)} models visible | text -> {self.text_models[:4]} | tts -> {self.tts_models[:2]}")

    def post(self, model, payload, timeout=180, max_wait=75):
        if not self.keys:
            raise LLMError("no GEMINI_API_KEY set")
        last = ""
        for attempt in range(7):
            now = time.time()
            cands = [i for i in range(len(self.keys)) if i not in self.dead_key and (model, i) not in self.dead]
            if model in self.dead_model or not cands:
                raise Gone(last or model)
            ready = [i for i in cands if self.cool.get((model, i), 0) <= now]
            if not ready:
                wait = min(self.cool.get((model, i), 0) for i in cands) - now
                if wait > max_wait:
                    raise Busy(f"{model} cooling {wait:.0f}s")
                time.sleep(max(0.5, wait))
                continue
            with self.lock:
                self.rr += 1
                ki = ready[self.rr % len(ready)]
            try:
                r = requests.post(f"{self.BASE}/models/{model}:generateContent",
                                  headers={"x-goog-api-key": self.keys[ki], "Content-Type": "application/json"},
                                  json=payload, timeout=timeout)
            except requests.RequestException as e:
                last = str(e)[:200]
                self.cool[(model, ki)] = time.time() + 5
                continue
            if r.status_code == 200:
                return r.json()
            body = r.text[:900]
            last = f"{r.status_code} {body[:220]}"
            if r.status_code == 429:
                m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', body)
                delay = float(m.group(1)) if m else 30.0
                if re.search(r"PerDay|per day|limit: 0", body, re.I):
                    self.dead.add((model, ki))
                    log(f"  (quota: {model} exhausted on key #{ki + 1} for today)")
                else:
                    self.cool[(model, ki)] = time.time() + clamp(delay, 3, 90)
                continue
            if r.status_code == 404:
                self.dead_model.add(model)
                raise Gone(last)
            if r.status_code in (401, 403):
                if r.status_code == 401 or "API key" in body or "API_KEY" in body:
                    self.dead_key.add(ki)
                else:
                    self.dead.add((model, ki))
                continue
            if r.status_code == 400:
                if "API key not valid" in body or "API_KEY_INVALID" in body:
                    self.dead_key.add(ki)
                    continue
                raise Bad(last)
            self.cool[(model, ki)] = time.time() + 6 * (attempt + 1)      # 5xx: back off
        raise Busy(last or "busy")


G = Gemini()


def _balance(s):
    """Best-effort repair of JSON cut off by the token limit."""
    stack, in_str, esc = [], False, False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                return s[:i + 1]
    t = s + ('"' if in_str else "")
    for pat in (r",\s*$", r',\s*"[^"]*"\s*:\s*$', r'"[^"]*"\s*:\s*$', r",\s*$"):
        t = re.sub(pat, "", t)
    return t + "".join(reversed(stack))


def parse_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"[\[{]", t)
    if not m:
        raise ValueError("no JSON in model output")
    s = t[m.start():]
    e = s.rfind("}" if s[0] == "{" else "]")
    if e > 0:
        for cand in (s[:e + 1], re.sub(r",\s*([}\]])", r"\1", s[:e + 1])):
            try:
                return json.loads(cand)
            except Exception:
                pass
    return json.loads(_balance(s))


def _text_of(data):
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = ((cands[0].get("content") or {}).get("parts")) or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


SAFETY = [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in (
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT")]


def llm(prompt, system=None, json_out=True, temperature=1.0, grounded=False, max_tokens=14000, tries=2):
    """One call. Walks the discovered models; never loops forever."""
    G.discover()
    gen = {"temperature": temperature, "maxOutputTokens": max_tokens, "topP": 0.95}
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen,
               "safetySettings": SAFETY}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if grounded:
        payload["tools"] = [{"google_search": {}}]
    elif json_out:
        gen["responseMimeType"] = "application/json"
    last = None
    for model in list(G.text_models):
        for t in range(tries):
            try:
                data = G.post(model, payload)
            except (Gone, Busy) as e:
                last = e
                break
            except Bad as e:
                last = e
                if "safetySettings" in payload:
                    payload.pop("safetySettings")
                    continue
                break
            text = _text_of(data)
            if not text.strip():
                last = LLMError("empty answer: " + str((data.get("candidates") or [{}])[0].get("finishReason")))
                continue
            if grounded or not json_out:
                return text
            try:
                return parse_json(text)
            except Exception as e:
                last = e
                payload["contents"][0]["parts"][0]["text"] = prompt + "\n\nReturn ONLY valid JSON. No commentary, no markdown."
    raise LLMError(f"all text models failed: {str(last)[:300]}")


# --------------------------------------------------------------------------
# Context: calendar (always works) + live trends (best effort) + web-grounded notes
# --------------------------------------------------------------------------
def _nth(y, m, wd, n):
    d = dt.date(y, m, 1)
    d += dt.timedelta(days=(wd - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return dt.date(y, (h + l - 7 * m + 114) // 31, ((h + l - 7 * m + 114) % 31) + 1)


def observances(today):
    y = today.year
    ev = [(dt.date(y, m, d), n) for m, d, n in [
        (1, 1, "New Year"), (2, 14, "Valentine's Day"), (3, 17, "St. Patrick's Day"), (4, 1, "April Fools"),
        (7, 4, "Fourth of July"), (10, 31, "Halloween"), (12, 25, "Christmas"), (12, 31, "New Year's Eve")]]
    thx = _nth(y, 11, 3, 4)
    ev += [(_nth(y, 5, 6, 2), "Mother's Day"), (_nth(y, 6, 6, 3), "Father's Day"), (_nth(y, 9, 0, 1), "Labor Day"),
           (thx, "Thanksgiving"), (thx + dt.timedelta(days=1), "Black Friday"), (thx + dt.timedelta(days=4), "Cyber Monday"),
           (_nth(y, 11, 6, 1), "daylight saving ends (clocks go back)"), (_nth(y, 3, 6, 2), "daylight saving starts"),
           (_easter(y), "Easter"), (_nth(y, 2, 6, 2), "Super Bowl weekend")]
    out = []
    for d, n in ev:
        k = (d - today).days
        if -3 <= k <= 14:
            out.append(f"{n} ({'today' if k == 0 else f'in {k} days' if k > 0 else f'{-k} days ago'})")
    md = (today.month, today.day)
    if (12, 3) <= md <= (12, 22) or (5, 1) <= md <= (5, 20) or (1, 5) <= md <= (1, 25):
        out.append("exam / finals season")
    if (8, 12) <= md <= (9, 20):
        out.append("back-to-school season")
    if (1, 1) <= md <= (1, 31):
        out.append("new-year resolutions")
    return out


def _get(url, **kw):
    kw.setdefault("headers", UA)
    kw.setdefault("timeout", 20)
    return requests.get(url, **kw)


def trend_brief():
    today = dt.date.today()
    bits = [f"Today: {today:%A %d %B %Y}. Observances nearby: " + (", ".join(observances(today)) or "none")]
    seeds = []
    try:
        r = _get("https://trends.google.com/trending/rss?geo=US")
        titles = [t for t in re.findall(r"<title>(.*?)</title>", r.text)[1:18] if t]
        if titles:
            bits.append("Google Trends US: " + "; ".join(titles))
            seeds += titles
    except Exception as e:
        log("  (google trends skipped:", str(e)[:80], ")")
    try:
        d = dt.datetime.utcnow().date() - dt.timedelta(days=2)
        r = _get(f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{d:%Y/%m/%d}")
        arts = [a["article"].replace("_", " ") for a in r.json()["items"][0]["articles"]
                if not re.match(r"(Main_Page|Special:|Wikipedia:|Portal:|File:|Category:|Help:|Talk:|-$)", a["article"])][:22]
        if arts:
            bits.append("Most-read Wikipedia pages (what people are curious about): " + "; ".join(arts))
    except Exception as e:
        log("  (wikipedia trends skipped:", str(e)[:80], ")")
    for sub in ("memes", "funny", "CasualConversation", "mildlyinfuriating", "relationship_advice"):
        try:
            r = _get(f"https://www.reddit.com/r/{sub}/top/.rss?t=day")
            ts = re.findall(r"<entry>.*?<title>(.*?)</title>", r.text, flags=re.S)[:6]
            if ts:
                bits.append(f"Reddit r/{sub} today: " + " | ".join(t.strip() for t in ts))
        except Exception:
            pass
    if not OFFLINE and G.keys:
        try:
            g = llm("In 8 short bullets: which comedy formats, meme templates, sounds and relatable everyday topics are "
                    "going viral on YouTube Shorts / TikTok / Instagram Reels for English-speaking viewers THIS week? "
                    "Be concrete and specific.", grounded=True, json_out=False, max_tokens=3000, tries=1)
            bits.append("Web-search trend notes:\n" + g[:2200])
        except Exception as e:
            log("  (grounded trend search skipped:", str(e)[:100], ")")
    brief = "\n".join(bits)[:5200]
    log("Trend brief:", len(brief), "chars")
    return brief


# --------------------------------------------------------------------------
# Audio file helpers
# --------------------------------------------------------------------------
def load_audio(path, channels=2):
    r = run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-f", "f32le", "-ac", str(channels), "-ar", str(SR), "-"])
    a = np.frombuffer(r.stdout, dtype=np.float32)
    return a.reshape(-1, channels) if channels > 1 else a


def decode_bytes(blob, channels=1):
    r = run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-vn", "-f", "f32le", "-ac", str(channels), "-ar", str(SR), "-"],
            inp=blob)
    a = np.frombuffer(r.stdout, dtype=np.float32)
    return a.reshape(-1, channels) if channels > 1 else a


def resample(x, a, b):
    if a == b or len(x) < 2:
        return x
    n = int(len(x) * b / a)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def pcm_from_blob(blob):
    """Gemini TTS answer: WAV (RIFF) or raw 24 kHz s16le -> mono float32 at SR."""
    if blob[:4] == b"RIFF":
        with wave.open(io.BytesIO(blob), "rb") as w:
            rate, ch = w.getframerate(), w.getnchannels()
            raw = w.readframes(w.getnframes())
        x = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        if ch > 1:
            x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
    else:
        rate = 24000
        x = np.frombuffer(blob[: len(blob) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    return resample(x, rate, SR)


def est_words(text, dur):
    words = text.split()
    wts = [len(w) + 2 + (4 if re.search(r"[.!?]$", w) else 2 if re.search(r"[,;:]$", w) else 0) for w in words]
    tot, t, out = sum(wts) or 1, 0.0, []
    for w, wt in zip(words, wts):
        d = dur * wt / tot
        out.append((w, t, t + d))
        t += d
    return out


# --------------------------------------------------------------------------
# Voice: Gemini TTS (best acting, tiny quota) -> Edge neural voices (free, word timings) -> Cloudflare
# --------------------------------------------------------------------------
GEMINI_VOICES = {"NARRATOR": "Puck", "A": "Fenrir", "B": "Kore", "C": "Charon"}
EDGE_VOICES = {"NARRATOR": ("en-US-AndrewMultilingualNeural", "+4%", "+0Hz"), "A": ("en-US-GuyNeural", "+6%", "-2Hz"),
               "B": ("en-US-AriaNeural", "+4%", "+0Hz"), "C": ("en-US-ChristopherNeural", "+0%", "-4Hz")}


class Voice:
    def __init__(self):
        self.g_used, self.g_budget = 0, int(env("TTS_GEMINI_MAX", "16"))
        self.g_off, self.edge_off = False, False

    def say(self, text, speaker="NARRATOR", style="deadpan", prefer="gemini"):
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return None
        if OFFLINE:
            res = self._finish(*self._stub(text), text)
            res["engine"] = "stub"
            return res
        order = ["gemini", "edge", "cf"] if prefer == "gemini" else ["edge", "gemini", "cf"]
        for eng in order:
            try:
                got = getattr(self, "_" + eng)(text, speaker, style)
            except Exception as ex:
                log(f"    tts {eng} error:", str(ex)[:140])
                got = None
            if got:
                res = self._finish(got[0], got[1], text)
                res["engine"] = eng
                return res
        return None

    @staticmethod
    def _finish(x, words, text):
        a = np.abs(x)
        idx = np.where(a > 10 ** (-46 / 20))[0]
        s, e = (max(0, idx[0] - int(0.03 * SR)), min(len(x), idx[-1] + int(0.06 * SR))) if len(idx) else (0, len(x))
        x = x[s:e]
        x = (x / (float(np.max(np.abs(x))) or 1.0) * 0.9).astype(np.float32)
        f = int(0.008 * SR)
        if len(x) > 4 * f:
            x[:f] *= np.linspace(0, 1, f)
            x[-f:] *= np.linspace(1, 0, f)
        dur = len(x) / SR
        if words:
            off = s / SR
            words = [(w, clamp(a_ - off, 0, dur), clamp(b_ - off, 0, dur)) for w, a_, b_ in words]
        else:
            words = est_words(text, dur)
        return {"wav": x, "dur": dur, "words": words}

    @staticmethod
    def _stub(text):
        dur = max(0.8, len(text.split()) * 0.33)
        t = np.arange(int(dur * SR)) / SR
        ph = 2 * np.pi * np.cumsum(150 + 30 * np.sin(2 * np.pi * 0.7 * t)) / SR
        x = (np.sign(np.sin(ph)) * 0.5 + np.sin(ph * 2) * 0.2) * np.sqrt(0.5 + 0.5 * np.sin(2 * np.pi * 4.2 * t)) * 0.35
        return x.astype(np.float32), None

    def _gemini(self, text, speaker, style):
        if self.g_off or self.g_used >= self.g_budget or not G.keys:
            return None
        G.discover()
        voice = GEMINI_VOICES.get(speaker, "Puck")
        st = f"{style}; natural American English, great comedic timing"
        new = {"contents": [{"role": "user", "parts": [{"text": text, "speech_metadata": {"style": st}}]}],
               "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"voice": voice}}}}
        old = {"contents": [{"role": "user", "parts": [{"text": f"Say this in a {st} way: {text}"}]}],
               "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                   "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
        for model in G.tts_models[:3]:
            for payload in (new, old):
                try:
                    data = G.post(model, payload, timeout=120, max_wait=20)
                except Gone:
                    break
                except Busy:
                    self.g_off = True
                    return None
                except Bad:
                    continue
                try:
                    part = data["candidates"][0]["content"]["parts"][0]
                    blob = base64.b64decode((part.get("inlineData") or part.get("inline_data"))["data"])
                    self.g_used += 1
                    return pcm_from_blob(blob), None
                except Exception:
                    continue
        return None

    def _edge(self, text, speaker, style):
        if self.edge_off:
            return None
        try:
            import edge_tts
        except ImportError:
            self.edge_off = True
            return None
        voice, rate, pitch = EDGE_VOICES.get(speaker, EDGE_VOICES["NARRATOR"])

        async def go():
            try:
                com = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary")
            except TypeError:
                com = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            audio, words = bytearray(), []
            async for ch in com.stream():
                if ch.get("type") == "audio":
                    audio.extend(ch["data"])
                elif ch.get("type") == "WordBoundary":
                    words.append((ch["text"], ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7))
            return bytes(audio), words
        for attempt in range(2):
            try:
                audio, words = asyncio.run(go())
                if audio:
                    x = decode_bytes(audio, 1)
                    return x, (words if len(words) >= 0.6 * len(text.split()) else None)
            except Exception as e:
                log("    edge tts retry:", str(e)[:100])
                time.sleep(1.5)
        self.edge_off = True
        return None

    def _cf(self, text, speaker, style):
        if not (CF_ACCOUNT and CF_TOKEN):
            return None
        r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/@cf/myshell-ai/melotts",
                          headers={"Authorization": f"Bearer {CF_TOKEN}"}, json={"prompt": text, "lang": "en"}, timeout=90)
        j = r.json()
        b64 = (j.get("result") or j).get("audio")
        return (decode_bytes(base64.b64decode(b64), 1), None) if b64 else None


VOICE = Voice()


# --------------------------------------------------------------------------
# Images: Cloudflare Flux -> Pollinations -> Pixabay illustration -> drawn card. Never fails, never stops.
# --------------------------------------------------------------------------
ART_STYLES = [
    ("chunky-cartoon", "flat 2D cartoon illustration, thick black outlines, bold saturated colors, exaggerated funny facial expressions, simple clean background"),
    ("claymation", "claymation stop-motion style, plasticine texture, soft studio lighting, exaggerated funny expression, simple background"),
    ("pop-comic", "pop-art comic book style, halftone dots, bold ink lines, vivid colors, dramatic funny expression"),
    ("paper-cut", "layered paper cutout collage, handmade craft texture, soft shadows, funny expressive faces"),
    ("doodle", "crude hand-drawn marker doodle, scribbly childlike lines, flat colors, very funny exaggerated faces, plain background, rage-comic energy"),
    ("toy-3d", "glossy plastic toy figure 3D render, soft studio light, shallow depth of field, comically expressive face"),
    ("retro-90s", "retro 90s hand-painted cartoon cel look, slightly grainy, bright colors, goofy expressive characters"),
    ("sticker", "flat vector sticker illustration with white outline, bold colors, cute funny exaggerated emotion"),
]
IMG_SUFFIX = ", no text, no letters, no words, no logos, no watermark, one clear subject centered, tall vertical composition"


def valid_image(path):
    try:
        im = Image.open(path)
        im.load()
        if im.width < 256 or im.height < 256:
            return False
        a = np.asarray(im.convert("L").resize((48, 48)), dtype=np.float32)
        return a.mean() > 8 and a.std() > 3
    except Exception:
        return False


def img_card(prompt, path, seed, size=1024):
    """Last-resort visual: gradient + a cartoon face whose expression follows the prompt."""
    rnd = random.Random(seed)
    c1 = tuple(rnd.randint(30, 190) for _ in range(3))
    c2 = tuple(rnd.randint(60, 230) for _ in range(3))
    t = np.linspace(0, 1, size)[:, None, None]
    arr = (np.array(c1)[None, None, :] * (1 - t) + np.array(c2)[None, None, :] * t) * np.ones((1, size, 1))
    im = Image.fromarray(arr.astype(np.uint8), "RGB")
    d = ImageDraw.Draw(im)
    for _ in range(7):
        x, y, r = rnd.randint(0, size), rnd.randint(0, size), rnd.randint(60, 220)
        d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(min(255, v + 25) for v in c2))
    cx, cy, R = size // 2, int(size * 0.52), int(size * 0.30)
    d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=(255, 205, 60), outline=(20, 20, 20), width=14)
    p = prompt.lower()
    eye = int(R * 0.16)
    big = bool(re.search(r"scream|shock|horror|panic|terrif|bulg|wide|gasp|stunned", p))
    for ex in (-0.38, 0.38):
        ex_, ey = cx + int(R * ex), cy - int(R * 0.22)
        rr = int(eye * (1.9 if big else 1.0))
        d.ellipse([ex_ - rr, ey - rr, ex_ + rr, ey + rr], fill="white", outline=(20, 20, 20), width=8)
        d.ellipse([ex_ - rr // 3, ey - rr // 3, ex_ + rr // 3, ey + rr // 3], fill=(20, 20, 20))
    my = cy + int(R * 0.42)
    if big:
        d.ellipse([cx - int(R * .26), my - int(R * .26), cx + int(R * .26), my + int(R * .30)], fill=(90, 20, 20), outline=(20, 20, 20), width=8)
    elif re.search(r"smug|smirk|confident|proud|sunglass|cool", p):
        d.arc([cx - int(R * .5), my - int(R * .25), cx + int(R * .5), my + int(R * .25)], 10, 150, fill=(20, 20, 20), width=14)
    else:
        d.line([cx - int(R * .35), my, cx + int(R * .35), my], fill=(20, 20, 20), width=14)
    im.save(path, quality=92)
    return True


class Imagery:
    def __init__(self, work):
        self.work = Path(work)
        self.work.mkdir(parents=True, exist_ok=True)
        self.cf_fails, self.cf_off, self.poll_off = 0, False, False
        self.stats = {"cf": 0, "poll": 0, "pixabay": 0, "card": 0}
        self.lock = threading.Lock()

    def _count(self, k):
        with self.lock:
            self.stats[k] += 1

    def _cf(self, prompt, seed, path):
        if not (CF_ACCOUNT and CF_TOKEN) or self.cf_off:
            return False
        try:
            r = requests.post(
                f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/@cf/black-forest-labs/flux-1-schnell",
                headers={"Authorization": f"Bearer {CF_TOKEN}"}, json={"prompt": prompt[:2000], "steps": 6, "seed": seed},
                timeout=120)
            if r.status_code == 429 or re.search(r"neurons|allocation", r.text[:400], re.I):
                log("    cloudflare image quota reached -> using fallbacks for the rest of this run")
                self.cf_off = True
                return False
            j = r.json()
            b64 = (j.get("result") or j).get("image")
            if b64:
                Path(path).write_bytes(base64.b64decode(b64))
                if valid_image(path):
                    self.cf_fails = 0
                    return True
        except Exception as e:
            log("    flux error:", str(e)[:100])
        self.cf_fails += 1
        if self.cf_fails >= 6:
            self.cf_off = True
        return False

    def _pollinations(self, prompt, seed, path):
        if self.poll_off:
            return False
        try:
            from urllib.parse import quote
            r = requests.get(f"https://image.pollinations.ai/prompt/{quote(prompt[:700])}",
                             params={"width": 1024, "height": 1024, "seed": seed, "nologo": "true", "model": "flux"},
                             headers=UA, timeout=80)
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                Path(path).write_bytes(r.content)
                return valid_image(path)
        except Exception:
            pass
        self.poll_off = True
        return False

    def _pixabay(self, prompt, path):
        if not PIXABAY_KEY:
            return False
        try:
            q = " ".join(re.sub(r"[^a-z ]", " ", prompt.lower()).split()[:4])
            r = requests.get("https://pixabay.com/api/", params={"key": PIXABAY_KEY, "q": q, "image_type": "illustration",
                                                                  "safesearch": "true", "per_page": 12}, timeout=30)
            hits = r.json().get("hits", [])
            if hits:
                Path(path).write_bytes(requests.get(random.choice(hits)["largeImageURL"], timeout=60).content)
                return valid_image(path)
        except Exception:
            pass
        return False

    def get(self, prompt, seed, name):
        path = self.work / f"{name}.jpg"
        if path.exists():
            return path
        if OFFLINE:
            img_card(prompt, path, seed)
            self._count("card")
            return path
        for k in range(2):
            if self._cf(prompt, seed + k * 977, path):
                self._count("cf")
                return path
        if self._pollinations(prompt, seed, path):
            self._count("poll")
            return path
        if self._pixabay(prompt, path):
            self._count("pixabay")
            return path
        img_card(prompt, path, seed)
        self._count("card")
        return path


def stock_clip(query, work, avoid, min_dur=2.5):
    """A real royalty-free clip from Pixabay (animals, crowds, traffic...). None if unavailable."""
    if OFFLINE or not PIXABAY_KEY:
        return None
    words = re.sub(r"[^a-z0-9 ]", " ", query.lower()).split()
    for q in dict.fromkeys(x for x in (" ".join(words[:4]), " ".join(words[:2]), words[0] if words else "") if x):
        try:
            r = requests.get("https://pixabay.com/api/videos/", params={"key": PIXABAY_KEY, "q": q, "per_page": 40,
                                                                          "safesearch": "true"}, timeout=30)
            hits = [h for h in r.json().get("hits", []) if str(h["id"]) not in avoid and h.get("duration", 0) >= min_dur]
        except Exception:
            continue
        if not hits:
            continue
        h = random.choice(hits[:15])
        v = h.get("videos", {})
        src = (v.get("medium") or {}).get("url") or (v.get("small") or {}).get("url")
        if not src:
            continue
        path = Path(work) / f"stock_{h['id']}.mp4"
        try:
            with requests.get(src, stream=True, timeout=90) as rr:
                rr.raise_for_status()
                size = 0
                with open(path, "wb") as f:
                    for chunk in rr.iter_content(1 << 16):
                        size += len(chunk)
                        f.write(chunk)
                        if size > 60e6:
                            raise RuntimeError("clip too big")
            return {"path": path, "id": str(h["id"]), "duration": float(h.get("duration", 5))}
        except Exception as e:
            log("    stock download failed:", str(e)[:80])
    return None


# --------------------------------------------------------------------------
# The creative matrix. Variety is structural: every slot gets a different
# (everyday area x joke engine x tone x format), rotated against the history.
# --------------------------------------------------------------------------
DOMAINS = [d.strip() for d in """exams and studying; group projects; the school cafeteria; substitute teachers; homework at midnight;
university lectures; online classes with the camera off; parents and phones; siblings; grandparents; family dinners; relatives at weddings;
group chats; read receipts; voice messages; typing and deleting a text; phone battery; the charger that is never there; wifi dropping;
passwords; software updates; too many browser tabs; autocorrect; social media scrolling; reels at 3 AM; online shopping; delivery drivers;
self-checkout machines; supermarket trips; coffee; energy drinks; diets that start on Monday; gym motivation; the first day at the gym;
cooking for yourself; food delivery apps; leftovers; sleep schedules; alarm clocks; snooze button; monday mornings; friday evenings;
the weekend plan; office meetings that could be emails; the work chat; a new boss; job interviews; deadlines; procrastination;
traffic jams; learning to drive; parking; public transport; the airport; long flights; hotel rooms; vacations; packing a suitcase;
weather forecasts; rain without an umbrella; hot summers; freezing winters; birthdays; gifts; surprise parties; new year resolutions;
dating apps; first dates; texting someone you like; exes; roommates; neighbors; moving apartments; laundry; cleaning; dishes;
haircuts; shopping for clothes; shoes; the dentist; the doctor; being sick; pets that act like people; cats; dogs; babies;
video games; losing online; streaming and 'what to watch'; spoilers; sports fans; karaoke; concerts; photos and selfies;
childhood memories; being an adult; money and prices; saving money; splitting the bill; borrowing a pen; lost keys; small talk;
awkward silence; waving at someone who was not waving at you; meeting someone you know in public; compliments; apologies;
being late; being early; elevators; the bathroom queue; free samples; customer service calls; waiting rooms; queues""".replace("\n", " ").split(";") if d.strip()]

MECHANICS = [
    "confident, then instantly humbled", "denial that escalates in three steps", "a calm voice describing a total disaster",
    "huge overreaction to a tiny annoyance", "the plan vs. what actually happened", "expectation vs. reality",
    "the lie everyone tells ('5 more minutes', 'almost there')", "me vs. also me (two sides of one person)",
    "an oddly specific number or detail that makes it painfully real", "bait and switch: wholesome setup, petty payoff",
    "tiny person, giant confidence", "fake professionalism applied to something dumb", "reverse psychology that backfires",
    "everyone is fine except one person who is not", "the time-jump ('3 hours later')",
    "treating a small choice like a life-or-death decision", "a rule that makes no sense but everyone follows it",
    "a text conversation that goes wrong", "a pet or baby with a hidden agenda", "being confidently, completely wrong",
    "painfully literal interpretation", "an excuse that gets more elaborate every time", "petty revenge that works too well",
    "the universe punishing one small moment of happiness", "an understatement so big it loops back around",
    "two characters with totally different ideas of 'normal'",
]
TONES = ["deadpan", "unhinged", "wholesome", "sarcastic", "dramatic", "absurd", "petty", "overconfident", "exhausted", "chaotic"]
SHORT_FORMATS = {"reaction": 4, "sequence": 3, "nobody": 2, "vs": 2, "chat": 3, "dialogue": 2}
SILENT_FORMATS = ["reaction", "sequence", "nobody", "vs", "chat"]          # used inside the long compilation
HITS = ["boom", "fail", "ding", "airhorn", "sting", "boing", "crickets", "glass"]
HIT_WEIGHTS = [30, 20, 10, 10, 10, 10, 5, 5]
MOODS = ["bouncy", "sneaky", "chill", "tense", "sad", "epic", "chaos"]
ARCS = [("bouncy", "tense"), ("chill", "sad"), ("sneaky", "chaos"), ("bouncy", "chaos"), ("chill", "epic"),
        ("sneaky", "tense"), ("epic", "sad"), ("bouncy", "sad")]

# One worked example per format, shown to the writer so the JSON shape and the quality bar are crystal clear.
EXAMPLES = {
    "reaction": {
        "yt_title": "Me at 11 PM vs me at 7:58 AM", "hook": "project due at 8:00",
        "setup": "Me at 11 PM: I'll start the project in the morning. I'm very disciplined.",
        "setup_visual": "smug man in pajamas leaning back in a gaming chair, hands behind his head, glowing monitor, cozy dark room",
        "turn": "Me at 7:58 AM, project due at 8:00:",
        "payoff_visual": "the same smug man in pajamas now screaming silently with huge bulging eyes, hair standing up, papers flying, clutching a laptop",
        "payoff": "it's called working under pressure", "music": "bouncy>tense", "hit": "boom"},
    "sequence": {
        "yt_title": "Phone battery denial, ranked", "hook": "1% and still scrolling", "title": "Phone battery levels of denial",
        "panels": [{"text": "20%: I'm fine", "visual": "calm young woman scrolling on a couch with a mug of tea, cozy living room"},
                   {"text": "5%: the charger is in the other room. I can make it last", "visual": "same young woman staring at a charger across the room like it is Mount Everest, dramatic clouds"},
                   {"text": "1%: just one more reel", "visual": "same young woman in a thunderstorm clutching her phone like a sacred artifact, wind blowing her hair"}],
        "payoff": "phone dies. everyone assumes I got kidnapped",
        "payoff_visual": "pitch black frame with the tiny silhouette of a woman silently screaming", "music": "bouncy>sad", "hit": "fail"},
    "nobody": {
        "yt_title": "Nobody: ... My brain at 3 AM", "hook": "the 2016 incident", "lines": ["Nobody:", "Absolutely nobody:"],
        "visual": "bored tired man staring at a dark bedroom ceiling, peaceful night, moonlight through blinds",
        "turn": "My brain at 3 AM:",
        "payoff_visual": "epic movie-trailer close-up of the same man's wide-open eyes lit by his phone, intense dramatic lighting",
        "payoff": "remember when you waved back at someone who wasn't waving at you? in 2016?", "hit": "boom"},
    "vs": {
        "yt_title": "What I ordered vs what arrived", "hook": "it matches the description",
        "left_label": "What I ordered", "left_visual": "magazine-style tower of huge fluffy pancakes dripping with glowing syrup and berries",
        "right_label": "What arrived", "right_visual": "one sad flat pancake on a paper plate with a single raisin on it, harsh lighting",
        "button": "customer service: 'it matches the description'", "hit": "fail"},
    "chat": {
        "yt_title": "Mom's text interrogation", "hook": "send a photo of the food", "contact": "Mom", "theme": "light", "group": False,
        "msgs": [{"from": "them", "text": "Did you eat?"}, {"from": "me", "text": "yes"}, {"from": "them", "text": "What did you eat"},
                 {"from": "me", "text": "food"}, {"from": "them", "text": "Send a photo of the food"}, {"from": "me", "text": "it's gone"},
                 {"from": "them", "text": "The food or the photo?"}, {"from": "me", "text": "both"}, {"from": "them", "text": "I'm calling you"}],
        "hit": "notif"},
    "dialogue": {
        "yt_title": "The cat has a lawyer", "hook": "allegedly",
        "cast": {"A": "angry mom in a pink bathrobe holding a broken vase", "B": "smug fluffy orange cat sitting innocently in broken vase pieces"},
        "lines": [{"who": "A", "text": "Who broke the vase?", "visual": "angry mom in a pink bathrobe holding a broken vase, furious", "style": "furious"},
                  {"who": "B", "text": "Not me.", "visual": "smug fluffy orange cat sitting innocently in broken vase pieces", "style": "calm, innocent"},
                  {"who": "A", "text": "You're standing in the pieces.", "visual": "angry mom in a pink bathrobe pointing at the floor, vein on forehead", "style": "irritated"},
                  {"who": "B", "text": "I'm a witness.", "visual": "smug fluffy orange cat with tiny reading glasses on, calm, courtroom vibe", "style": "deadpan"},
                  {"who": "A", "text": "You're the cat!", "visual": "angry mom in a pink bathrobe throwing her hands up in the air", "style": "exasperated"},
                  {"who": "B", "text": "Allegedly.", "visual": "close-up of a smug fluffy orange cat with half-closed eyes, tiny smirk", "style": "very calm, slow"}],
        "hit": "ding"},
}

# Emergency bank: hand-written, used only when the AI writers are unreachable, so the channel never misses a day.
BANK = [
    {"fmt": "reaction", "yt_title": "Me after cleaning my room for 3 hours", "hook": "and this?",
     "setup": "Me after cleaning my room for 3 hours", "setup_visual": "proud young man with hands on hips in a spotless bright bedroom, sparkles everywhere, big grin",
     "turn": "My mom walking in:", "payoff_visual": "stern mother with a raised eyebrow pointing at one tiny sock on the floor, dramatic spotlight",
     "payoff": "and this?", "hit": "boom", "music": "bouncy>tense"},
    {"fmt": "reaction", "yt_title": "I'll just check one notification", "hook": "a man restoring a rusty knife",
     "setup": "Me: I'll just check one notification", "setup_visual": "relaxed man on a couch holding a phone with a small smile, warm afternoon light",
     "turn": "3 hours later:", "payoff_visual": "the same man with hollow glassy eyes lit only by his phone, empty snack bags around him, dark room",
     "payoff": "why am I watching a man restore a rusty knife", "hit": "fail", "music": "chill>sad"},
    {"fmt": "reaction", "yt_title": "Ordering food in a language I don't speak", "hook": "it's looking at me",
     "setup": "Me confidently ordering food in a language I don't speak", "setup_visual": "cocky tourist in a flowery shirt pointing at a menu, waiter smiling politely",
     "turn": "The waiter returns:", "payoff_visual": "waiter placing a whole fish with huge round eyes in front of a terrified tourist",
     "payoff": "it's looking at me", "hit": "sting", "music": "bouncy>tense"},
    {"fmt": "reaction", "yt_title": "Me: I won't snack tonight", "hook": "the fridge light",
     "setup": "Me at 9 PM: I'm not eating any snacks tonight", "setup_visual": "determined woman in sportswear with arms crossed, glowing aura of willpower, clean kitchen",
     "turn": "Me at 12:03 AM, fridge light on:", "payoff_visual": "same woman as a raccoon-like goblin crouching in a glowing open fridge, cheeks stuffed, eyes shining",
     "payoff": "this is a different me. she doesn't know you", "hit": "airhorn", "music": "chill>chaos"},
    {"fmt": "sequence", "yt_title": "Cooking a proper dinner for myself", "hook": "deconstructed cereal", "title": "Cooking a real dinner tonight",
     "panels": [{"text": "6:00 PM - I'm going to make a real meal", "visual": "man in an apron holding a wooden spoon like a sword, heroic pose, bright kitchen"},
                {"text": "6:40 PM - the recipe needs 14 ingredients. I own 3", "visual": "same man in an apron staring into an open fridge with one lonely lemon, dramatic lighting"},
                {"text": "7:30 PM - it's cereal. I'm calling it 'deconstructed'", "visual": "same man in an apron eating cereal from a giant mixing bowl, tired eyes, chef hat tilted"}],
     "payoff": "five stars, no notes", "payoff_visual": "same man in an apron with a tiny golden star floating over his head, smug pose", "hit": "ding", "music": "bouncy>chill"},
    {"fmt": "sequence", "yt_title": "Sleep math at 2 AM", "hook": "a nap with ambition", "title": "Sleep math",
     "panels": [{"text": "11 PM: sleep now = 8 hours. perfect", "visual": "happy woman in bed with a moon-shaped pillow, sweet dreams, soft blue light"},
                {"text": "1 AM: 6 hours is basically 8", "visual": "same woman in bed scrolling her phone with one eye open, glowing screen"},
                {"text": "3 AM: 4 hours is a nap with ambition", "visual": "same woman sitting up in bed with messy hair and a thousand-yard stare, clock showing 3"},
                {"text": "5 AM: why sleep. just start the day early", "visual": "same woman wearing sunglasses at the window at dawn, zombie energy"}],
     "payoff": "Me at 10 AM: I'm built different (I'm dead)", "payoff_visual": "same woman face-down on a desk with a keyboard imprint on her cheek", "hit": "fail", "music": "chill>sad"},
    {"fmt": "nobody", "yt_title": "Nobody: ... my neighbor on a Saturday", "hook": "drilling the planet", "lines": ["Nobody:", "Absolutely nobody:"],
     "visual": "peaceful sleeping man in bed with a smile, sun rising, birds outside", "turn": "My neighbor at 7 AM on a Saturday:",
     "payoff_visual": "huge cartoon neighbor with a tool belt holding a giant drill triumphantly, dust cloud, a wall cracking",
     "payoff": "drilling through what I can only assume is the planet", "hit": "airhorn"},
    {"fmt": "nobody", "yt_title": "Rehearsing arguments in the shower", "hook": "nobody asked", "lines": ["Nobody:", "Not a single person:"],
     "visual": "calm man standing in a steamy shower, peaceful, eyes closed", "turn": "Me, rehearsing an argument in the shower:",
     "payoff_visual": "same man in a shower pointing dramatically at the shower head like a lawyer in court, intense eyes",
     "payoff": "and THAT is why I'm right. (I'm alone)", "hit": "ding"},
    {"fmt": "vs", "yt_title": "The Pinterest cake vs my cake", "hook": "it tastes fine", "left_label": "The recipe photo",
     "left_visual": "towering rainbow layer cake with perfect frosting and sprinkles, glowing in studio light",
     "right_label": "My cake", "right_visual": "collapsed lopsided brown blob with a candle sinking into it, sad lighting",
     "button": "it tastes fine. (it does not)", "hit": "fail"},
    {"fmt": "vs", "yt_title": "My weekend plan vs my weekend", "hook": "I called grandma in my head", "left_label": "The plan",
     "left_visual": "energetic woman juggling a guitar, laundry basket, dumbbells and a phone, bright motivational poster style",
     "right_label": "The weekend", "right_visual": "same woman melted on a couch under a blanket with snacks, phone at 3 percent, sleepy eyes",
     "button": "I did call grandma. in my head", "hit": "boing"},
    {"fmt": "chat", "yt_title": "Mom's text interrogation", "hook": "send a photo of the food", "contact": "Mom", "theme": "light", "group": False,
     "msgs": [{"from": "them", "text": "Did you eat?"}, {"from": "me", "text": "yes"}, {"from": "them", "text": "What did you eat"},
              {"from": "me", "text": "food"}, {"from": "them", "text": "Send a photo of the food"}, {"from": "me", "text": "it's gone"},
              {"from": "them", "text": "The food or the photo?"}, {"from": "me", "text": "both"}, {"from": "them", "text": "I'm calling you"}], "hit": "notif"},
    {"fmt": "chat", "yt_title": "Where are we eating?", "hook": "nobody wants anything", "contact": "Squad", "theme": "dark", "group": True,
     "msgs": [{"from": "Dave", "text": "where are we eating"}, {"from": "Sara", "text": "idk"}, {"from": "Mike", "text": "idk"},
              {"from": "Dave", "text": "I'm fine with anything"}, {"from": "Sara", "text": "same"}, {"from": "Mike", "text": "ok how about pizza"},
              {"from": "Dave", "text": "not pizza"}, {"from": "Sara", "text": "not pizza"}, {"from": "Mike", "text": "WHAT DO YOU WANT"},
              {"from": "Dave", "text": "idk"}], "hit": "notif"},
    {"fmt": "dialogue", "yt_title": "The cat has a lawyer", "hook": "allegedly", "cast": EXAMPLES["dialogue"]["cast"],
     "lines": EXAMPLES["dialogue"]["lines"], "hit": "ding"},
    {"fmt": "dialogue", "yt_title": "Job interview: biggest weakness", "hook": "honesty",
     "cast": {"A": "tired interviewer in a gray suit behind a desk, holding a resume", "B": "overconfident candidate in a too-big blazer, huge fake smile"},
     "lines": [{"who": "A", "text": "What's your biggest weakness?", "visual": "tired interviewer in a gray suit behind a desk, holding a resume, bored", "style": "bored"},
               {"who": "B", "text": "Honesty.", "visual": "overconfident candidate in a too-big blazer with a huge fake smile, thumbs up", "style": "proud, confident"},
               {"who": "A", "text": "I don't think that's a weakness.", "visual": "tired interviewer in a gray suit raising one eyebrow, skeptical", "style": "flat"},
               {"who": "B", "text": "I don't really care what you think.", "visual": "overconfident candidate in a too-big blazer leaning back with arms crossed, smirk", "style": "casual, cheerful"}],
     "hit": "boom"},
]


# --------------------------------------------------------------------------
# Writers' room: pitch many -> a panel ranks -> the best one ships. No scores, no gates, no rewrite loops.
# --------------------------------------------------------------------------
HUMOR = """You are the head writer of a viral meme-video channel (think of the funniest meme pages on Instagram, TikTok and YouTube Shorts, 5 million followers). You are a real, extremely online person in your mid-20s - never an AI, never a brand.

HOW YOU WRITE
1. Start from a REAL, specific moment people recognise from their own week, then bend it. The laugh comes from an exact detail, a number, or a reaction that is far too big (or far too calm) for the situation.
2. The payoff must be something the viewer could NOT have predicted from the setup. If they can finish your sentence, throw it away and find a stranger, truer one.
3. Text-message voice: contractions, plain words, lowercase is fine. No "quirky" adjectives, no exclamation spam, no explaining the joke, no moral.
4. Brutal economy: setup <= 14 words, turn <= 9 words, payoff <= 10 words. Every word has to earn its place.
5. Universal: a 16-year-old in Texas, a 30-year-old in Cairo and a 45-year-old in London must all get it. No celebrities or real people, no politics, no religion, no sex, no tragedy, nothing cruel to a group, no brand names inside the joke.
6. Pictures are cartoon / illustration frames (never photorealistic people). One frame, one clear subject, a HUGE exaggerated emotion. When a character appears in several pictures of one video, describe it with the SAME words every time (for example "a stressed bald man in a green hoodie").
7. You may use at most ONE picture per video written as "STOCK: <2-4 words>" (e.g. "STOCK: cat staring") when a real animal or real place is funnier than a drawing. Only things a stock-footage library really has: cats, dogs, monkeys, birds, crowds, traffic, rain, fireworks, kids playing, city timelapse.
8. Never write: plot twist, spoiler alert, buckle up, game changer, let that sink in, we've all been there, here's the thing, that moment when, when you realize, adulting, literally me, no cap, fr fr, bestie, slay, bruh, relatable."""

BANNED = ["plot twist", "spoiler alert", "buckle up", "game changer", "let that sink in", "we've all been there",
          "here's the thing", "that moment when", "when you realize", "adulting", "literally me", "no cap", "fr fr",
          "bestie", "like and subscribe"]
UNSAFE = re.compile(r"\b(nazi|hitler|suicid\w*|kill myself|rape\w*|porn\w*|nude|naked|sex\w*|terroris\w*|bomb\w*|isis|trump|biden|"
                    r"putin|allah|jesus|christ|church|mosque|israel|gaza|ukrain\w*|n[i1]gg\w*|fag\w*|retard\w*)\b", re.I)

NOTES = {
    "reaction": "Silent ~10 second meme. A setup caption over a picture, then a 'turn' caption, then a payoff picture with a tiny payoff caption. The payoff PICTURE does half the work: make it a huge, specific visual reaction. 'music' is 'mood>mood' from: bouncy, sneaky, chill, tense, sad, epic, chaos. 'hit' is one of: boom, fail, ding, airhorn, sting, boing, crickets, glass.",
    "sequence": "3-5 panels that escalate; each picture gets crazier than the last; the payoff is the funniest outcome. Same 'music' and 'hit' rules as reaction.",
    "nobody": "'Nobody:' lines, then the 'turn', then the payoff. The payoff must be oddly specific and unexpected, never generic. Same 'hit' rule.",
    "vs": "Two pictures with labels (expectation/reality style but be inventive with the labels). The optional 'button' line is the killer. Same 'hit' rule.",
    "chat": "A fake text conversation of 5-10 messages, each <= 14 words, natural back and forth; the LAST message is the punchline. 'from' is 'me' or 'them' (or real first names with group=true). theme is light or dark. 'hit' stays 'notif' unless the last message needs a sting (then boom/fail/ding).",
    "dialogue": "3-7 spoken lines between A and B (cartoon characters with a vivid look in 'cast'), each <= 14 words, last line is the button. 'style' is a 2-5 word acting note. EVERY line has its own 'visual' of the speaker. Same 'hit' rule.",
}


def T(s, maxw, minw=1):
    s = re.sub(r"\s+", " ", str(s or "")).strip().strip('"“”')
    n = len(s.split())
    return s if minw <= n <= maxw + 4 else None


def V(s):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if s.upper().startswith("STOCK:"):
        w = s[6:].split()
        return "STOCK: " + " ".join(w[:6]) if 1 <= len(w) <= 8 else None
    s = re.sub(r"[\"“”][^\"“”]{1,40}[\"“”]", "", s)        # images cannot hold text
    return s[:340] if 3 <= len(s.split()) <= 70 else None


def parse_music(s, hit, rnd):
    m = re.findall(r"[a-z]+", str(s or "").lower())
    if len(m) >= 2 and m[0] in MOODS and m[1] in MOODS:
        return (m[0], m[1])
    if hit == "fail":
        return pick(rnd, [("bouncy", "sad"), ("chill", "sad"), ("epic", "sad")])
    if hit in ("boom", "sting", "glass", "airhorn"):
        return pick(rnd, [("bouncy", "tense"), ("sneaky", "tense"), ("chill", "tense"), ("sneaky", "chaos"), ("bouncy", "chaos")])
    return pick(rnd, ARCS)


def _common(o, fmt, rnd):
    title = re.sub(r"\s+", " ", str(o.get("yt_title") or o.get("title") or "")).strip().strip('"')[:80]
    hook = re.sub(r"\s+", " ", str(o.get("hook") or "")).strip()[:80]
    hit = o.get("hit") if o.get("hit") in HITS + ["notif"] else None
    if hit == "notif" and fmt != "chat":
        hit = None
    return {"fmt": fmt, "yt_title": title, "hook": hook, "hit": hit or rnd.choices(HITS, HIT_WEIGHTS)[0]}


def v_reaction(o, rnd):
    c = _common(o, "reaction", rnd)
    c.update(setup=T(o.get("setup"), 16, 3), setup_visual=V(o.get("setup_visual")), turn=T(o.get("turn"), 11),
             turn_visual=V(o.get("turn_visual")) if o.get("turn_visual") else None, payoff_visual=V(o.get("payoff_visual")),
             payoff=T(o.get("payoff"), 12) if o.get("payoff") else "")
    if not (c["setup"] and c["setup_visual"] and c["turn"] and c["payoff_visual"]):
        return None
    c["music"] = parse_music(o.get("music"), c["hit"], rnd)
    c["texts"] = [c["setup"], c["turn"], c["payoff"]]
    return c


def v_sequence(o, rnd):
    c = _common(o, "sequence", rnd)
    ps = []
    for p in (o.get("panels") or [])[:5]:
        if isinstance(p, dict) and T(p.get("text"), 18) and V(p.get("visual")):
            ps.append({"text": T(p.get("text"), 18), "visual": V(p.get("visual"))})
    if len(ps) < 3:
        return None
    c.update(title=T(o.get("title"), 10) or c["yt_title"], panels=ps, payoff=T(o.get("payoff"), 16) if o.get("payoff") else "",
             payoff_visual=V(o.get("payoff_visual")))
    c["music"] = parse_music(o.get("music"), c["hit"], rnd)
    c["texts"] = [c["title"]] + [p["text"] for p in ps] + [c["payoff"]]
    return c


def v_nobody(o, rnd):
    c = _common(o, "nobody", rnd)
    lines = [T(x, 7) for x in (o.get("lines") or [])[:3]]
    c.update(lines=[x for x in lines if x] or ["Nobody:"], visual=V(o.get("visual")), turn=T(o.get("turn"), 12),
             payoff_visual=V(o.get("payoff_visual")), payoff=T(o.get("payoff"), 26, 2))
    if not (c["visual"] and c["turn"] and c["payoff_visual"] and c["payoff"]):
        return None
    c["music"] = parse_music(o.get("music"), c["hit"], rnd)
    c["texts"] = c["lines"] + [c["turn"], c["payoff"]]
    return c


def v_vs(o, rnd):
    c = _common(o, "vs", rnd)
    c.update(left_label=T(o.get("left_label"), 6), left_visual=V(o.get("left_visual")), right_label=T(o.get("right_label"), 6),
             right_visual=V(o.get("right_visual")), button=T(o.get("button"), 18) if o.get("button") else "")
    if not (c["left_label"] and c["left_visual"] and c["right_label"] and c["right_visual"]):
        return None
    c["music"] = parse_music(o.get("music"), c["hit"], rnd)
    c["texts"] = [c["left_label"], c["right_label"], c["button"]]
    return c


def v_chat(o, rnd):
    c = _common(o, "chat", rnd)
    group = bool(o.get("group"))
    msgs = []
    for m in (o.get("msgs") or [])[:12]:
        if isinstance(m, dict) and T(m.get("text"), 18) and str(m.get("from", "")).strip():
            f = str(m["from"]).strip()[:14]
            f = "me" if f.lower() == "me" else (f if group else "them")
            msgs.append({"from": f, "text": T(m["text"], 18)})
    if len(msgs) < 4 or len({m["from"] for m in msgs}) < 2:
        return None
    c.update(msgs=msgs, group=group, contact=T(o.get("contact"), 4) or "Chat", theme="dark" if o.get("theme") == "dark" else "light")
    c["texts"] = [m["text"] for m in msgs]
    c["music"] = ("chill", "chill")
    return c


def v_dialogue(o, rnd):
    c = _common(o, "dialogue", rnd)
    cast = {k: str(v)[:220] for k, v in (o.get("cast") or {}).items() if k in ("A", "B", "C") and isinstance(v, str)}
    lines = []
    for ln in (o.get("lines") or [])[:8]:
        if not isinstance(ln, dict):
            continue
        who = str(ln.get("who", "A")).upper()
        who = who if who in ("A", "B", "C", "NARRATOR") else "A"
        vis = V(ln.get("visual")) or cast.get(who)
        if T(ln.get("text"), 18) and vis:
            lines.append({"who": who, "text": T(ln["text"], 18), "visual": vis, "style": str(ln.get("style") or "natural")[:50]})
    if len(lines) < 3 or "A" not in cast:
        return None
    c.update(cast=cast, lines=lines, music=pick(rnd, [("chill", "chill"), ("bouncy", "bouncy"), ("sneaky", "sneaky")]))
    c["texts"] = [x["text"] for x in lines]
    return c


VALIDATORS = {"reaction": v_reaction, "sequence": v_sequence, "nobody": v_nobody, "vs": v_vs, "chat": v_chat, "dialogue": v_dialogue}


def lint(c):
    blob = " ".join(t for t in c.get("texts", []) if t).lower()
    return not (any(b in blob for b in BANNED) or UNSAFE.search(blob))


def fp_text(c):
    return " ".join([c.get("yt_title", "")] + [t for t in c.get("texts", []) if t])


def summarize(c):
    f = c["fmt"]
    if f == "reaction":
        return f'"{c["setup"]}" -> "{c["turn"]}" -> [{c["payoff"] or c["payoff_visual"][:60]}]'
    if f == "sequence":
        return f'{c["title"]}: ' + " / ".join(p["text"] for p in c["panels"]) + f' => {c["payoff"]}'
    if f == "nobody":
        return " ".join(c["lines"]) + f' {c["turn"]} => {c["payoff"]}'
    if f == "vs":
        return f'{c["left_label"]} [{c["left_visual"][:50]}] vs {c["right_label"]} [{c["right_visual"][:50]}] => {c["button"]}'
    if f == "chat":
        return " | ".join(f'{m["from"]}: {m["text"]}' for m in c["msgs"])
    return " | ".join(f'{x["who"]}: {x["text"]}' for x in c["lines"])


def make_slots(n, formats, mem, rnd, domains=None, weights=None):
    """Structural variety: every slot gets a different (format, area, joke engine, tone), rotated against history."""
    recent_f, recent_d, recent_m = mem.recent("fmt", 12), set(mem.recent("domain", 45)), set(mem.recent("mechanic", 14))
    pool = domains or DOMAINS
    slots, used_f, used_d = [], [], set()
    for i in range(n):
        w = [SHORT_FORMATS.get(f, 2) / (1 + recent_f.count(f) + 1.5 * used_f.count(f)) * (weights or {}).get(f, 1.0) for f in formats]
        f = rnd.choices(formats, w)[0]
        doms = [d for d in pool if d not in recent_d and d not in used_d] or pool
        mech = [m for m in MECHANICS if m not in recent_m] or MECHANICS
        s = {"slot": i, "fmt": f, "domain": rnd.choice(doms), "mechanic": rnd.choice(mech), "tone": rnd.choice(TONES)}
        slots.append(s)
        used_f.append(f)
        used_d.add(s["domain"])
        recent_m.add(s["mechanic"])
    return slots


def pitch_prompt(slots, brief, avoid, per_slot):
    fmts = sorted({s["fmt"] for s in slots})
    shapes = "\n".join(f"{f}: {NOTES[f]}\n  example: {json.dumps(EXAMPLES[f], ensure_ascii=False)}" for f in fmts)
    lines = "\n".join(f'SLOT {s["slot"]}: format={s["fmt"]} | everyday area: {s["domain"]} | joke engine: {s["mechanic"]} | tone: {s["tone"]}'
                      for s in slots)
    return f"""TREND BRIEF (inspiration only - evergreen, deeply relatable moments are the core; at most 1 in 4 ideas may touch something timely):
{brief[:2400]}

ALREADY MADE - never repeat the joke, premise or punchline of any of these: {avoid}

For EACH slot below write {per_slot} DIFFERENT candidate videos (different jokes, not rewordings). Follow the JSON shape of the slot's format exactly (the examples show the keys and the quality bar - never reuse their topics or wording).
{lines}

FORMATS
{shapes}

Every candidate also has: "yt_title" (<= 60 chars, curiosity or relatable, no ALL CAPS, no clickbait lies), "hook" (<= 8 words, the funniest phrase of the video).
Return JSON: a flat list of objects. Each object has "slot": <slot number> plus the keys of its format."""


def rank_candidates(cands):
    multi = {s: v for s, v in cands.items() if len(v) > 1}
    if not multi:
        return {}
    blocks = "\n".join(f"SLOT {s}:\n" + "\n".join(f"  [{s}.{k}] {summarize(c)}" for k, c in enumerate(v)) for s, v in multi.items())
    prompt = f"""You are a panel of five very online viewers scrolling Shorts at speed: a 17-year-old gamer, a 24-year-old nurse on a break, a 31-year-old dad, a 40-year-old teacher and a 19-year-old college student. You have seen every meme. You only laugh at what is genuinely surprising, specific and true to life; generic 'relatable' filler gets a scroll.
For each slot pick the ONE candidate that would get the most real laughs and shares. Penalise predictable punchlines, jokes you have seen a hundred times, wordy or explain-y text, vague wording, forced slang.
{blocks}
Return JSON: {{"best": {{"<slot>": "<id such as 3.1>"}}}}"""
    out = {}
    try:
        j = llm(prompt, temperature=0.3, max_tokens=1500)
        for s, cid in ((j.get("best") if isinstance(j, dict) else None) or {}).items():
            m = re.match(r"(\d+)\.(\d+)", str(cid))
            if m and int(m.group(1)) == int(s) and int(m.group(2)) < len(multi.get(int(s), [])):
                out[int(s)] = int(m.group(2))
    except Exception as e:
        log("  (ranking skipped:", str(e)[:100], ")")
    return out


def bank_pick(fmt, mem, rnd, used):
    pool = [b for b in BANK if b["fmt"] == fmt]
    fresh = [b for b in pool if b["yt_title"] not in used and not mem.is_dup(" ".join([b["yt_title"], b.get("hook", "")]), 0.7)]
    for b in rnd.sample(fresh or pool, len(fresh or pool)):
        c = VALIDATORS[fmt](dict(b), rnd)
        if c:
            used.add(b["yt_title"])
            return c
    return None


def write_slots(slots, brief, mem, rnd, per_slot=2, bank_used=None):
    """One content dict per slot, in order. Always returns something for every slot."""
    bank_used = bank_used if bank_used is not None else set()
    avoid = "; ".join(mem.titles(110)) or "none yet"
    by_slot = {s["slot"]: s for s in slots}
    cands = {s["slot"]: [] for s in slots}
    for attempt in range(2):
        need = [s for s in slots if not cands[s["slot"]]]
        if not need:
            break
        try:
            raw = llm(pitch_prompt(need, brief, avoid, per_slot), system=HUMOR, temperature=1.05, max_tokens=16000)
        except Exception as e:
            log("  writers' room unreachable:", str(e)[:160])
            break
        if isinstance(raw, dict):
            raw = next((v for v in raw.values() if isinstance(v, list)), [raw])
        for o in raw if isinstance(raw, list) else []:
            try:
                spec = by_slot.get(int(o.get("slot")))
            except Exception:
                continue
            if not spec:
                continue
            c = VALIDATORS[spec["fmt"]](o, rnd)
            if not c or not lint(c):
                continue
            fp = fp_text(c)
            if mem.is_dup(fp, 0.5) or any(sim(toks(fp), toks(fp_text(x))) > 0.6 for v in cands.values() for x in v):
                continue
            c.update(domain=spec["domain"], mechanic=spec["mechanic"], tone=spec["tone"])
            cands[spec["slot"]].append(c)
    best = rank_candidates(cands)
    out = []
    for s in slots:
        v = cands[s["slot"]]
        if v:
            c = v[best.get(s["slot"], 0)]
        else:
            c = bank_pick(s["fmt"], mem, rnd, bank_used) or bank_pick("reaction", mem, rnd, bank_used)
            log(f"  (writers' room fallback used for slot {s['slot']}: {c['yt_title'] if c else 'none'})")
            if c:
                c.update(domain=s["domain"], mechanic=s["mechanic"], tone=s["tone"], bank=True)
        out.append(c)
    return out


CTAS = ["Tell me this isn't just me 😅", "Tag the friend who does this", "Which one are you?", "Be honest. Is this you?",
        "Who else? 👇", "Send this to someone who needs it", "Rate this 1-10", "Drop your version in the comments"]


def short_meta(c, rnd):
    title = re.sub(r"\s+", " ", c.get("yt_title") or c.get("hook") or "Wait for it").strip()[:68]
    hook = (c.get("hook") or "").strip()
    desc = (hook[:1].upper() + hook[1:] + "\n\n" if hook else "") + pick(rnd, CTAS) + "\n\n#shorts #funny #memes #comedy #relatable"
    tags = ["shorts", "funny", "memes", "comedy", "relatable", "humor", "funny shorts", "meme"]
    tags += re.findall(r"[a-z]{4,}", (c.get("domain", "") + " " + hook).lower())[:5]
    return {"title": title, "description": desc, "tags": list(dict.fromkeys(tags))[:15]}


def plan_long(brief, mem, rnd):
    recent = "; ".join(mem.titles(60)) or "none"
    try:
        j = llm(f"""Plan ONE 7-minute comedy compilation for YouTube: a themed collection of ~35 mini meme skits.
Pick a theme ordinary people instantly recognise themselves in (a type of person, a place, a season, a situation).
Titles already used, never repeat: {recent}
Trend brief (inspiration only): {brief[:1500]}
Return JSON: {{"theme": "...", "title": "<= 70 chars, a curiosity or challenge, not ALL CAPS, honest", "description": "2-3 punchy sentences",
"tags": ["12 tags"], "thumb_text": "2-4 words, huge, e.g. NO WAY", "thumb_prompt": "one extreme close-up cartoon reaction face, huge exaggerated emotion, simple background",
"areas": ["8 different everyday areas inside the theme, in the style of: exams and studying, group chats, monday mornings"]}}""",
                system=HUMOR, temperature=1.0, max_tokens=3000)
        if isinstance(j, dict) and j.get("title") and len(j.get("areas") or []) >= 4:
            return j
    except Exception as e:
        log("  long plan fallback:", str(e)[:120])
    d = rnd.sample(DOMAINS, 8)
    return {"theme": d[0], "title": f"Try Not To Laugh: {d[0].title()} Edition"[:70], "description": "A new batch of mini skits. Try not to laugh.",
            "tags": ["funny", "comedy", "memes", "try not to laugh", "compilation", "relatable"], "thumb_text": "TRY NOT TO LAUGH",
            "thumb_prompt": "extreme close-up cartoon face trying not to laugh, cheeks puffed, watery eyes, simple background", "areas": d}


# --------------------------------------------------------------------------
# Sound design: synthesized sfx + music beds (numpy). Files in assets/ override/extend them.
# --------------------------------------------------------------------------
_RS = np.random.RandomState(1234)
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".mp4", ".mov", ".webm", ".mkv")


def tt(dur):
    return np.arange(int(dur * SR), dtype=np.float64) / SR


def lp(x, fc):
    a = 1 - np.exp(-2 * np.pi * fc / SR)
    n = int(min(len(x), max(8, 6 / a)))
    return np.convolve(x, a * (1 - a) ** np.arange(n))[:len(x)]


def hp(x, fc):
    return x - lp(x, fc)


def noise(dur):
    return _RS.uniform(-1, 1, int(dur * SR))


def sine(f, dur):
    return np.sin(2 * np.pi * f * tt(dur))


def saw(f, dur):
    return 2 * ((tt(dur) * f) % 1) - 1


def square(f, dur, duty=0.5):
    return np.where(((tt(dur) * f) % 1) < duty, 1.0, -1.0)


def tri(f, dur):
    return 2 * np.abs(2 * ((tt(dur) * f) % 1) - 1) - 1


def sweep(f0, f1, dur):
    t = tt(dur)
    f = f0 * (f1 / f0) ** (t / dur)
    return np.sin(2 * np.pi * np.cumsum(f) / SR)


def env_exp(dur, a=0.003, tau=0.2):
    t = tt(dur)
    return np.minimum(1, t / max(a, 1e-4)) * np.exp(-t / tau)


def env_adsr(dur, a=0.01, r=0.08):
    t = tt(dur)
    return np.minimum(1, t / max(a, 1e-4)) * np.minimum(1, np.maximum(0, (dur - t) / max(r, 1e-4)))


def reverb(x, tail=0.6, wet=0.2, seed=5):
    n = int(tail * SR)
    r = np.random.RandomState(seed)
    ir = lp(r.randn(n) * np.exp(-np.arange(n) / (SR * tail / 4.5)), 5000)
    ir[0] = 0
    total = len(x) + n
    nfft = 1 << (total - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(ir, nfft), nfft)[:total]
    y *= (np.max(np.abs(x)) + 1e-9) / (np.max(np.abs(y)) + 1e-9)
    out = np.zeros(total)
    out[:len(x)] += x
    return out + wet * y


def seq(parts, total):
    out = np.zeros(int(total * SR))
    for t, snd in parts:
        i = int(t * SR)
        out[i:i + len(snd)] += snd[:max(0, len(out) - i)]
    return out


def s_pop():
    return sweep(900, 300, 0.12) * env_exp(0.12, 0.002, 0.04)


def s_whoosh(d=0.45):
    n = noise(d)
    return (lp(n, 5000) - lp(n, 400)) * np.sin(np.pi * tt(d) / d) ** 2 * 1.6


def s_boom():
    d = 1.0
    x = sweep(120, 36, d) * env_exp(d, 0.003, 0.3) + lp(noise(d), 280) * env_exp(d, 0.001, 0.1) * 0.8 \
        + 0.35 * sine(55, d) * env_exp(d, 0.003, 0.5)
    return reverb(x, 0.7, 0.28)


def s_ding():
    d, t = 0.9, tt(0.9)
    x = sum(a * np.sin(2 * np.pi * 1318.5 * k * t) * np.exp(-t / (0.4 / k)) for k, a in ((1, 1), (2.0, .35), (3.01, .15)))
    return x * np.minimum(1, t / 0.002)


def s_fail():
    parts, t0 = [], 0.0
    for k, (f, d) in enumerate(((233.1, .34), (220.0, .34), (207.7, .34), (196.0, 1.0))):
        t = tt(d)
        vib = 1 + (0.02 * np.sin(2 * np.pi * 5.5 * t) if k == 3 else 0)
        x = lp(2 * ((np.cumsum(f * vib) / SR) % 1) - 1, 1500 if k < 3 else 900) * env_adsr(d, 0.03, 0.08 if k < 3 else 0.35)
        parts.append((t0, x * 0.8))
        t0 += d - 0.02
    return seq(parts, t0 + 0.1)


def s_scratch():
    d, t = 0.4, tt(0.4)
    f = 350 + 1700 * np.abs(np.sin(2 * np.pi * 2.4 * t))
    x = np.sign(np.sin(2 * np.pi * np.cumsum(f) / SR)) * 0.35 + lp(noise(d), 4500) * 0.8
    return x * np.hanning(len(t)) ** 0.35 * 0.9


def s_boing():
    d, t = 0.6, tt(0.6)
    f = 300 * np.exp(-t * 3) + 90 * np.sin(2 * np.pi * 16 * t) * np.exp(-t * 4) + 120
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * env_exp(d, 0.004, 0.25)


def s_riser(d=1.2):
    t = tt(d)
    return (hp(noise(d), 800) * 0.6 + sweep(200, 2600, d) * 0.5) * (t / d) ** 2


def s_crickets():
    parts = []
    for k in range(4):
        for j in range(3):
            t = tt(0.055)
            parts.append((0.15 + k * 0.4 + j * 0.07, np.sin(2 * np.pi * 4700 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 70 * t)) * np.hanning(len(t)) * 0.5))
    return seq(parts, 1.9)


def s_airhorn():
    parts = []
    for t0, d in ((0.0, 0.2), (0.26, 0.2), (0.52, 0.6)):
        t = tt(d)
        x = sum(saw(f * (1 + dv), d) for f in (440, 554, 659) for dv in (-0.008, 0.008))
        parts.append((t0, lp(x, 3200) * env_adsr(d, 0.01, 0.05) * (1 + 0.15 * np.sin(2 * np.pi * 22 * t)) * 0.18))
    return seq(parts, 1.2)


def s_glass():
    d, t = 1.0, tt(1.0)
    x = hp(noise(d), 3000) * env_exp(d, 0.001, 0.1) + sum(np.sin(2 * np.pi * f * t) * np.exp(-t / 0.18) * 0.18 for f in (4200, 5300, 6100, 7300))
    return reverb(x, 0.4, 0.25)


def s_click():
    return hp(noise(0.025), 2500) * env_exp(0.025, 0.0005, 0.005)


def s_notif():
    return seq([(0, sine(988, .1) * env_exp(.1, .002, .04)), (0.09, sine(1318, .3) * env_exp(.3, .002, .09))], 0.4)


def s_sent():
    d = 0.16
    return sweep(500, 1500, d) * np.hanning(int(d * SR)) * 0.8


def s_error():
    return seq([(0, square(233, .12) * env_adsr(.12, .004, .01) * .35), (0.17, square(233, .12) * env_adsr(.12, .004, .01) * .35)], 0.4)


def s_tada():
    parts = [(k * 0.09, sine(f, .5) * env_exp(.5, .004, .25)) for k, f in enumerate((523.3, 659.3, 784, 1046.5))]
    parts.append((0.4, sum(sine(f, .9) for f in (523.3, 659.3, 784, 1046.5)) * 0.3 * env_exp(.9, .005, .4)))
    return seq(parts, 1.4)


def s_sting():
    parts = []
    for t0, d in ((0.0, 0.28), (0.34, 0.28), (0.68, 1.2)):
        t = tt(d)
        x = sum(saw(f, d) for f in (73.4, 110.0, 138.6)) * 0.25 + lp(noise(d), 700) * 0.35 * np.exp(-t / 0.1)
        parts.append((t0, lp(x, 900) * env_exp(d, 0.004, d * 0.45) * 1.6))
    return reverb(seq(parts, 2.0), 0.7, 0.25)


def s_heartbeat():
    th = lambda: sweep(90, 45, .18) * env_exp(.18, .004, .06)
    return seq([(0, th()), (0.22, th() * .75), (0.9, th()), (1.12, th() * .75)], 1.5)


SFX_BUILD = {"pop": s_pop, "whoosh": s_whoosh, "boom": s_boom, "ding": s_ding, "fail": s_fail, "scratch": s_scratch,
             "boing": s_boing, "riser": s_riser, "crickets": s_crickets, "airhorn": s_airhorn, "glass": s_glass,
             "click": s_click, "notif": s_notif, "sent": s_sent, "error": s_error, "tada": s_tada, "sting": s_sting,
             "heartbeat": s_heartbeat}
SFX_GAIN = {"whoosh": .22, "pop": .45, "boom": 1.0, "ding": .4, "fail": .75, "scratch": .6, "boing": .5, "riser": .5,
            "crickets": .35, "airhorn": .55, "glass": .55, "click": .3, "notif": .5, "sent": .3, "error": .4, "tada": .5,
            "sting": .8, "heartbeat": .6}
_SFX_CACHE = {}


def find_user_file(folder, stem):
    d = ASSETS / folder
    if not d.exists():
        return None
    want = re.sub(r"[-_ ]", "", stem.lower())
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in AUDIO_EXT and re.sub(r"[-_ ]", "", p.stem.lower()) in (want, want + "sfx", "sfx" + want):
            return p
    return None


def get_sfx(name):
    """mono float32. A file assets/sfx/<name>.* replaces the synthesized sound."""
    if name in _SFX_CACHE:
        return _SFX_CACHE[name]
    x = None
    up = find_user_file("sfx", name) if name != "vine_boom" else None
    if name == "boom":
        up = find_user_file("sfx", "boom") or find_user_file("sfx", "vine_boom") or find_user_file("sfx", "vineboom")
    if up:
        try:
            x = load_audio(up, 1)[: int(5 * SR)].astype(np.float64)
        except Exception as e:
            log("  sfx file unreadable:", up.name, str(e)[:80])
    if x is None:
        x = SFX_BUILD[name]()
    x = (x / (np.max(np.abs(x)) + 1e-9) * 0.9).astype(np.float32)
    _SFX_CACHE[name] = x
    return x


# ---- music beds ----------------------------------------------------------
SCALES = {"major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10], "pent": [0, 2, 4, 7, 9], "dorian": [0, 2, 3, 5, 7, 9, 10],
          "phrygian": [0, 1, 3, 5, 7, 8, 10]}
MOOD_SPECS = {
    "bouncy": dict(bpm=(116, 128), root=60, scale="major", prog=[[0, 4, 5, 3], [0, 3, 4, 4], [0, 5, 3, 4]], bass="x.x.x.x.x.x.x.xx",
                   chord="x...x...x...x...", lead=0.55, kit=("x...x...x...x...", "....x.......x...", "x.x.x.x.x.x.x.x."), voice="square"),
    "sneaky": dict(bpm=(100, 112), root=57, scale="minor", prog=[[0, 0, 3, 4], [0, 5, 3, 4]], bass="x..x..x.x..x....",
                   chord="..x...x...x...x.", lead=0.5, kit=("x.......x.......", "........x.......", "x.x.x.x.x.x.x.x."), voice="pluck"),
    "chill": dict(bpm=(84, 96), root=57, scale="dorian", prog=[[0, 3, 6, 4], [0, 5, 3, 4], [1, 4, 0, 0]], bass="x.....x.x.....x.",
                  chord="x.......x.......", lead=0.35, kit=("x.....x.x.......", "........x.......", "x.x.x.x.x.x.x.x."), voice="soft", pad=True),
    "tense": dict(bpm=(96, 120), root=45, scale="phrygian", prog=[[0, 0, 1, 0], [0, 1, 0, 5]], bass="x.x.x.x.x.x.x.x.",
                  chord="x...............", lead=0.0, kit=("x...x...x...x...", "", ""), voice="saw", pad=True),
    "sad": dict(bpm=(66, 78), root=57, scale="minor", prog=[[0, 5, 2, 6], [0, 3, 5, 4]], bass="x...............",
                chord="x.......x.......", lead=0.4, kit=None, voice="soft", pad=True),
    "epic": dict(bpm=(104, 120), root=50, scale="minor", prog=[[0, 5, 2, 6], [0, 6, 5, 4]], bass="x...x...x...x...",
                 chord="x.......x.......", lead=0.5, kit=("x...x.x.x...x.x.", "....x.......x...", "x.x.x.x.x.x.x.x."), voice="saw", pad=True),
    "chaos": dict(bpm=(150, 168), root=62, scale="pent", prog=[[0, 3, 4, 0], [0, 5, 4, 3]], bass="x.xxx.xxx.xxx.xx",
                  chord="x.x.x.x.x.x.x.x.", lead=0.8, kit=("x..xx..xx..xx.x.", "....x..x....x...", "xxxxxxxxxxxxxxxx"), voice="square"),
}
_NOTE_CACHE = {}


def mtof(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def note(kind, midi, dur):
    key = (kind, midi, round(dur, 3))
    if key in _NOTE_CACHE:
        return _NOTE_CACHE[key]
    f = mtof(midi)
    if kind == "square":
        x = square(f, dur, 0.35) * env_exp(dur, 0.004, min(dur * 0.7, 0.16))
    elif kind == "pluck":
        x = lp(tri(f, dur) * 0.7 + saw(f, dur) * 0.3, 2400) * env_exp(dur, 0.002, 0.09)
    elif kind == "soft":
        x = (sine(f, dur) + 0.3 * sine(2 * f, dur) + 0.1 * sine(3 * f, dur)) * env_exp(dur, 0.012, min(dur * 0.8, 0.7))
    elif kind == "saw":
        x = lp(saw(f, dur) + saw(f * 1.006, dur), 1700) * env_adsr(dur, 0.02, 0.1) * 0.6
    elif kind == "pad":
        x = lp(saw(f, dur) + saw(f * 1.004, dur) + saw(f * 0.996, dur), 1100) * env_adsr(dur, 0.1, 0.25) * 0.4
    else:   # bass
        x = lp(sine(f, dur) + 0.3 * saw(f, dur), 520) * env_adsr(dur, 0.006, 0.05)
    _NOTE_CACHE[key] = x
    return x


def _drum(kind):
    if kind == "k":
        return sweep(150, 46, 0.2) * env_exp(0.2, 0.001, 0.07) * 1.1
    if kind == "s":
        return (hp(noise(0.18), 1500) * env_exp(0.18, 0.001, 0.05) * 0.8 + sine(190, 0.18) * env_exp(0.18, 0.001, 0.04) * 0.4)
    return hp(noise(0.05), 7000) * env_exp(0.05, 0.0005, 0.012) * 0.5


def _put(buf, snd, t, g=1.0):
    i = int(t * SR)
    if i < len(buf):
        seg = snd[:len(buf) - i]
        buf[i:i + len(seg)] += seg * g


def make_bed(mood, seconds, seed=0):
    """Original loopable background music (stereo float32, bpm)."""
    sp = MOOD_SPECS[mood]
    rnd = random.Random(seed)
    bpm = rnd.uniform(*sp["bpm"])
    step = 60.0 / bpm / 4
    bar = step * 16
    loop_bars = 4
    root = sp["root"] + rnd.choice([-2, 0, 2, 3, 5])
    sc = SCALES[sp["scale"]]
    prog = rnd.choice(sp["prog"])
    deg = lambda d, oct_=0: root + sc[d % len(sc)] + 12 * (d // len(sc)) + 12 * oct_
    pent = [0, 1, 2, 4, 5] if len(sc) == 7 else list(range(len(sc)))
    motif, cur = [], 2
    for s in range(16):
        if s % 2 == 0 and rnd.random() < sp["lead"] + (0.25 if s in (0, 8) else 0):
            cur = clamp(cur + rnd.choice([-2, -1, -1, 0, 1, 1, 2]), 0, len(pent) + 2)
            motif.append(pent[cur % len(pent)] + 7 * (cur // len(pent)))
        else:
            motif.append(None)
    buf = np.zeros(int((loop_bars * bar + 1.2) * SR))
    for b in range(loop_bars):
        d0 = prog[b % len(prog)]
        t_bar = b * bar
        if sp["kit"]:
            for s in range(16):
                for ch, kind, g in ((sp["kit"][0], "k", .9), (sp["kit"][1], "s", .7), (sp["kit"][2], "h", .55 if s % 4 else .8)):
                    if ch and ch[s] == "x":
                        _put(buf, _drum(kind), t_bar + s * step, g)
        for s in range(16):
            t = t_bar + s * step
            if sp["bass"][s] == "x":
                _put(buf, note("bass", deg(d0, -2), step * (1.6 if sp["voice"] != "pluck" else 0.7)), t, 0.8)
            if sp["chord"][s] == "x":
                for o in (0, 2, 4):
                    _put(buf, note("pad" if sp.get("pad") else sp["voice"], deg(d0 + o, -1), step * (7.5 if sp.get("pad") else 2)), t, 0.28)
            if motif[s] is not None:
                _put(buf, note(sp["voice"], deg(d0 + motif[s], 0), step * (1.8 if sp["voice"] != "soft" else 3)), t, 0.4)
    buf = reverb(buf, 0.5, 0.14)
    buf /= np.max(np.abs(buf)) + 1e-9
    loop_len = loop_bars * bar
    total = max(seconds, loop_len) + 1.0
    out = np.zeros(int(total * SR) + len(buf))
    for k in range(int(math.ceil(total / loop_len))):
        _put(out, buf, k * loop_len)
    out = out[:int(seconds * SR)] * 0.8
    d = int(0.017 * SR)
    right = out * 0.84 + 0.16 * np.concatenate([np.zeros(d), out[:-d]])
    return np.stack([out, right], 1).astype(np.float32), bpm


# ---- user music (assets/music) ---------------------------------------------
_USER_TRACKS = {}


def user_tracks():
    d = ASSETS / "music"
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT)


def music_for(mood, seconds, rnd, seed):
    """Returns stereo float32 of exactly `seconds`. Your tracks (assets/music) are preferred when present."""
    tracks = user_tracks()
    if tracks and rnd.random() < float(env("USER_MUSIC_SHARE", "0.85")):
        tagged = [p for p in tracks if p.stem.lower().startswith(mood)]
        p = pick(rnd, tagged or tracks)
        try:
            if p not in _USER_TRACKS:
                _USER_TRACKS[p] = load_audio(p, 2)
            a = _USER_TRACKS[p]
            n = int(seconds * SR)
            if len(a) >= n:
                off = 0 if len(a) < 1.3 * n or rnd.random() < 0.5 else rnd.randrange(0, len(a) - n)
                return a[off:off + n].copy()
            reps = int(math.ceil(n / len(a)))
            return np.tile(a, (reps, 1))[:n].copy()
        except Exception as e:
            log("  music file unreadable:", p.name, str(e)[:80])
    return make_bed(mood, seconds, seed)[0]


# ---- the mixer -------------------------------------------------------------
class Mix:
    def __init__(self, seconds):
        self.n = int(seconds * SR) + SR // 2
        self.music = np.zeros((self.n, 2), np.float32)
        self.fx = np.zeros((self.n, 2), np.float32)
        self.vo = np.zeros(self.n, np.float32)
        self.mask = np.zeros(self.n, np.float32)

    def add_music(self, arr, t0, dur, gain=0.5, fade_in=0.04, fade_out=0.35):
        i, n = int(t0 * SR), int(dur * SR)
        arr = arr[:n].copy()
        if len(arr) < 8:
            return
        fi, fo = min(int(fade_in * SR), len(arr) // 2), min(int(fade_out * SR), len(arr) // 2)
        if fi:
            arr[:fi] *= np.linspace(0, 1, fi)[:, None]
        if fo:
            arr[-fo:] *= np.linspace(1, 0, fo)[:, None]
        seg = arr[:max(0, self.n - i)]
        self.music[i:i + len(seg)] += seg * gain

    def add_sfx(self, name, t, gain=1.0, pan=0.0):
        x = get_sfx(name) * SFX_GAIN.get(name, 0.5) * gain
        i = int(t * SR)
        seg = x[:max(0, self.n - i)]
        l, r = (1 - max(0, pan)) , (1 + min(0, pan))
        self.fx[i:i + len(seg), 0] += seg * l
        self.fx[i:i + len(seg), 1] += seg * r

    def add_vo(self, wav, t, gain=1.0):
        i = int(t * SR)
        seg = wav[:max(0, self.n - i)]
        self.vo[i:i + len(seg)] += seg * gain
        self.mask[i:i + len(seg)] = 1.0

    def render(self, duck=0.62):
        k = int(0.28 * SR)
        pad = np.pad(self.mask, (k, k)).astype(np.float64)
        c = np.concatenate([[0.0], np.cumsum(pad)])
        env = ((c[2 * k + 1:] - c[:-(2 * k + 1)]) / (2 * k + 1)).astype(np.float32)
        g = (1 - duck * np.clip(env * 1.6, 0, 1)).astype(np.float32)
        out = self.music * g[:, None] + self.fx + self.vo[:, None] * 0.95
        return np.tanh(out * 0.95).astype(np.float32) / np.tanh(0.95)


def write_wav(path, arr):
    pcm = (np.clip(arr, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(arr.shape[1] if arr.ndim > 1 else 1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


# --------------------------------------------------------------------------
# Typography: meme captions with outline + shadow, accent words (*word*), colour emoji.
# --------------------------------------------------------------------------
FONT_DIRS = [ASSETS / "fonts", Path("/usr/share/fonts/truetype/google-fonts"), Path("/usr/share/fonts/truetype/liberation"),
             Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/truetype/noto"), Path("/usr/share/fonts/truetype/montserrat"),
             Path("/usr/share/fonts/truetype/oswald"), Path("/usr/share/fonts/truetype/roboto"), Path("/usr/share/fonts/truetype"),
             Path.home() / ".fonts"]
FONT_PREF = {
    "impact": ["Anton-Regular.ttf", "Impact.ttf", "BebasNeue-Regular.ttf", "ArchivoBlack-Regular.ttf", "Poppins-ExtraBold.ttf",
               "Poppins-Bold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"],
    "modern": ["Poppins-ExtraBold.ttf", "Montserrat-ExtraBold.ttf", "Poppins-Bold.ttf", "Montserrat-Bold.ttf", "LiberationSans-Bold.ttf",
               "DejaVuSans-Bold.ttf"],
    "ui": ["Inter-Regular.ttf", "Roboto-Regular.ttf", "Poppins-Regular.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"],
    "ui_bold": ["Roboto-Bold.ttf", "Poppins-SemiBold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"],
}
_FONT_PATH, _FONTS = {}, {}


def find_font(role):
    if role not in _FONT_PATH:
        found = None
        for name in FONT_PREF[role]:
            for d in FONT_DIRS:
                if (d / name).exists():
                    found = str(d / name)
                    break
            if found:
                break
        _FONT_PATH[role] = found
    return _FONT_PATH[role]


def get_font(role, size):
    key = (role, int(size))
    if key not in _FONTS:
        p = find_font(role)
        try:
            _FONTS[key] = ImageFont.truetype(p, int(size)) if p else ImageFont.load_default(int(size))
        except Exception:
            _FONTS[key] = ImageFont.load_default(int(size))
    return _FONTS[key]


EMOJI_RE = re.compile(r"([\U0001F000-\U0001FAFF☀-➿⬀-⯿](?:[️‍\U0001F000-\U0001FAFF☀-➿])*)")
_EMO = {"font": None, "tried": False}
_EMO_IMG = {}


def emoji_font():
    if not _EMO["tried"]:
        _EMO["tried"] = True
        for p in (ASSETS / "fonts" / "NotoColorEmoji.ttf", Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
                  Path("/usr/share/fonts/noto/NotoColorEmoji.ttf")):
            if p.exists():
                try:
                    _EMO["font"] = ImageFont.truetype(str(p), 109)
                    break
                except Exception:
                    pass
    return _EMO["font"]


def emoji_img(run, size):
    key = (run, int(size))
    if key not in _EMO_IMG:
        im, f = None, emoji_font()
        if f:
            try:
                tmp = Image.new("RGBA", (420, 190), (0, 0, 0, 0))
                ImageDraw.Draw(tmp).text((10, 10), run, font=f, embedded_color=True)
                bb = tmp.getbbox()
                if bb:
                    tmp = tmp.crop(bb)
                    k = size * 1.0 / tmp.height
                    im = tmp.resize((max(1, int(tmp.width * k)), max(1, int(tmp.height * k))), Image.LANCZOS)
            except Exception:
                im = None
        _EMO_IMG[key] = im
    return _EMO_IMG[key]


def _segs(word):
    return [(s, i % 2 == 1) for i, s in enumerate(EMOJI_RE.split(word)) if s]


def word_width(word, font, size):
    w = 0.0
    for s, em in _segs(word):
        if em:
            im = emoji_img(s, size)
            w += (im.width + size * 0.05) if im else 0
        else:
            w += font.getlength(s)
    return w


def parse_runs(text):
    out, open_ = [], False
    for part in re.split(r"(\*[^*]+\*)", text):
        if not part:
            continue
        acc = len(part) > 2 and part.startswith("*") and part.endswith("*")
        s = part[1:-1] if acc else part.replace("*", "")
        for i, w in enumerate(s.split(" ")):
            if w == "":
                open_ = False
                continue
            if i == 0 and open_ and out:
                out[-1] = (out[-1][0] + w, out[-1][1] or acc)
            else:
                out.append((w, acc))
            open_ = True
        if s.endswith(" "):
            open_ = False
    return out


def text_block(text, role, size, max_w, fill, stroke=None, sw=0, accent=(255, 226, 0), align="center", lh=1.12):
    font = get_font(role, size)
    space = font.getlength(" ")
    lines, cur, cw = [], [], 0.0
    for w, acc in parse_runs(text):
        ww = word_width(w, font, size)
        if cur and cw + space + ww > max_w:
            lines.append((cur, cw))
            cur, cw = [], 0.0
        cw += (space if cur else 0) + ww
        cur.append((w, acc))
    if cur:
        lines.append((cur, cw))
    if not lines:
        return Image.new("RGBA", (4, 4), (0, 0, 0, 0)), 0
    pad = sw + 4
    Wb = int(max(l[1] for l in lines)) + 2 * pad
    lh_px = int(size * lh)
    Hb = lh_px * len(lines) + 2 * pad + int(size * 0.18)
    img = Image.new("RGBA", (Wb, Hb), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = pad
    for line, lw in lines:
        x = pad + ((Wb - 2 * pad - lw) / 2 if align == "center" else 0)
        for w, acc in line:
            col = accent if acc else fill
            for s, em in _segs(w):
                if em:
                    im = emoji_img(s, size)
                    if im:
                        try:
                            img.alpha_composite(im, (int(x), int(y + size * 0.08)))
                        except Exception:
                            pass
                        x += im.width + size * 0.05
                else:
                    d.text((x, y), s, font=font, fill=col, stroke_width=sw, stroke_fill=stroke or col)
                    x += font.getlength(s)
            x += space
        y += lh_px
    return img, len(lines)


CAP = {   # size = fraction of the SHORTER frame side
    "impact": dict(role="impact", upper=True, size=0.100, fill=(255, 255, 255), stroke=(0, 0, 0), sw=0.085, shadow=True),
    "modern": dict(role="modern", upper=False, size=0.080, fill=(255, 255, 255), stroke=(0, 0, 0), sw=0.085, shadow=True),
    "bar": dict(role="modern", upper=False, size=0.070, fill=(14, 14, 14), stroke=None, sw=0, shadow=False, accent=(220, 30, 30)),
    "sub": dict(role="modern", upper=False, size=0.062, fill=(255, 255, 255), stroke=(0, 0, 0), sw=0.10, shadow=True),
    "label": dict(role="modern", upper=True, size=0.040, fill=(255, 255, 255), stroke=(0, 0, 0), sw=0.10, shadow=True),
    "card": dict(role="impact", upper=True, size=0.175, fill=(255, 255, 255), stroke=(0, 0, 0), sw=0.07, shadow=True),
}
_CAPS = {}


def render_caption(text, W, H, style="modern", max_w=0.9, max_h=0.36, boost=1.0):
    key = (text, style, W, H, max_w, max_h, boost)
    if key in _CAPS:
        return _CAPS[key]
    st = CAP[style]
    s0 = min(W, H)
    size = int(s0 * st["size"] * boost)
    lo = int(s0 * 0.036)
    txt = text.upper() if st["upper"] else text
    while True:
        sw = int(size * st["sw"])
        img, n = text_block(txt, st["role"], size, int(W * max_w), st["fill"], st["stroke"], sw, st.get("accent", (255, 226, 0)))
        if (img.height <= H * max_h and n <= 5) or size <= lo:
            break
        size -= max(2, size // 12)
    if st["shadow"] and sw:
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sh.putalpha(img.split()[3].filter(ImageFilter.GaussianBlur(max(2, sw * 0.8))).point(lambda v: int(v * 0.65)))
        out = Image.new("RGBA", img.size, (0, 0, 0, 0))
        out.alpha_composite(sh, (0, int(sw * 0.7)))
        out.alpha_composite(img)
        img = out
    if len(_CAPS) > 600:
        _CAPS.clear()
    _CAPS[key] = img
    return img


# --------------------------------------------------------------------------
# Procedural backgrounds
# --------------------------------------------------------------------------
def card_base(style, w, h, c1, c2, seed=0):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2, h / 2
    c1, c2 = np.array(c1, np.float32), np.array(c2, np.float32)
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / math.hypot(cx, cy)
    if style == "burst":
        t = (np.floor((np.arctan2(yy - cy, xx - cx) + np.pi) / (2 * np
