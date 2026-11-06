import asyncio
import io
import json
import re
import time
import logging
import os
from datetime import datetime, timezone

try:
    import winsound  # Windows beep (not on Linux/cloud)
except ImportError:
    winsound = None

import pytesseract
from PIL import Image, ImageOps
from telethon import TelegramClient, events, utils
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from telethon.sessions import StringSession

logging.basicConfig(level=logging.WARNING)

# ---------- SETTINGS ----------
API_ID = int(os.environ.get("API_ID", 12345))
API_HASH = os.environ.get("API_HASH", "your_api_hash")
CHANNEL = os.environ.get("CHANNEL", "channel_username")   # channel to watch (without @)

DATA_FILE = "data.json"
ALIASES_FILE = "aliases.json"

CROP_FRAC = (20 / 720, 315 / 1280, 700 / 720, 410 / 1280)
UPSCALE = 1

POLL_EVERY = 0.25      # start a new lookup request every 0.25s (old ones are NOT cancelled)
POLL_MAX_INFLIGHT = 6  # never more than this many lookups at once (avoids flood waits)
THREAD_TIMEOUT = 10.0  # give up waiting for the comment thread after this many seconds

TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.name == "nt":
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
OCR_CONFIG = "--oem 1 --psm 6"
# ------------------------------


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


raw = load_json(DATA_FILE)
if isinstance(raw, list):
    DATA = {str(item["symbol"]).upper(): item for item in raw}
else:
    DATA = {str(k).upper(): v for k, v in raw.items()}

ALIASES = {k.upper(): v.upper() for k, v in load_json(ALIASES_FILE).items()}

NAMES = {s: s for s in DATA}
NAMES.update(ALIASES)
PATTERNS = [
    (re.compile(r"(?<![A-Z0-9])" + re.escape(name) + r"(?![A-Z0-9])"), sym)
    for name, sym in sorted(NAMES.items(), key=lambda x: -len(x[0]))
]

answered = set()


def alert(text):
    print(f"[!] {text}")
    if winsound:
        winsound.Beep(1200, 400)
        winsound.Beep(900, 400)


