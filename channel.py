#!/usr/bin/env python3
"""
Daily comedy channel pipeline (English, US audience).

Per day: 3 Shorts (vertical, ~25-45s) + 1 long video (16:9, 6+ minutes).

Flow for every video
  trends -> many ideas -> harsh judge picks the best -> write script ->
  judge -> punch-up rewrite loop -> (quality gate) -> voice (Gemini TTS) ->
  images (Cloudflare Workers AI) -> ffmpeg montage + burned captions + sfx ->
  upload to YouTube (scheduled for US prime time).

Secrets (GitHub Actions -> env):
  GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3   text + voice (rotated)
  CF_ACCOUNT_ID, CF_API_TOKEN                          images (+ voice fallback)
  PIXABAY_API_KEY                                      image fallback
  YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN     YouTube upload

Usage
  python channel.py                 full daily run
  python channel.py --dry-run       everything except the upload
  python channel.py --only short    only shorts   (or: --only long)
  python channel.py --selftest      offline montage test (no APIs needed)
"""
import argparse
import base64
import datetime as dt
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
HISTORY_FILE = ROOT / "history.json"
FPS = 25
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def env(name, default=""):
    return os.environ.get(name, default).strip()


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
GEMINI_KEYS = [k for k in (env("GEMINI_API_KEY"), env("GEMINI_API_KEY_2"), env("GEMINI_API_KEY_3")) if k]
TEXT_MODELS = [m.strip() for m in env(
    "GEMINI_MODELS", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-2.5-flash").split(",") if m.strip()]
TTS_MODELS = [m.strip() for m in env(
    "GEMINI_TTS_MODELS", "gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts,gemini-2.5-flash-preview-tts").split(",") if m.strip()]
CF_ACCOUNT, CF_TOKEN = env("CF_ACCOUNT_ID"), env("CF_API_TOKEN")
PIXABAY_KEY = env("PIXABAY_API_KEY")

SHORTS_PER_DAY = int(env("SHORTS_PER_DAY", "3"))
LONGS_PER_DAY = int(env("LONGS_PER_DAY", "1"))
MIN_SCORE = float(env("MIN_SCORE", "8"))            # judge score needed to stop rewriting
PUBLISH_FLOOR = float(env("PUBLISH_FLOOR", "7"))    # below this the video is NOT published
MAX_ROUNDS = int(env("MAX_ROUNDS", "3"))
LONG_MIN_SECONDS = float(env("LONG_MIN_SECONDS", "375"))
PUBLISH_MODE = env("PUBLISH_MODE", "schedule")      # schedule | now | private
SHORT_SLOTS_UTC = [int(x) for x in env("SHORT_SLOTS_UTC", "15,19,23").split(",")]
LONG_SLOT_UTC = int(env("LONG_SLOT_UTC", "1"))
NARRATOR_VOICE = env("NARRATOR_VOICE", "Puck")
VOICES = {"NARRATOR": NARRATOR_VOICE, "A": "Fenrir", "B": "Kore", "C": "Charon"}
IMAGE_STYLE = env(
    "IMAGE_STYLE",
    "flat 2D cartoon illustration, thick black outlines, bold saturated colors, exaggerated funny "
    "facial expressions, simple clean background, meme-comic style, no text, no letters, no logos, no watermark")
OFFLINE = env("OFFLINE_TEST") == "1"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def parse_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"[\[{]", t)
    if not m:
        raise ValueError("no JSON found in model output")
    open_c = t[m.start()]
    close_c = "}" if open_c == "{" else "]"
    end = t.rfind(close_c)
    return json.loads(t[m.start():end + 1])


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("command failed: " + " ".join(map(str, cmd[:6])) + "\n" + r.stderr[-1500:])
    return r


def probe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []


def save_history(h):
    HISTORY_FILE.write_text(json.dumps(h[-400:], ensure_ascii=False, indent=1))


# --------------------------------------------------------------------------
# Gemini (text + voice) with key rotation and model fallback
# --------------------------------------------------------------------------
class ModelUnavailable(Exception):
    pass


class BadRequest(Exception):
    pass


_key_index = 0


def gemini_post(model, payload, timeout=240):
    """POST generateContent; rotates keys on quota/auth errors, retries on 5xx."""
    global _key_index
    if not GEMINI_KEYS:
        raise RuntimeError("no GEMINI_API_KEY set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    last = ""
    for attempt in range(4):
        for _ in range(len(GEMINI_KEYS)):
            key = GEMINI_KEYS[_key_index % len(GEMINI_KEYS)]
            try:
                r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                                  json=payload, timeout=timeout)
            except requests.RequestException as e:
                last = str(e)
                _key_index += 1
                continue
            if r.status_code == 200:
                return r.json()
            last = f"{r.status_code} {r.text[:300]}"
            if r.status_code == 404:
                raise ModelUnavailable(last)
            if r.status_code == 400:
                raise BadRequest(last)
            _key_index += 1                      # 401/403/429/5xx -> try the next key
        time.sleep(8 * (attempt + 1))
    raise RuntimeError("Gemini failed on all keys: " + last)


def llm(prompt, system=None, json_out=False, temperature=1.0, grounded=False):
    gen = {"temperature": temperature, "maxOutputTokens": 16000}
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if grounded:
        payload["tools"] = [{"google_search": {}}]
    elif json_out:
        gen["responseMimeType"] = "application/json"
    last = None
    for model in TEXT_MODELS:
        for _ in range(2):
            try:
                data = gemini_post(model, payload)
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                return parse_json(text) if json_out else text
            except ModelUnavailable as e:
                last = e
                break
            except (BadRequest, KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
                last = e                          # malformed output: one retry on the same model
                continue
    raise RuntimeError(f"all text models failed: {last}")


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------
def trend_brief():
    bits = []
    try:
        r = requests.get("https://trends.google.com/trending/rss?geo=US", headers=UA, timeout=20)
        titles = re.findall(r"<title>(.*?)</title>", r.text)[1:16]
        if titles:
            bits.append("Google Trends US right now: " + "; ".join(titles))
    except Exception as e:
        log("  (google trends skipped:", e, ")")
    for sub in ("funny", "AskReddit", "Unexpected", "mildlyinfuriating", "CasualConversation"):
        try:
            r = requests.get(f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=6", headers=UA, timeout=20)
            posts = [c["data"]["title"] for c in r.json()["data"]["children"]]
            bits.append(f"Reddit r/{sub} top today: " + " | ".join(posts))
        except Exception:
            pass
    try:
        g = llm("In 10 short bullets: which comedy formats, meme templates, sounds and relatable everyday topics "
                "are going viral on YouTube Shorts and TikTok for US viewers THIS week? Be concrete.", grounded=True)
        bits.append("Web search trend notes:\n" + g[:2500])
    except Exception as e:
        log("  (grounded trend search skipped:", str(e)[:120], ")")
    brief = "\n".join(bits) or "No live trend data available; use evergreen relatable American everyday situations."
    log("Trend brief ready:", len(brief), "chars")
    return brief


# --------------------------------------------------------------------------
# Writing: ideas -> judge -> script -> punch-up
# --------------------------------------------------------------------------
COMEDY_RULES = """COMEDY RULES (non-negotiable)
- Audience: Americans aged 16-40 scrolling YouTube. Write like a sharp stand-up / late-night staff writer, never like an AI.
- Line 1 is the hook (spoken in <= 2 seconds): a specific, absurd or painfully relatable claim. NEVER open with greetings, "Did you know", "Imagine", "Let's", "So", "Ever wonder".
- Specific beats generic: real American details (Costco samples, Target runs, self-checkout, Ring doorbells, Venmo requests, group chats, "per my last email", HOA meetings, Wawa vs Sheetz, gas station coffee), exact numbers, oddly precise details.
- Joke density: a laugh every 3-5 seconds. Every line either sets up or pays off. No filler lines.
- Tools: escalation (each beat crazier than the last), rule of three where the third item breaks the pattern, misdirection, deadpan understatement, confident delusional characters, callbacks.
- The last line is a button: short, sharp, and it loops back into line 1 so the video replays.
- BANNED: "plot twist", "spoiler alert", "buckle up", "in a world", "game changer", "let that sink in", "we've all been there", "here's the thing", explaining the joke, moral/lesson endings, "like and subscribe".
- Advertiser-friendly: no politics, religion, real named people, tragedies, sexual content, slurs, or punching down. Mild language only.
- Spoken text must sound like real speech: contractions, short sentences, no stage directions, no emojis."""

SCENE_SCHEMA = ('{"speaker":"NARRATOR|A|B","line":"spoken words only","delivery":"2-5 word acting note",'
                '"shot":"concrete funny picture for this moment","chars":["A"],"sfx":"none|pop|boom|ding|fail","beat":0.0}')


def pick_ideas(kind, brief, history, n_pick):
    recent = "; ".join(h.get("title", "") for h in history[-60:]) or "none yet"
    if kind == "short":
        spec = ("a YouTube Short of 25-45 seconds. Fast, one premise, escalating, ends on a punchline that loops. "
                "Formats to mix: POV monologue, two-character argument, fake nature documentary about Americans, "
                "honest warning labels, 'things that are legal but shouldn't be', ranking X from least to most unhinged, "
                "customer-service call going off the rails, overconfident expert, passive-aggressive AI assistant, "
                "tiny everyday annoyance treated like an epic disaster.")
    else:
        spec = ("a 7-8 minute YouTube video made of 8-9 escalating segments under one big premise (e.g. 'N types of ...', "
                "'a day as ...', 'if [thing] was brutally honest', 'every [situation] in America'). Each segment must be "
                "funny on its own AND the whole thing must build, with running gags and a callback finale.")
    prompt = f"""Trend inspiration (topics/formats only; do NOT copy any existing video, make everything original):
{brief}

Titles already used, do not repeat these premises: {recent}

{COMEDY_RULES}

Brainstorm 14 ORIGINAL concepts for {spec}
Return JSON: a list of objects {{"title":"","format":"","premise":"","hook_line":"","punchline":"","why_funny":""}}"""
    ideas = llm(prompt, json_out=True, temperature=1.1)
    judge = llm(f"""You are a ruthless comedy development exec. Rate each concept 1-10 for how likely it is to make a US viewer laugh out loud and share it.
Calibration: 5 = forgettable, 7 = smile, 8 = real laugh, 9+ = send-to-friends. Most deserve 3-6. Penalize generic, preachy, 'relatable-but-no-joke' ideas.
Concepts:
{json.dumps(ideas, ensure_ascii=False)}
Return JSON: a list of {{"index": int, "score": number}} for every concept.""", json_out=True, temperature=0.2)
    scores = {int(j["index"]): float(j["score"]) for j in judge}
    order = sorted(range(len(ideas)), key=lambda i: -scores.get(i, 0))
    picked = [ideas[i] for i in order[:n_pick]]
    log("Picked ideas:", [(p.get("title"), scores.get(order[k])) for k, p in enumerate(picked)])
    return picked


def clean_scenes(scenes, kind):
    out = []
    for s in scenes:
        line = re.sub(r"[\[\(][^\]\)]*[\]\)]", "", str(s.get("line", "")))          # stage directions
        line = re.sub(r"[^\w\s'.,!?$%&:;\-\"’…/+@#]", "", line).strip()
        if not line:
            continue
        sp = str(s.get("speaker", "NARRATOR")).upper()
        sfx = s.get("sfx", "none")
        out.append({
            "speaker": sp if sp in VOICES else "NARRATOR",
            "line": line,
            "delivery": str(s.get("delivery", "deadpan"))[:60],
            "shot": str(s.get("shot", line))[:400],
            "chars": [c for c in s.get("chars", []) if isinstance(c, str)],
            "sfx": sfx if sfx in ("none", "pop", "boom", "ding", "fail") else "none",
            "beat": max(0.0, min(1.2, float(s.get("beat", 0) or 0))),
        })
    return out[:14] if kind == "short" else out


def numbered(scenes):
    return "\n".join(f"{i}. [{s['speaker']}] {s['line']}" for i, s in enumerate(scenes))


def judge_script(title, scenes, long=False):
    prompt = f"""You are a ruthless comedy editor at a late-night show. Title: {title}
Score this script 1-10 on: hook strength, joke density (laugh every 3-5s), originality (does it feel like AI filler?), escalation, ending button/loop, US relatability, speakability.
Calibration: 5 = forgettable, 7 = smile, 8 = real laugh, 9+ = would send to friends. Most drafts deserve 4-6. Be specific and harsh.
{numbered(scenes)}
Return JSON: {{"score": number, "weakest": [{{"scene": int, "problem": "", "fix": ""}}], "summary": ""}}"""
    j = llm(prompt, json_out=True, temperature=0.2)
    return float(j.get("score", 0)), j


def write_short(idea, brief):
    base = f"""Write the full script for this Short.
Concept: {json.dumps(idea, ensure_ascii=False)}

{COMEDY_RULES}

Length: 55-95 spoken words total, 7-11 scenes, each line <= 14 words (about 1.5-3 seconds).
Characters: define 1-2 characters in "cast" (key A, B) with a vivid visual description (species/age/clothes/colors) that will be repeated for every picture. Use speaker NARRATOR for narration, A/B for dialogue.
sfx: use "boom"/"ding"/"fail" ONLY on a punchline (max 2 per video), "pop" for small beats. beat = pause in seconds after the line (0-1) for comic timing.
Return JSON: {{"title":"<= 70 chars, curiosity-driven, no lies, not ALL CAPS","description":"1-2 punchy sentences","tags":["8-12 tags"],"cast":{{"A":"..."}},"scenes":[{SCENE_SCHEMA}]}}"""
    script = llm(base, json_out=True, temperature=1.0)
    best, best_score = None, -1
    for rnd in range(MAX_ROUNDS):
        script["scenes"] = clean_scenes(script["scenes"], "short")
        score, j = judge_script(script["title"], script["scenes"])
        log(f"  short '{script['title']}' round {rnd + 1}: score {score}")
        if score > best_score:
            best, best_score = json.loads(json.dumps(script)), score
        if score >= MIN_SCORE:
            break
        fix = llm(f"""You are the punch-up room. Rewrite this short script so it is genuinely funnier.
Editor notes: {json.dumps(j, ensure_ascii=False)}
Current script JSON: {json.dumps(script, ensure_ascii=False)}
{COMEDY_RULES}
Replace weak lines with more specific, surprising ones. Keep the strong ones. Fix the opening hook and the ending button. Same length budget (55-95 words), same JSON schema, same cast. Return only the JSON.""",
                  json_out=True, temperature=1.0)
        if isinstance(fix, dict) and fix.get("scenes"):
            script = fix
    best["score"] = best_score
    best["idea"] = idea.get("premise", "")
    return best


def est_seconds(scenes):
    words = sum(len(s["line"].split()) for s in scenes)
    return words / 2.55 + 0.3 * len(scenes)


def write_segment(outline, seg, k, prev_tail, notes=None, existing=None):
    prompt = f"""You are writing segment {k + 1} of a long comedy video for US viewers.
Video: {outline['title']} - premise: {outline.get('premise', '')}
Cast (repeat these visual descriptions in every picture): {json.dumps(outline.get('cast', {}), ensure_ascii=False)}
Running gags to keep alive / call back to: {json.dumps(outline.get('running_gags', []), ensure_ascii=False)}
This segment: "{seg.get('heading', '')}" - angle: {seg.get('angle', '')}
Last lines of the previous segment (keep flow, do not repeat): {prev_tail}

{COMEDY_RULES}

Write 4-5 scenes, 110-150 spoken words total for this segment, each scene's line 15-35 words (2-3 spoken sentences, with a laugh in every sentence).
The segment must escalate and end on its own big laugh. Use speaker NARRATOR mainly; A/B only for short bits of dialogue.
{"Editor notes to fix: " + json.dumps(notes, ensure_ascii=False) if notes else ""}
{"Previous draft to beat: " + json.dumps(existing, ensure_ascii=False) if existing else ""}
Return JSON: a list of scenes: [{SCENE_SCHEMA}]"""
    sc = llm(prompt, json_out=True, temperature=1.0)
    if isinstance(sc, dict):
        sc = sc.get("scenes", [])
    return clean_scenes(sc, "long")


def punch_segment(outline, seg, k, scenes, prev_tail):
    fix = llm(f"""You are the punch-up room. Make this segment funnier without making it longer.
Segment "{seg.get('heading', '')}": {json.dumps(scenes, ensure_ascii=False)}
{COMEDY_RULES}
Swap every flat line for something more specific and surprising, tighten setups, sharpen the final laugh. Same JSON schema, same number of scenes (+/-1). Return only a JSON list of scenes.""",
              json_out=True, temperature=1.0)
    if isinstance(fix, dict):
        fix = fix.get("scenes", [])
    fix = clean_scenes(fix, "long")
    return fix if len(fix) >= 3 else scenes


def write_long(idea, brief):
    outline = llm(f"""Plan a 7-8 minute comedy video for US viewers.
Concept: {json.dumps(idea, ensure_ascii=False)}
{COMEDY_RULES}
Return JSON: {{"title":"<= 70 chars, curiosity-driven, no lies","description":"2-3 sentences","tags":["10-15 tags"],
"premise":"one sentence","cast":{{"A":"vivid visual description","B":"..."}},"running_gags":["2-3 gags to call back"],
"segments":[{{"heading":"","angle":"the comic angle and how it escalates"}}]}}
Use exactly 9 segments; segment 1 is a cold-open hook, segment 9 is a callback finale.""", json_out=True, temperature=1.0)
    segs, tail = [], ""
    for k, seg in enumerate(outline["segments"]):
        log(f"  long segment {k + 1}/{len(outline['segments'])}: {seg.get('heading')}")
        sc = write_segment(outline, seg, k, tail)
        sc = punch_segment(outline, seg, k, sc, tail)
        segs.append(sc)
        tail = " / ".join(s["line"] for s in sc[-2:])
    extra = 0
    while est_seconds([s for g in segs for s in g]) < LONG_MIN_SECONDS + 25 and extra < 3:
        extra += 1
        seg = {"heading": f"Bonus {extra}", "angle": "one more escalation of the best running gag"}
        log("  adding bonus segment for length")
        segs.insert(len(segs) - 1, write_segment(outline, seg, len(segs) - 1, tail))
    flat = [s for g in segs for s in g]
    score, j = judge_script(outline["title"], flat, long=True)
    log(f"  long '{outline['title']}' score {score}")
    if score < MIN_SCORE:
        weak = sorted({min(len(segs) - 1, int(w["scene"]) * len(segs) // max(1, len(flat)))
                       for w in j.get("weakest", []) if str(w.get("scene", "")).isdigit()})[:3]
        for idx in weak:
            seg = outline["segments"][min(idx, len(outline["segments"]) - 1)]
            segs[idx] = write_segment(outline, seg, idx, "", notes=j.get("weakest"), existing=segs[idx])
        flat = [s for g in segs for s in g]
        score, _ = judge_script(outline["title"], flat, long=True)
        log(f"  long after rewrite: score {score}")
    outline["scenes"] = flat
    outline["score"] = score
    outline["idea"] = idea.get("premise", "")
    return outline


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------
def pcm_to_wav(pcm, path, rate=24000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def tts_gemini(text, voice, style, out_wav):
    style = f"{style}; natural American English, great comedic timing"
    for model in TTS_MODELS:
        legacy = "2.5" in model
        variants = ([_tts_payload_old(text, voice, style)] if legacy else
                    [_tts_payload_new(text, voice, style), _tts_payload_old(text, voice, style)])
        for payload in variants:
            try:
                data = gemini_post(model, payload, timeout=120)
                part = data["candidates"][0]["content"]["parts"][0]
                blob = base64.b64decode((part.get("inlineData") or part.get("inline_data"))["data"])
                if blob[:4] == b"RIFF":
                    Path(out_wav).write_bytes(blob)
                else:
                    pcm_to_wav(blob, out_wav)
                return True
            except ModelUnavailable:
                break
            except (BadRequest, KeyError, IndexError, TypeError):
                continue
            except Exception as e:
                log("    gemini tts error:", str(e)[:150])
                break
    return False


def _tts_payload_new(text, voice, style):
    return {"contents": [{"role": "user", "parts": [{"text": text, "speech_metadata": {"style": style}}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"voice": voice}}}}


def _tts_payload_old(text, voice, style):
    return {"contents": [{"role": "user", "parts": [{"text": f"Say this in a {style} way: {text}"}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}


def tts_cloudflare(text, out_wav):
    if not (CF_ACCOUNT and CF_TOKEN):
        return False
    try:
        r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/@cf/myshell-ai/melotts",
                          headers={"Authorization": f"Bearer {CF_TOKEN}"}, json={"prompt": text, "lang": "en"}, timeout=90)
        j = r.json()
        b64 = (j.get("result") or j).get("audio")
        raw = Path(str(out_wav) + ".raw")
        raw.write_bytes(base64.b64decode(b64))
        run(["ffmpeg", "-y", "-i", str(raw), "-ar", "24000", "-ac", "1", str(out_wav)])
        return True
    except Exception as e:
        log("    cloudflare tts error:", str(e)[:150])
        return False


def tts_edge(text, voice, out_wav):
    try:
        raw = Path(str(out_wav) + ".mp3")
        run([sys.executable, "-m", "edge_tts", "--voice", "en-US-GuyNeural" if voice != "Kore" else "en-US-JennyNeural",
             "--text", text, "--write-media", str(raw)])
        run(["ffmpeg", "-y", "-i", str(raw), "-ar", "24000", "-ac", "1", str(out_wav)])
        return True
    except Exception:
        return False


def tts_stub(text, out_wav):
    dur = max(0.8, len(text.split()) * 0.36)
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=f=180:d={dur:.2f},tremolo=f=5:d=0.8,volume=0.5",
         "-ar", "24000", "-ac", "1", str(out_wav)])
    return True


def make_voice(text, speaker, delivery, out_wav):
    out_wav = Path(out_wav)
    raw = out_wav.with_suffix(".raw.wav")
    ok = (tts_stub(text, raw) if OFFLINE else
          tts_gemini(text, VOICES.get(speaker, NARRATOR_VOICE), delivery, raw)
          or tts_cloudflare(text, raw) or tts_edge(text, VOICES.get(speaker), raw))
    if not ok:
        raise RuntimeError("all voice engines failed for: " + text[:60])
    # trim leading/trailing silence so comedic timing stays tight
    trim = ("silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB,areverse,"
            "silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB,areverse")
    run(["ffmpeg", "-y", "-i", str(raw), "-af", trim, "-ar", "44100", "-ac", "1", str(out_wav)])
    raw.unlink(missing_ok=True)
    if not OFFLINE:
        time.sleep(float(env("TTS_PAUSE", "1.5")))


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------
def _save_b64(b64, path):
    Path(path).write_bytes(base64.b64decode(b64))


def img_cloudflare(prompt, seed, path):
    if not (CF_ACCOUNT and CF_TOKEN):
        return False
    hdr = {"Authorization": f"Bearer {CF_TOKEN}"}
    base = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/"
    try:
        r = requests.post(base + "@cf/black-forest-labs/flux-1-schnell", headers=hdr,
                          json={"prompt": prompt[:2000], "steps": 6, "seed": seed}, timeout=120)
        j = r.json()
        b64 = (j.get("result") or j).get("image")
        if b64:
            _save_b64(b64, path)
            return True
    except Exception as e:
        log("    flux error:", str(e)[:120])
    try:
        r = requests.post(base + "@cf/bytedance/stable-diffusion-xl-lightning", headers=hdr,
                          json={"prompt": prompt[:1500], "width": 768, "height": 1024}, timeout=120)
        if r.ok and r.headers.get("content-type", "").startswith("image"):
            Path(path).write_bytes(r.content)
            return True
    except Exception as e:
        log("    sdxl error:", str(e)[:120])
    return False


def img_pixabay(query, path):
    if not PIXABAY_KEY:
        return False
    try:
        r = requests.get("https://pixabay.com/api/", params={
            "key": PIXABAY_KEY, "q": query[:90], "image_type": "illustration", "safesearch": "true", "per_page": 8}, timeout=30)
        hits = r.json().get("hits", [])
        if not hits:
            return False
        img = requests.get(random.choice(hits)["largeImageURL"], timeout=60)
        Path(path).write_bytes(img.content)
        return True
    except Exception:
        return False


def img_card(text, path, seed):
    rnd = random.Random(seed)
    c1 = tuple(rnd.randint(40, 200) for _ in range(3))
    c2 = tuple(rnd.randint(40, 200) for _ in range(3))
    im = Image.new("RGB", (1024, 1024), c1)
    px = ImageDraw.Draw(im)
    for y in range(1024):
        t = y / 1023
        px.line([(0, y), (1024, y)], fill=tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3)))
    im.save(path, quality=92)
    return True


def make_image(scene, cast, seed, path):
    chars = "; ".join(f"{k}: {cast[k]}" for k in scene.get("chars", []) if k in cast)
    prompt = f"{IMAGE_STYLE}. Scene: {scene['shot']}. " + (f"Characters: {chars}." if chars else "")
    if OFFLINE:
        return img_card(prompt, path, seed)
    for attempt in range(3):
        if img_cloudflare(prompt, seed + attempt, path):
            return True
        time.sleep(3)
    if img_pixabay(" ".join(scene["shot"].split()[:5]), path):
        return True
    log("    image fallback card used")
    return img_card(prompt, path, seed)


# --------------------------------------------------------------------------
# Montage
# --------------------------------------------------------------------------
SFX_SPECS = {
    "whoosh": ("anoisesrc=d=0.45:c=pink:a=0.5,highpass=f=400,lowpass=f=4000,afade=t=in:d=0.15,afade=t=out:st=0.2:d=0.25", 0.45),
    "pop": ("sine=f=700:d=0.12,afade=t=out:st=0.03:d=0.09", 0.12),
    "boom": ("sine=f=55:d=0.7,afade=t=out:st=0.05:d=0.65,volume=1.4,alimiter=limit=0.9", 0.7),
    "ding": ("sine=f=1760:d=0.6,afade=t=out:st=0.02:d=0.58", 0.6),
    "fail": ("aevalsrc='0.5*sin(2*PI*(330*t-90*t*t))*(1-t/1.1)':d=1.1:s=44100,vibrato=f=7:d=0.25", 1.1),
}
SFX_VOL = {"whoosh": 0.22, "pop": 0.45, "boom": 0.9, "ding": 0.35, "fail": 0.6}
END_SFX = ("boom", "ding", "fail")      # land after the line (the punchline), others at the start


def build_sfx(d):
    paths = {}
    for name, (spec, _) in SFX_SPECS.items():
        p = Path(d) / f"sfx_{name}.wav"
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", spec, "-ar", "44100", "-ac", "1", str(p)])
        paths[name] = p
    return paths


def word_times(line, dur):
    words = line.split()
    wts = [len(w) + 2 + (4 if re.search(r"[.!?]$", w) else 2 if re.search(r"[,;:]$", w) else 0) for w in words]
    total = sum(wts) or 1
    t, out = 0.0, []
    for w, wt in zip(words, wts):
        out.append((w, t, t + dur * wt / total))
        t += dur * wt / total
    return out


def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def make_ass(line, narr_dur, W, H, path, kind):
    short = kind == "short"
    fs, outline, chunk = (96, 7, 3) if short else (58, 4, 6)
    marginv = 520 if short else 70
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Liberation Sans,{fs},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{outline},3,2,60,60,{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    words = word_times(line.upper() if short else line, narr_dur)
    events = []
    for i, (w, a, b) in enumerate(words):
        c0 = (i // chunk) * chunk
        grp = words[c0:c0 + chunk]
        text = " ".join(("{\\c&H00D7FF&}" + g[0] + "{\\c&HFFFFFF&}") if c0 + j == i else g[0] for j, g in enumerate(grp))
        end = words[i + 1][1] if i + 1 < len(words) else narr_dur + 0.15
        events.append(f"Dialogue: 0,{ass_time(a)},{ass_time(end)},Cap,,0,0,0,,{text}")
    Path(path).write_text(head + "\n".join(events) + "\n", encoding="utf-8")


def compose_frame(src, dst, W, H, kind):
    """Blur-fit for shorts (any image shape), cover-crop for 16:9."""
    im = Image.open(src).convert("RGB")
    S = 1.35
    OW, OH = int(W * S), int(H * S)

    def cover(img, w, h):
        k = max(w / img.width, h / img.height)
        img = img.resize((math.ceil(img.width * k), math.ceil(img.height * k)), Image.LANCZOS)
        l, t = (img.width - w) // 2, (img.height - h) // 2
        return img.crop((l, t, l + w, t + h))

    if kind == "long":
        frame = cover(im, OW, OH)
    else:
        bg = cover(im, OW, OH).filter(ImageFilter.GaussianBlur(40))
        bg = Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.35)
        k = OW / im.width
        fg = im.resize((OW, int(im.height * k)), Image.LANCZOS)
        if fg.height > OH:
            fg = fg.crop((0, (fg.height - OH) // 2, OW, (fg.height - OH) // 2 + OH))
        bg.paste(fg, (0, (OH - fg.height) // 2))
        frame = bg
    frame.save(dst, quality=93)


def motion(i, F):
    m = i % 4
    cx, cy = "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"
    if m == 0:
        return f"1+0.12*on/{F}", cx, cy
    if m == 1:
        return "1.12", f"(iw-iw/zoom)*on/{F}", cy
    if m == 2:
        return f"1.12-0.12*on/{F}", cx, cy
    return "1.12", f"(iw-iw/zoom)*(1-on/{F})", cy


def render_video(script, kind, work, out_mp4):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    W, H = (1080, 1920) if kind == "short" else (1920, 1080)
    scenes, cast = script["scenes"], script.get("cast", {})
    sfx = build_sfx(work)
    seed0 = random.randint(1000, 10 ** 6)
    gap = 0.10 if kind == "short" else 0.28

    log(f"  images ({len(scenes)}) ...")
    img_paths = [work / f"img_{i:03}.jpg" for i in range(len(scenes))]
    with ThreadPoolExecutor(max_workers=1 if OFFLINE else 3) as ex:
        list(ex.map(lambda a: make_image(scenes[a], cast, seed0 + a * 7, img_paths[a]), range(len(scenes))))

    log("  voice + scene clips ...")
    vids, auds = [], []
    for i, sc in enumerate(scenes):
        narr = work / f"narr_{i:03}.wav"
        make_voice(sc["line"], sc["speaker"], sc["delivery"], narr)
        dn = probe_duration(narr)
        name = sc["sfx"] if sc["sfx"] != "none" else ("whoosh" if (kind == "short" and i > 0) else "none")
        end_extra = min(SFX_SPECS[name][1], 0.8) if name in END_SFX else 0.0
        total = dn + gap + sc["beat"] + end_extra
        frames = math.ceil(total * FPS)
        dur = frames / FPS

        # audio: narration + sfx, padded to the exact scene duration
        a_out = work / f"aud_{i:03}.wav"
        if name != "none":
            delay = int((dn + 0.05) * 1000) if name in END_SFX else 0
            fc = (f"[0:a]apad=whole_dur={dur:.3f}[n];[1:a]volume={SFX_VOL[name]},adelay={delay}|{delay},apad=whole_dur={dur:.3f}[s];"
                  f"[n][s]amix=inputs=2:duration=first:normalize=0,atrim=0:{dur:.3f}[a]")
            run(["ffmpeg", "-y", "-i", str(narr), "-i", str(sfx[name]), "-filter_complex", fc, "-map", "[a]",
                 "-ar", "44100", "-ac", "1", str(a_out)])
        else:
            run(["ffmpeg", "-y", "-i", str(narr), "-af", f"apad=whole_dur={dur:.3f},atrim=0:{dur:.3f}",
                 "-ar", "44100", "-ac", "1", str(a_out)])
        auds.append(a_out)

        # video: ken-burns on the composed frame + burned captions
        frame = work / f"frame_{i:03}.jpg"
        compose_frame(img_paths[i], frame, W, H, kind)
        ass = work / f"cap_{i:03}.ass"
        make_ass(sc["line"], dn, W, H, ass, kind)
        z, x, y = motion(i, frames)
        v_out = work / f"vid_{i:03}.mp4"
        vf = f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},subtitles={ass.name},format=yuv420p"
        run(["ffmpeg", "-y", "-i", frame.name, "-vf", vf, "-frames:v", str(frames), "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "21", "-an", v_out.name], cwd=work)
        vids.append(v_out)

    log("  final assembly ...")
    (work / "v.txt").write_text("".join(f"file '{p.name}'\n" for p in vids))
    (work / "a.txt").write_text("".join(f"file '{p.name}'\n" for p in auds))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "v.txt", "-c", "copy", "video_all.mp4"], cwd=work)
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "a.txt", "-c", "copy", "audio_all.wav"], cwd=work)

    music = sorted((ROOT / "music").glob("*.mp3")) if (ROOT / "music").exists() else []
    if music:
        fc = ("[0:a]aresample=44100[v];[1:a]volume=0.07,aresample=44100[m];"
              "[v][m]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-15:TP=-1.5:LRA=11[a]")
        extra = ["-stream_loop", "-1", "-i", str(random.choice(music))]
        run(["ffmpeg", "-y", "-i", "audio_all.wav", *extra, "-filter_complex", fc, "-map", "[a]", "audio_final.wav"], cwd=work)
    else:
        run(["ffmpeg", "-y", "-i", "audio_all.wav", "-af", "loudnorm=I=-15:TP=-1.5:LRA=11", "audio_final.wav"], cwd=work)
    run(["ffmpeg", "-y", "-i", "video_all.mp4", "-i", "audio_final.wav", "-c:v", "copy", "-c:a", "aac",
         "-b:a", "192k", "-ar", "44100", "-shortest", "-movflags", "+faststart", str(out_mp4)], cwd=work)
    total = probe_duration(out_mp4)
    log(f"  rendered {Path(out_mp4).name}: {total:.1f}s")
    return total


def make_thumbnail(work, title, out_jpg):
    try:
        im = Image.open(Path(work) / "frame_000.jpg").convert("RGB").resize((1280, 720), Image.LANCZOS)
        d = ImageDraw.Draw(im)
        font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 96)
        words = title.upper().split()[:5]
        text = "\n".join([" ".join(words[:3]), " ".join(words[3:])]).strip()
        d.multiline_text((60, 60), text, font=font, fill=(255, 235, 0), stroke_width=8, stroke_fill=(0, 0, 0), spacing=10)
        im.save(out_jpg, quality=90)
        return out_jpg
    except Exception as e:
        log("  thumbnail skipped:", e)
        return None


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------
def youtube_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(None, refresh_token=env("YT_REFRESH_TOKEN"), token_uri="https://oauth2.googleapis.com/token",
                        client_id=env("YT_CLIENT_ID"), client_secret=env("YT_CLIENT_SECRET"),
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def next_slot(hour_utc, offset_min=30):
    now = dt.datetime.now(dt.timezone.utc)
    t = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    while t < now + dt.timedelta(minutes=offset_min):
        t += dt.timedelta(days=1)
    return t


def upload(yt, mp4, meta, kind, slot_hour, thumb=None):
    from googleapiclient.http import MediaFileUpload
    title = meta["title"][:95]
    desc = meta.get("description", "").strip()
    tags = [t.lstrip("#") for t in meta.get("tags", [])][:15]
    if kind == "short":
        desc += "\n\n#shorts #funny #comedy"
        if "shorts" not in [t.lower() for t in tags]:
            tags.append("shorts")
    status = {"selfDeclaredMadeForKids": False, "privacyStatus": "public"}
    if PUBLISH_MODE == "private":
        status["privacyStatus"] = "private"
    elif PUBLISH_MODE == "schedule":
        status["privacyStatus"] = "private"
        status["publishAt"] = next_slot(slot_hour).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if env("YT_SYNTHETIC") == "1":
        status["containsSyntheticMedia"] = True
    body = {"snippet": {"title": title, "description": desc, "tags": tags, "categoryId": "23",
                        "defaultLanguage": "en", "defaultAudioLanguage": "en"}, "status": status}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(mp4), chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    log(f"  uploaded {vid} ({status.get('publishAt', status['privacyStatus'])})")
    if thumb:
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb))).execute()
        except Exception as e:
            log("  thumbnail not set:", str(e)[:120])
    return vid


# --------------------------------------------------------------------------
# Selftest (offline) and main
# --------------------------------------------------------------------------
def selftest(out_dir):
    global OFFLINE
    OFFLINE = True
    cast = {"A": "a smug cartoon raccoon in a yellow hoodie"}
    scn = [
        ("NARRATOR", "My raccoon has a Costco membership and I have questions.", "pop"),
        ("A", "Relax. I only buy the rotisserie chicken. And the entire pallet of toilet paper.", "none"),
        ("NARRATOR", "That is not what a normal person buys, Gary.", "boom"),
        ("A", "Normal people do not get free samples at 11 AM, Karen.", "fail"),
        ("NARRATOR", "My raccoon has a Costco membership and I have questions.", "ding"),
    ]
    sc = [{"speaker": s, "line": t, "delivery": "deadpan", "shot": t, "chars": ["A"], "sfx": f, "beat": 0.2}
          for s, t, f in scn]
    results = {}
    for kind in ("short", "long"):
        results[kind] = render_video({"scenes": sc, "cast": cast}, kind, out_dir / f"selftest_{kind}", out_dir / f"selftest_{kind}.mp4")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=["short", "long"])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "out"))
    args = ap.parse_args()
    out = Path(args.out) / dt.date.today().isoformat()
    out.mkdir(parents=True, exist_ok=True)

    if args.selftest:
        log(selftest(out))
        return 0

    can_upload = all(env(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")) and not args.dry_run
    yt = youtube_service() if can_upload else None
    if not can_upload:
        log("Upload disabled (dry run or missing YouTube secrets).")

    history = load_history()
    brief = trend_brief()
    failures = 0
    jobs = []
    if args.only != "long":
        for k, idea in enumerate(pick_ideas("short", brief, history, SHORTS_PER_DAY + 2)):
            jobs.append(("short", idea, SHORT_SLOTS_UTC[k % len(SHORT_SLOTS_UTC)]))
    if args.only != "short":
        for idea in pick_ideas("long", brief, history, LONGS_PER_DAY + 1):
            jobs.append(("long", idea, LONG_SLOT_UTC))

    made = {"short": 0, "long": 0}
    quota = {"short": SHORTS_PER_DAY if args.only != "long" else 0, "long": LONGS_PER_DAY if args.only != "short" else 0}
    for n, (kind, idea, slot) in enumerate(jobs):
        if made[kind] >= quota[kind]:
            continue
        log(f"\n=== {kind} #{made[kind] + 1}: {idea.get('title')} ===")
        if kind == "short":
            slot = SHORT_SLOTS_UTC[made["short"] % len(SHORT_SLOTS_UTC)]   # slots follow published count
        try:
            script = write_short(idea, brief) if kind == "short" else write_long(idea, brief)
            if script["score"] < PUBLISH_FLOOR:
                log(f"  skipped: judge score {script['score']} < {PUBLISH_FLOOR} (not funny enough)")
                continue
            work = out / f"{kind}_{n}"
            work.mkdir(parents=True, exist_ok=True)
            (work / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=1))
            mp4 = work / "final.mp4"
            secs = render_video(script, kind, work / "build", mp4)
            if kind == "long" and secs < 360:
                log(f"  long video only {secs:.0f}s (<6:00); not uploading")
                failures += 1
                continue
            vid = None
            if yt:
                thumb = make_thumbnail(work / "build", script["title"], work / "thumb.jpg") if kind == "long" else None
                vid = upload(yt, mp4, script, kind, slot, thumb)
            history.append({"date": dt.date.today().isoformat(), "kind": kind, "title": script["title"],
                            "premise": script.get("idea", ""), "score": script["score"], "video_id": vid})
            save_history(history)
            made[kind] += 1
        except Exception as e:
            failures += 1
            log(f"  FAILED: {type(e).__name__}: {str(e)[:400]}")
    log(f"\nDone. made={made} failures={failures}")
    return 1 if failures and not any(made.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
