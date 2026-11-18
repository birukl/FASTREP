import os

# Tesseract: one OpenMP thread is much faster on small images (set BEFORE any OCR call)
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

import asyncio
import io
import json
import re
import time
import logging

try:
    import winsound  # Windows beep (not on Linux/cloud)
except ImportError:
    winsound = None

try:  # optional: faster event loop on Linux  (pip install uvloop)
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

import pytesseract
from PIL import Image, ImageOps
from telethon import TelegramClient, events, errors, utils
from telethon.tl import types
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest, SendMessageRequest
from telethon.sessions import StringSession

logging.basicConfig(level=logging.WARNING)

# ---------- SETTINGS ----------
API_ID = int(os.environ.get("API_ID", 12345))   # from my.telegram.org
API_HASH = os.environ.get("API_HASH", "your_api_hash")
CHANNEL = os.environ.get("CHANNEL", "channel_username")   # channel to watch (without @)

DATA_FILE = "data.json"            # symbols with high/low
ALIASES_FILE = "aliases.json"      # {"gold": "XAUUSD", "us tech 100": "NAS100", ...}

CROP_FRAC = (20 / 720, 315 / 1280, 700 / 720, 410 / 1280)
LOOKUP_TIMEOUT = 0.5               # seconds before a slow discussion lookup is retried
SEND_TIMEOUT = 0.8                 # seconds before a slow send is re-sent (same random_id = no duplicate)
UPSCALE = 1
# None = download the biggest photo size (best OCR). -2 = next smaller size: smaller file, faster
# download, but TEST that OCR still reads every question correctly before using it.
DOWNLOAD_THUMB = None

ONE_SHOT = True                    # answer ONE question, then stop the program
READY_WORDS = ["ready"]            # a text message containing one of these = "get ready" -> warm up again
WARM_EVERY = 300                   # seconds: keep connections hot while waiting for the question

TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.name == "nt":
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
OCR_CONFIG = "--oem 1 --psm 6"
# ------------------------------


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f, parse_float=str)   # keep numbers exactly as written


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
# for the "glued words" fallback: precompute once instead of on every post
FLAT_NAMES = [
    (re.sub(r"[^A-Z0-9]", "", name), sym)
    for name, sym in sorted(NAMES.items(), key=lambda x: -len(x[0]))
    if len(re.sub(r"[^A-Z0-9]", "", name)) >= 5
]

answered = set()
busy = False   # a question is being processed right now
done = False   # the one question was answered


async def stop_soon():
    await asyncio.sleep(1.5)
    print("Answered. Stopping.")
    await client.disconnect()


async def keep_warm():
    """While waiting for the question: refresh the connections every few minutes."""
    while not done:
        await asyncio.sleep(WARM_EVERY)
        if not done:
            await warm_up()


def alert(text):
    print(f"[!] {text}")
    if winsound:
        winsound.Beep(1200, 400)
        winsound.Beep(900, 400)