def ocr_image(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    l, t, r, b = CROP_FRAC
    img = img.crop((int(l * w), int(t * h), int(r * w), int(b * h)))
    img = ImageOps.invert(ImageOps.grayscale(img))
    if UPSCALE != 1:
        img = img.resize((img.width * UPSCALE, img.height * UPSCALE))
    return pytesseract.image_to_string(img, config=OCR_CONFIG)


def find_symbol(text):
    text = re.sub(r"\s+", " ", text.upper())
    for pattern, symbol in PATTERNS:
        if pattern.search(text) and symbol in DATA:
            return symbol
    flat = re.sub(r"[^A-Z0-9]", "", text)
    for name, symbol in sorted(NAMES.items(), key=lambda x: -len(x[0])):
        flat_name = re.sub(r"[^A-Z0-9]", "", name)
        if len(flat_name) >= 5 and flat_name in flat and symbol in DATA:
            return symbol
    return None


def build_answer(symbol, text):
    item = DATA[symbol]
    t = text.upper()
    wants_high = "HIGH" in t
    wants_low = "LOW" in t
    if wants_high and not wants_low:
        return f"{symbol} high: {item['high']}"
    if wants_low and not wants_high:
        return f"{symbol} low: {item['low']}"
    return f"{symbol}\nHigh: {item['high']}\nLow: {item['low']}"


SESSION = os.environ.get("SESSION")
client = TelegramClient(StringSession(SESSION) if SESSION else "auto_answer_session", API_ID, API_HASH)

CHANNEL_INPUT = None     # input peer of the channel
CHANNEL_RAW_ID = None    # numeric id of the channel
GROUP_INPUT = None       # input peer of the linked discussion group

# post_id -> Future((peer, reply_id, how)). Whoever finds the comment thread first
# (the push update from the group, or one of the polling requests) fills it.
threads = {}


def thread_future(post_id):
    f = threads.get(post_id)
    if f is None:
        f = asyncio.get_running_loop().create_future()
        threads[post_id] = f
        if len(threads) > 200:                      # keep memory small
            for k in list(threads)[:100]:
                threads.pop(k, None)
    return f


def resolve_thread(post_id, peer, reply_id, how):
    f = thread_future(post_id)
    if not f.done():
        f.set_result((peer, reply_id, how))


async def poll_thread(post_id):
    """Ask Telegram for the comment thread every POLL_EVERY seconds WITHOUT cancelling
    earlier requests (cancelling made Telethon restart its 2s sleep on server errors)."""
    f = thread_future(post_id)
    start = time.perf_counter()
    tasks = []

    async def one():
        try:
            r = await client(GetDiscussionMessageRequest(peer=CHANNEL_INPUT, msg_id=post_id))
            m = r.messages[0]
            chat = next(c for c in r.chats if c.id == m.peer_id.channel_id)
            resolve_thread(post_id, utils.get_input_peer(chat), m.id, "poll")
        except Exception:
            pass   # thread not ready yet -> next request will try again

    while not f.done() and time.perf_counter() - start < THREAD_TIMEOUT:
        if sum(not t.done() for t in tasks) < POLL_MAX_INFLIGHT:
            tasks.append(asyncio.create_task(one()))
        await asyncio.sleep(POLL_EVERY)
    for t in tasks:
        t.cancel()


async def group_handler(event):
    """The moment Telegram copies the post into the discussion group, we get it as a
    normal update. That copy IS the comment thread root -> reply to it right away."""
    m = event.message
    fwd = m.fwd_from
    if fwd and fwd.channel_post and getattr(fwd.from_id, "channel_id", None) == CHANNEL_RAW_ID:
        resolve_thread(fwd.channel_post, GROUP_INPUT, m.id, "push")


async def handler(event):
    msg = event.message
    if not msg.photo or msg.id in answered:
        return
    answered.add(msg.id)

    t0 = time.perf_counter()
    lag = (datetime.now(timezone.utc) - msg.date).total_seconds()   # post -> we saw it
    poll_task = asyncio.create_task(poll_thread(msg.id))

    image_bytes = await msg.download_media(bytes)
    t1 = time.perf_counter()

    text = await asyncio.to_thread(ocr_image, image_bytes)
    t2 = time.perf_counter()

    symbol = find_symbol(text)
    if not symbol:
        poll_task.cancel()
        threads.pop(msg.id, None)
        alert(f"Could not answer post {msg.id}. OCR read: {text.strip()!r}")
        return

    answer = build_answer(symbol, text)
    try:
        peer, reply_id, how = await asyncio.wait_for(thread_future(msg.id), THREAD_TIMEOUT)
    except asyncio.TimeoutError:
        peer = None
        how = "fallback"
    poll_task.cancel()
    threads.pop(msg.id, None)
    t3 = time.perf_counter()

    if peer:
        await client.send_message(peer, answer, reply_to=reply_id)
    else:
        await client.send_message(CHANNEL, answer, comment_to=msg)
    t4 = time.perf_counter()

    print(f"{symbol} | seen {lag:.2f}s after post | download {t1-t0:.2f}s | OCR {t2-t1:.2f}s | "
          f"thread({how}) wait {t3-t2:.2f}s, ready {t3-t0:.2f}s after start | send {t4-t3:.2f}s | total {t4-t0:.2f}s")


async def main():
    global CHANNEL_INPUT, CHANNEL_RAW_ID, GROUP_INPUT
    await client.start()
    CHANNEL_INPUT = await client.get_input_entity(CHANNEL)
    CHANNEL_RAW_ID = CHANNEL_INPUT.channel_id

    full = await client(GetFullChannelRequest(CHANNEL_INPUT))
    linked_id = full.full_chat.linked_chat_id
    if not linked_id:
        raise SystemExit("This channel has no linked discussion group.")
    group = next(c for c in full.chats if c.id == linked_id)
    GROUP_INPUT = utils.get_input_peer(group)

    client.add_event_handler(handler, events.NewMessage(chats=CHANNEL_INPUT))
    client.add_event_handler(group_handler, events.NewMessage(chats=GROUP_INPUT))
    print(f"Watching channel + its discussion group ({getattr(group, 'title', linked_id)})... Ctrl+C to stop")
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