def ocr_image(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    l, t, r, b = CROP_FRAC
    img = img.crop((int(l * w), int(t * h), int(r * w), int(b * h)))   # crop first, convert after
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
    for flat_name, symbol in FLAT_NAMES:
        if flat_name in flat and symbol in DATA:
            return symbol
    return None


def build_answer(symbol, text):
    item = DATA[symbol]
    t = text.upper()
    wants_high = "HIGH" in t
    wants_low = "LOW" in t
    if wants_high and not wants_low:
        return f"{item['high']}"
    if wants_low and not wants_high:
        return f"{item['low']}"
    return f"{item['high']}\n{item['low']}"


SESSION = os.environ.get("SESSION")
client = TelegramClient(StringSession(SESSION) if SESSION else "auto_answer_session", API_ID, API_HASH)

CHANNEL_INPUT = None   # resolved once at startup
CHANNEL_ID = None
thread_waiters = {}    # post id -> Future((group peer, message id in group))


def thread_future(post_id):
    fut = thread_waiters.get(post_id)
    if fut is None:
        fut = asyncio.get_running_loop().create_future()
        thread_waiters[post_id] = fut
    return fut


async def group_handler(event):
    """The channel post is auto-forwarded into the discussion group. When that copy arrives
    we already know the comment thread: no extra request to Telegram needed."""
    m = event.message
    fwd = m.fwd_from
    if not (fwd and fwd.channel_post):
        return
    if getattr(fwd.from_id, "channel_id", None) != CHANNEL_ID:
        return
    fut = thread_future(fwd.channel_post)
    if not fut.done():
        fut.set_result((GROUP_INPUT, m.id))


GROUP_INPUT = None   # set in main()


async def get_discussion_target(post_id, give_up=10.0):
    """Fallback: ask Telegram for the comment thread (retrying fast on slow/failed tries)."""
    start = time.perf_counter()
    while time.perf_counter() - start < give_up:
        try:
            r = await asyncio.wait_for(
                client(GetDiscussionMessageRequest(peer=CHANNEL_INPUT, msg_id=post_id)),
                timeout=LOOKUP_TIMEOUT,
            )
            m = r.messages[0]
            chat = next(c for c in r.chats if c.id == m.peer_id.channel_id)
            return utils.get_input_peer(chat), m.id
        except asyncio.TimeoutError:
            pass
        except errors.RPCError:
            await asyncio.sleep(0.05)
    return None


async def first_target(rpc_task, fut):
    """Whichever finds the comment thread first wins: the group update or the request."""
    pending = {rpc_task, fut}
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for d in done:
            if d.cancelled():
                continue
            result = d.result()
            if result:
                for p in pending:
                    p.cancel()
                return result
    return None


def reply_kwargs(reply_id):
    # Telethon versions differ in how a reply is passed to SendMessageRequest
    if hasattr(types, "InputReplyToMessage"):
        return {"reply_to": types.InputReplyToMessage(reply_to_msg_id=reply_id)}
    return {"reply_to_msg_id": reply_id}


async def send_fast(peer, text, reply_id, give_up=10.0):
    """Direct SendMessage with a FIXED random_id. If a try is slow (Telethon sleeps 2s on server
    errors) it is cancelled and re-sent at once; Telegram ignores duplicates of the same random_id."""
    random_id = int.from_bytes(os.urandom(8), "big", signed=True)
    req = SendMessageRequest(peer=peer, message=text, random_id=random_id,
                             no_webpage=True, **reply_kwargs(reply_id))
    start = time.perf_counter()
    while time.perf_counter() - start < give_up:
        try:
            return await asyncio.wait_for(client(req), timeout=SEND_TIMEOUT)
        except asyncio.TimeoutError:
            continue
    return None


@client.on(events.NewMessage(chats=CHANNEL))
async def handler(event):
    global busy, done
    msg = event.message
    if not msg.photo:
        if msg.text and not msg.document:
            low = msg.text.lower()
            if any(w in low for w in READY_WORDS):
                print(f"[READY] get-ready message seen: {msg.text[:60]!r} -> warming up")
                asyncio.create_task(warm_up())
            else:
                print("txt ignored")
        return
    if msg.id in answered or done or busy:
        return
    answered.add(msg.id)  # mark first so we never comment twice
    busy = True

    try:
        t0 = time.perf_counter()
        # find the comment thread NOW (two ways at once) while we download + OCR the image
        rpc_task = asyncio.create_task(get_discussion_target(msg.id))
        fut = thread_future(msg.id)

        image_bytes = await msg.download_media(bytes, thumb=DOWNLOAD_THUMB)
        t1 = time.perf_counter()

        text = await asyncio.to_thread(ocr_image, image_bytes)
        t2 = time.perf_counter()

        symbol = find_symbol(text)
        if not symbol:
            rpc_task.cancel()
            fut.cancel()
            thread_waiters.pop(msg.id, None)
            busy = False   # allow the next photo to be tried
            alert(f"Could not answer post {msg.id}. OCR read: {text.strip()!r}")
            return

        answer = build_answer(symbol, text)
        target = await first_target(rpc_task, fut)
        thread_waiters.pop(msg.id, None)
        t3 = time.perf_counter()
        sent = None
        if target:
            peer, reply_id = target
            sent = await send_fast(peer, answer, reply_id)
        if sent is None:   # fallback: the slow built-in way
            await client.send_message(CHANNEL, answer, comment_to=msg)
        t4 = time.perf_counter()

        if ONE_SHOT:
            done = True
            asyncio.create_task(stop_soon())
        print(f"{symbol} | download {t1-t0:.2f}s | OCR {t2-t1:.2f}s | wait-thread {t3-t2:.2f}s | send {t4-t3:.2f}s | total {t4-t0:.2f}s")
    except Exception as e:   # never stay locked after an error: allow the next photo to be tried
        busy = False
        print(f"[error] post {msg.id}: {type(e).__name__}: {e}")
        raise


async def warm_up():
    """Pay the one-time costs now, not on the first real post."""
    try:
        blank = Image.new("RGB", (720, 1280), "white")
        buf = io.BytesIO()
        blank.save(buf, "JPEG")
        await asyncio.to_thread(ocr_image, buf.getvalue())      # loads Tesseract + models into cache
    except Exception as e:
        print(f"OCR warm-up skipped: {e}")
    try:
        async for m in client.iter_messages(CHANNEL_INPUT, limit=30):
            if m.photo:
                await m.download_media(bytes)                   # opens the connection to the media DC
                break
    except Exception as e:
        print(f"Download warm-up skipped: {e}")


def set_high_priority():
    """Windows: give this process priority over other programs so nothing slows OCR/network."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080)
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)   # do not let the PC sleep while waiting
    except Exception:
        pass


async def main():
    print("v5")
    set_high_priority()
    await client.start()
    global CHANNEL_INPUT, CHANNEL_ID, GROUP_INPUT
    CHANNEL_INPUT = await client.get_input_entity(CHANNEL)
    entity = await client.get_entity(CHANNEL_INPUT)
    CHANNEL_ID = entity.id

    # discussion group of the channel -> listen to it for instant comment-thread ids
    try:
        full = await client(GetFullChannelRequest(CHANNEL_INPUT))
        gid = full.full_chat.linked_chat_id
        if gid:
            group = next(c for c in full.chats if c.id == gid)
            GROUP_INPUT = utils.get_input_peer(group)
            client.add_event_handler(group_handler, events.NewMessage(chats=GROUP_INPUT))
            print(f"Listening to discussion group: {getattr(group, 'title', gid)}")
    except Exception as e:
        print(f"Group listener off (using request lookup only): {e}")

    await warm_up()
    asyncio.create_task(keep_warm())

    name = getattr(entity, "title", None) or CHANNEL
    username = f" (@{entity.username})" if getattr(entity, "username", None) else ""
    print(f"Watching channel: {name}{username} ... (Ctrl+C to stop)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
