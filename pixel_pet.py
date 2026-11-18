#!/usr/bin/env python3
"""Byte — a smug gray pixel cat who lives on the assistant's computer.

Stdlib only. Every command applies real-time decay from timestamps.
A light background ticker (no LLM) may also run every ~50 minutes.
Timezone: America/Chicago. Sleep window: 11:00 PM – 8:00 AM.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import signal
import subprocess
import sys
import time as time_mod
from contextlib import contextmanager
from datetime import datetime, timedelta, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

DIR = Path(__file__).resolve().parent
DEFAULT_STATE = DIR / "pet.json"
DEFAULT_LOG = DIR / "pet.log"
TZ = ZoneInfo("America/Chicago")
SLEEP_START = time(23, 0)
SLEEP_END = time(8, 0)

NAME = "Byte"
CARETAKER = "your assistant"

HUNGER_AWAKE = 3.0
HUNGER_SLEEP = 1.5
HAPPINESS_AWAKE = 1.5
HAPPINESS_SLEEP = 0.3
ENERGY_AWAKE = 2.0
ENERGY_SLEEP_GAIN = 8.0
HYGIENE_AWAKE = 1.5
HYGIENE_SLEEP = 0.75
HEALTH_CRITICAL_RATE = 2.0
CRITICAL_GRACE_HOURS = 8.0
MISTAKE_HOURS = 8.0
MISSED_CARE_HOURS = 72.0
BOND_NEGLECT_AFTER = 16.0
BOND_NEGLECT_RATE = 0.4
MEAL_MESS = 10
MEAL_HUNGER = 70
SNACK_HUNGER = 20
SNACK_HAPPINESS = 15
PLAY_HAPPINESS = 40
PLAY_ENERGY = 20
GROOM_HYGIENE = 75
REST_ENERGY = 45
HATCHLING_DECAY = 1.25
ELDER_DECAY = 0.85
REVIVAL_NEEDED = 2
EVENT_CHANCE = 0.30
EVENT_COOLDOWN = timedelta(hours=6)
DIARY_CAP = 200
LOG_CAP = 500
TICKER_LOG_CAP = 200
DEFAULT_TICK_SECONDS = 50 * 60
SLEEP_CHUNK_SECONDS = 60
MAX_CATCHUP_ROLLS = 12
HYGIENE_SAFE_FLOOR = 35
HAPPINESS_SAFE_FLOOR = 25
LONELY_AFTER_HOURS = 8.0

# Hairball is Byte's only "illness" (house rule).
# Occasional, comedic, never lowers health, never causes hibernation.
HAIRBALL_COOLDOWN_HOURS = 24.0
HAIRBALL_CLEAR_HOURS = 2.0
HAIRBALL_BASE_CHANCE_PER_HOUR = 0.004
HAIRBALL_HYGIENE_BONUS_PER_HOUR = 0.02
HAIRBALL_HYGIENE_SOFT = 40.0
HAIRBALL_HYGIENE_MESSY = 25.0
HAIRBALL_GROOM_LAPSE_HOURS = 16.0
HAIRBALL_GROOM_BONUS_PER_HOUR = 0.015
HAIRBALL_HAPPINESS_NICK = 6.0
HAIRBALL_HYGIENE_NICK = 5.0

# Litter box — smell is a 0-10 aroma score that falls off with distance.
# A well-kept box with a freshener is only noticeable up close.
LITTER_USAGE_MAX = 100.0
LITTER_MEAL_USAGE = 22.0
LITTER_SNACK_USAGE = 12.0
LITTER_IDLE_USAGE = 16.0
LITTER_IDLE_HOURS = 8.0
LITTER_FRESHENER_HOURS = 72.0
LITTER_GRUMPY_USAGE = 45.0
LITTER_OUTSIDE_USAGE = 85.0
LITTER_OUTSIDE_HAPPINESS = 8.0
LITTER_OUTSIDE_HYGIENE = 6.0
LITTER_HYGIENE_FLOOR = 40.0
LITTER_HAPPINESS_RATE = 0.6
LITTER_PESTER_USAGE = 55.0
LITTER_PESTER_CHANCE = 0.40
LITTER_PESTER_COOLDOWN_HOURS = 6.0
LITTER_SCOOP_HAPPINESS = 5.0
LITTER_CHANGE_HAPPINESS = 8.0
LITTER_DISTANCES = ("up close", "nearby", "across the room")
LITTER_DISTANCE_FACTOR = {
    "up close": 1.0,
    "nearby": 0.72,
    "across the room": 0.50,
}

# Furnishings (2026-11-17). Off by default until the owner opens the den.
LASER_HAPPINESS = 45
LASER_ENERGY = 22
LASER_PLAY_CHANCE = 0.25
TREE_CLIMB_HAPPINESS = 22
TREE_CLIMB_ENERGY = 12
TREE_NAP_ENERGY = 18
TREE_NAP_HAPPINESS = 10
LOUNGE_ENERGY = 22
LOUNGE_HAPPINESS = 12
PERCH_HAPPINESS = 14
PERCH_WATCH_ENERGY = 5
PERCH_LOAF_ENERGY = 10
HIDEOUT_HAPPINESS = 16
HIDEOUT_ENERGY = 8
HIDEOUT_HOURS = 4.0
FURNISHING_COUNT_KEYS = ("lasers", "perches", "trees", "lounges", "hideouts")

TRAITS = ("loyal", "snarky", "lazy", "curious", "proud", "gentle")

STAT_KEYS = ("hunger", "happiness", "energy", "hygiene", "health")


def default_now() -> datetime:
    env = os.environ.get("PET_NOW")
    if env:
        return parse_ts(env)
    return datetime.now(TZ)


def state_path() -> Path:
    return Path(os.environ.get("PET_STATE", DEFAULT_STATE))


def log_path() -> Path:
    return Path(os.environ.get("PET_LOG", DEFAULT_LOG))


def ticker_pid_path() -> Path:
    sp = state_path()
    return sp.with_name(sp.name + ".ticker.pid")


def ticker_log_path() -> Path:
    return state_path().with_name("ticker.log")


def tick_interval() -> float:
    raw = os.environ.get("PET_TICK_SECONDS")
    if raw:
        return max(1.0, float(raw))
    return float(DEFAULT_TICK_SECONDS)


def sleep_chunk() -> float:
    raw = os.environ.get("PET_SLEEP_CHUNK")
    if raw:
        return max(0.05, float(raw))
    return float(SLEEP_CHUNK_SECONDS)


def due_tick_times(
    last_tick: datetime,
    now: datetime,
    interval: float,
    cap: int = MAX_CATCHUP_ROLLS,
) -> tuple[list[datetime], datetime]:
    """Wall-clock intervals due after last_tick, up to `cap` most recent ones.

    Returns (due_times, new_last_tick). new_last_tick always jumps to the
    latest completed interval so a long freeze cannot leave a backlog.
    """
    if interval <= 0:
        interval = float(DEFAULT_TICK_SECONDS)
    if now <= last_tick:
        return [], last_tick
    n = int((now - last_tick).total_seconds() // interval)
    if n <= 0:
        return [], last_tick
    step = timedelta(seconds=interval)
    start_k = n - cap + 1 if n > cap else 1
    times = [last_tick + step * k for k in range(start_k, n + 1)]
    return times, last_tick + step * n


def sleep_until_next_tick(
    now_fn,
    last_tick: datetime,
    interval: float,
    chunk: float | None = None,
    sleep_fn=None,
) -> None:
    """Sleep in short chunks, re-checking the wall clock after each one."""
    if chunk is None:
        chunk = sleep_chunk()
    if sleep_fn is None:
        sleep_fn = time_mod.sleep
    if interval <= 0:
        interval = float(DEFAULT_TICK_SECONDS)
    while True:
        now = now_fn()
        remaining = interval - (now - last_tick).total_seconds()
        if remaining <= 0:
            return
        sleep_fn(min(chunk, remaining))


def parse_latest_ticker_log(path: Path | None = None) -> datetime | None:
    path = path or ticker_log_path()
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line or line.lower().startswith("ticker "):
            continue
        token = line.split()[0]
        try:
            return parse_ts(token)
        except (ValueError, OSError):
            continue
    return None


def infer_last_tick_at(
    state: dict[str, Any],
    now: datetime,
    log_file: Path | None = None,
) -> datetime:
    raw = state.get("last_tick_at")
    if raw:
        try:
            return parse_ts(raw)
        except (ValueError, TypeError):
            pass
    logged = parse_latest_ticker_log(log_file or ticker_log_path())
    if logged is not None:
        return logged
    for key in ("last_event_at",):
        if state.get(key):
            try:
                return parse_ts(state[key])
            except (ValueError, TypeError):
                pass
    diary = state.get("diary") or []
    if diary and diary[-1].get("at"):
        try:
            return parse_ts(diary[-1]["at"])
        except (ValueError, TypeError, KeyError):
            pass
    if state.get("last_updated"):
        try:
            return parse_ts(state["last_updated"])
        except (ValueError, TypeError):
            pass
    return now


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_ts(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def is_sleep_time(dt: datetime, start: time = SLEEP_START, end: time = SLEEP_END) -> bool:
    t = dt.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def next_boundary(dt: datetime, start: time = SLEEP_START, end: time = SLEEP_END) -> datetime:
    """Next sleep-window boundary strictly after dt."""
    d = dt.date()
    candidates = [
        datetime.combine(d, start, TZ),
        datetime.combine(d, end, TZ),
        datetime.combine(d + timedelta(days=1), start, TZ),
        datetime.combine(d + timedelta(days=1), end, TZ),
    ]
    later = [c for c in candidates if c > dt]
    return min(later)


def iter_slices(
    start: datetime,
    end: datetime,
    sleep_start: time = SLEEP_START,
    sleep_end: time = SLEEP_END,
):
    """Yield (slice_start, slice_end, sleeping) covering [start, end)."""
    if end <= start:
        return
    cursor = start
    while cursor < end:
        nxt = min(next_boundary(cursor, sleep_start, sleep_end), end)
        yield cursor, nxt, is_sleep_time(cursor, sleep_start, sleep_end)
        cursor = nxt


# ---------------------------------------------------------------------------
# ASCII art — a big smug gray cat made of glowing pixels
# ---------------------------------------------------------------------------

ART = {
    "egg": r"""
      .--.
     /    \
     \    /
      `--'
    """,
    "hatchling": r"""
     /\/\
    ( o.o)
     >^^<  *
    """,
    "hatchling_sleepy": r"""
     /\/\
    ( -.-)
     >^^<  z
    """,
    "hatchling_sad": r"""
     /\/\
    ( ;.;)
     >^^<
    """,
    "hatchling_hairball": r"""
     /\/\
    ( >.<)
     >^^<  *
       (o)
    """,
    "hatchling_hibernating": r"""
     /\/\
    ( -.-)  zzz
     >__<
    """,
    "juvenile": r"""
      /\_/\
     ( o.o )
     (  w  )  *
      V   V
    """,
    "juvenile_sleepy": r"""
      /\_/\
     ( -.- )
     (  w  ) z
      V   V
    """,
    "juvenile_sad": r"""
      /\_/\
     ( ;.; )
     (  w  )
      V   V
    """,
    "juvenile_hairball": r"""
      /\_/\
     ( >.< )
     (  w  ) *
       (o)
      V   V
    """,
    "juvenile_hibernating": r"""
      /\_/\
     ( -.- ) zzz
     (_____)
    """,
    "adult": r"""
       /\_/\
      ( o.o )  *
      (  -  )
     /      \
     \______/
    """,
    "adult_smug": r"""
       /\_/\
      ( -.o )  *
      (  -  )
     /      \
     \______/
    """,
    "adult_sleepy": r"""
       /\_/\
      ( -.- )
      (  -  ) z
     /      \
     \______/
    """,
    "adult_sad": r"""
       /\_/\
      ( ;.; )
      (  -  )
     /      \
     \______/
    """,
    "adult_hairball": r"""
       /\_/\
      ( >.< )  *
      (  -  )
     /      \
     \______/  (o)
    """,
    "adult_hibernating": r"""
       /\_/\
      ( -.- )  zzz
     /(_____)\
     \_______/
    """,
    "guardian": r"""
       /\_/\
      ( O.O )  *
      (  -  )
     /  vv  \
     \______/
    """,
    "magnificent": r"""
       /\_/\
      ( -.o )  *
      (  ~  )
     /      \
     \______/
    """,
    "loaf": r"""
       /\_/\
      ( -.- )
     (       )
     (  ___  )
      \_____/
    """,
    "elder": r"""
       /\_/\
      ( -.- )  *
      (  -  )
     /  ww  \
     \______/
    """,
    "elder_sleepy": r"""
       /\_/\
      ( -.- )
      (  -  ) z
     /  ww  \
     \______/
    """,
}


def _art_key(stage: str, form: str | None, mood: str) -> str:
    if mood == "hibernating":
        if stage in ("hatchling", "juvenile"):
            return f"{stage}_hibernating"
        return "adult_hibernating"
    if mood == "hairball":
        if stage in ("hatchling", "juvenile"):
            return f"{stage}_hairball"
        return "adult_hairball"
    if mood in ("sleepy", "loafing") and stage == "hatchling":
        return "hatchling_sleepy"
    if mood in ("sad", "hungry", "dirty", "grumpy") and stage == "hatchling":
        return "hatchling_sad"
    if stage == "hatchling":
        return "hatchling"
    if mood in ("sleepy", "loafing") and stage == "juvenile":
        return "juvenile_sleepy"
    if mood in ("sad", "hungry", "dirty", "grumpy") and stage == "juvenile":
        return "juvenile_sad"
    if stage == "juvenile":
        return "juvenile"
    if stage == "elder":
        if mood in ("sleepy", "loafing"):
            return "elder_sleepy"
        return "elder"
    if form == "loaf" and mood not in ("sad", "hungry", "hairball", "grumpy"):
        return "loaf"
    if form == "guardian" and mood not in ("sad", "hungry", "sleepy", "grumpy"):
        return "guardian"
    if form == "magnificent" and mood not in ("sad", "hungry", "sleepy", "hairball", "grumpy"):
        return "magnificent"
    if mood == "smug":
        return "adult_smug"
    if mood in ("sleepy", "loafing"):
        return "adult_sleepy"
    if mood in ("sad", "hungry", "dirty", "grumpy"):
        return "adult_sad"
    return "adult"


def ascii_art(stage: str, form: str | None, mood: str) -> str:
    key = _art_key(stage, form, mood)
    raw = ART.get(key) or ART["adult"]
    lines = [ln.rstrip() for ln in raw.strip("\n").splitlines()]
    # trim common leading indent
    indents = [len(ln) - len(ln.lstrip(" ")) for ln in lines if ln.strip()]
    pad = min(indents) if indents else 0
    return "\n".join(ln[pad:] if len(ln) >= pad else ln for ln in lines)


# ---------------------------------------------------------------------------
# Voice — playful, smug, affectionate
# ---------------------------------------------------------------------------

QUIPS = {
    "smug": [
        "I suppose this will do. Don't get used to the praise.",
        "Yes, I am magnificent. Try to keep up.",
        "You may admire me. Brief admiration is acceptable.",
    ],
    "content": [
        "Not bad. I could get used to this arrangement.",
        "I'm comfortable. That's your cue to keep it that way.",
        "All is as it should be. Namely: I am here.",
    ],
    "happy": [
        "That's more like it. You may continue.",
        "I am in a generous mood. Don't waste it.",
        "Good. I knew you had it in you.",
    ],
    "hungry": [
        "I have been patient. That patience has expired.",
        "The food situation is becoming a personal insult.",
        "Feed me. I will consider forgiving you.",
    ],
    "sad": [
        "I have not forgotten you. I have simply noticed your absence.",
        "A little company would not kill you, you know.",
        "I sat here. You were elsewhere. We can fix this.",
    ],
    "sleepy": [
        "I am a loaf now. Do not disturb the loaf.",
        "Speak softly. Some of us are conserving greatness.",
        "If this is a crisis, it can wait until after my nap.",
    ],
    "loafing": [
        "Behold the loaf. This is peak performance.",
        "I have achieved the shape of success.",
        "Wake me only if the snacks are real.",
    ],
    "hairball": [
        "That was entirely intentional. A tactical hairball. You're welcome.",
        "*hack* *hack* ... I have no idea what you're talking about.",
        "A brief operational disturbance. I am, as ever, magnificent. Do not stare at it.",
    ],
    "dirty": [
        "I appear to have... pixels out of place. Groom me.",
        "This is beneath my dignity. A brushing, if you please.",
        "I stepped in something. We will not discuss what.",
    ],
    "hibernating": [
        "Dimmed, not gone. Come back and try harder.",
        "I am conserving myself. You know what you have to do.",
        "A long nap. Wake me properly this time.",
    ],
    "needy": [
        "I would like attention. I will not beg. I am asking.",
        "Several of my needs are having a meeting without you.",
        "This is your reminder that I exist, and I have standards.",
    ],
    "grumpy": [
        "The box situation is unacceptable. I have standards.",
        "I have been polite. The litter box is no longer polite back.",
        "Fix the box. I will be in a better mood after, possibly.",
    ],
}

ACTION_LINES = {
    "feed_meal": [
        "Devoured the meal as if he'd invented dinner.",
        "Ate with the dignity of a king and the speed of a raccoon.",
        "Accepted the meal. A slow blink means you're welcome.",
    ],
    "feed_snack": [
        "A snack. Acceptable. A second snack would be more acceptable.",
        "Crunched it, judged it, and filed it under 'adequate tribute.'",
        "Snack acquired. Mood: slightly more generous.",
    ],
    "overfed": [
        "He ate it anyway, then gave you a look of profound betrayal.",
        "Too full, but pride would not allow a leftover.",
        "That was one bite too many. He will complain later.",
    ],
    "play": [
        "Stalked a cursor, pounced, and pretended it was always the plan.",
        "A brief hunt. He won, of course.",
        "Played, then sat on the victory like a throne.",
    ],
    "play_refuse": [
        "He opened one eye. That was a no.",
        "Too tired to perform. Try a nap, then a hunt.",
        "Magnificent cats do not sprint on an empty battery.",
    ],
    "groom": [
        "Purrs in a straight line while the pixels get tidied.",
        "Submitted to grooming with the air of a celebrity.",
        "Much better. The glow is back to regulation smugness.",
    ],
    "rest": [
        "Folded himself into a loaf and clocked out.",
        "A tactical nap. Do not mistake it for laziness. It is strategy.",
        "Already halfway to a dream about temple dogs and tuna.",
    ],
    "heal": [
        "Accepted a little fussing, then immediately looked judgmental again.",
        "The glow steadies. He will allow you to feel useful.",
        "Soothing accepted. He expects a snack for his trouble.",
    ],
    "hairball_event": [
        "He hacked up a hairball with great ceremony, then sat down as if nothing had occurred.",
        "*hack* *hack* A tactical hairball. Entirely intentional. You're welcome.",
        "A brief operational disturbance. Do not stare at it. He is, as ever, magnificent.",
    ],
    "hairball_clear": [
        "The hairball is already ancient history, according to him.",
        "He licked a paw, looked unimpressed, and moved on.",
        "Acted as if nothing happened. The evidence on the floor disagrees, quietly.",
    ],
    "hairball_groom": [
        "Groomed the drama out of him. He pretends the hairball was a design choice.",
        "A brushing, and the incident is officially beneath discussion.",
        "Pixels tidied, hairball retired. He is smug again.",
    ],
    "hairball_heal": [
        "There is no medicine for this, only a dramatic performance and a smug recovery.",
        "You fussed. He hacked, glared, and declared the matter closed.",
        "Sat with him through the production. He is already pretending it never happened.",
    ],
    "heal_fine": [
        "He is fine, and slightly offended you suggested otherwise.",
        "No medicine required. Your concern has been noted, though.",
        "Sound as a pixel. Save the fussing for a real emergency.",
    ],
    "talk": [
        "Listened, blinked slowly, and bumped his head against the void.",
        "He heard you. The slow blink is the official reply.",
        "A brief audience granted. You used it well.",
    ],
    "hatch": [
        "A pile of glowing gray pixels stretched, yawned, and became a cat.",
        "He arrived already unimpressed, which means it's going well.",
        "Byte is here. He will permit you to take care of him.",
    ],
    "hibernate": [
        "The glow dimmed. Byte curled up to wait — not gone, just resting.",
        "He tucked himself away. Come back with real care, not excuses.",
        "Hibernation. A scar on the record, but the story isn't over.",
    ],
    "revive_partial": [
        "A single ear twitched. He's listening. Keep going.",
        "The pixels flickered. One more push should do it.",
        "He stirred, then settled. Almost. Don't stop now.",
    ],
    "revive": [
        "The glow came back, a little faint, and very unimpressed.",
        "He's up. Grumpy, hungry, and willing to renegotiate terms.",
        "Byte returned from the long nap. Try not to repeat the prelude.",
    ],
    "evolve_juvenile": [
        "Longer legs, a bigger smirk. Juvenile Byte has entered the chat.",
        "He grew. The attitude grew faster.",
    ],
    "evolve_adult": [
        "Full-sized, fully smug, and officially someone's problem. Yours.",
        "Adulthood looks good on him. He agrees.",
    ],
    "evolve_elder": [
        "A little slower, a little wiser, still in charge.",
        "Elder Byte has opinions now. He also had them before.",
    ],
    "scoop": [
        "Scooped the box. He inspected the work and sat down like a supervisor.",
        "The box is presentable again. He pretended not to watch.",
        "Litter restored to regulation neatness. He may forgive you.",
    ],
    "scoop_clean": [
        "The box was already fine. He still supervised the scoop, just in case.",
        "Nothing to scoop, but he appreciated the ceremony.",
        "Already clean. He blinked once, which is official approval.",
    ],
    "relitter": [
        "Full litter change. He stepped in, approved the footing, and declared it adequate.",
        "Fresh litter, clean box. He is already tracking pixels into it, of course.",
        "The old litter is gone. A new era of dignity begins, until later today.",
    ],
    "freshen": [
        "Clicked a freshener into place. The air near the box is almost polite again.",
        "Air freshener refreshed. Up close you might still notice; across the room, no.",
        "A new freshener. The box now only admits to existing if you lean in.",
    ],
    "pester": [
        "Stood by the litter box and meowed like this was a formal complaint.",
        "Pawed at your ankle, then trotted back to the box. He is not subtle.",
        "Parked himself next to the box and stared. The message was the box.",
        "A single, official meow. Then another. The litter box is the topic.",
    ],
    "outside_box": [
        "He considered the box, found it beneath him, and left a pointed protest nearby.",
        "The box was too far gone, so he made other arrangements. A gentle hint.",
        "He declined the box and made his feelings known just beside it.",
    ],
    "laser": [
        "Chased the red dot with his whole soul and caught exactly none of it. He is choosing to blame physics.",
        "The laser cheated. He has notes. He almost had it. He did not have it.",
        "Pounced, missed, pounced again, and formally declared the dot a coward.",
        "A magnificent hunt. Zero catches, which he would like recorded as a technicality.",
    ],
    "perch": [
        "Took the window perch and settled in like a lighthouse with opinions.",
        "Claimed the perch and began a formal review of whatever the window felt like showing.",
        "Loafed on the window perch. The view is his now. He may comment.",
    ],
    "tree_climb": [
        "Climbed the cat tree, claimed the top, and surveyed his domain. A pixel was knocked off as tribute.",
        "Took the high platform of the cat tree and sat like a small, smug gargoyle.",
        "Scrambled up the cat tree and declared the lower levels optional.",
    ],
    "tree_nap": [
        "Loafed on the middle platform of the cat tree and called it a strategic rest.",
        "Curled up in the cat tree as if meetings would now come to him.",
        "A nap in the cat tree. Officially not laziness. Officially a lookout.",
    ],
    "lounge": [
        "Collapsed into the lounge spot and became, for official purposes, a cushion with opinions.",
        "Claimed the lounge and achieved a loaf of record dignity.",
        "The lounge accepted him. He accepted the lounge. A treaty was signed in fur.",
    ],
    "hideout": [
        "Vanished into the beer-can hideout. From inside: a muffled, official 'I do too fit.'",
        "Crawled into the beer-can hideout and closed negotiations with the rest of the room.",
        "The beer-can hideout accepted him, which physics has been asked not to comment on. Skippy would understand.",
    ],
    "den_closed": [
        "The new den is designed but not open yet. Nothing changes for Byte until the owner gives the go-ahead.",
        "Those toys are waiting behind the door. The den stays dark until it is turned on.",
        "Noted, and filed under 'not yet.' The den will not change his day until it is opened.",
    ],
    "den_enable": [
        "The den is open. Window perch, laser, cat tree, lounge, and the beer-can hideout are now part of the house.",
        "Furnishings are live. He has already claimed something, in principle.",
    ],
    "den_already": [
        "The den is already open. He checked.",
        "Already open. He saw no reason to celebrate twice.",
    ],
    "den_disable": [
        "The den is closed again. The furniture stays put; it just will not change his day until it is reopened.",
        "Den closed. His old routine is back on the clock.",
    ],
}

EVENTS = [
    ("cpu_warm", "Claimed the warmest corner of the CPU and is not sharing.", {"happiness": 5, "energy": 5}),
    ("cursor", "Spent a while stalking a cursor that wasn't even on this screen.", {"happiness": 8, "energy": -5}),
    ("snack", "Intercepted a packet and is calling it a snack.", {"hunger": 10}),
    ("syslog", "Left a snarky comment in the system log. It was not helpful.", {"happiness": 4}),
    ("temple", "Dreamed of a great temple dog and woke up slightly braver.", {"bond": 2, "happiness": 5}),
    ("beercan", "Attempted to hide in a beer can. He does not fit. He will not discuss it.", {"happiness": 6, "energy": -3}),
    ("pixels", "Shed a few glowing pixels on the desk. Fashion, apparently.", {"hygiene": -8}),
    ("window", "Stared out of a terminal window as if it were a real one.", {"happiness": 3}),
    ("perfect_loaf", "Achieved a theoretically perfect loaf and expects applause.", {"energy": 5, "happiness": 8}),
    ("keyboard", "Walked across the keyboard and accomplished nothing on purpose.", {"happiness": 5}),
    ("mouse", "Sat very still, as if practicing to be a temple dog. Lasted twelve seconds.", {"bond": 1, "happiness": 4}),
]

# Offline ticker events: (id, text, deltas, tags)
# tags: periods, moods, traits, lonely
DIARY_EVENTS = [
    ("stretch", "Stretched long enough to look like a question mark, then pretended it was graceful.", {"energy": 3}, {"periods": ("morning", "afternoon")}),
    ("sunbeam", "Claimed a warm patch near the CPU and paid taxes to nobody.", {"happiness": 5, "energy": 3}, {"periods": ("morning", "afternoon")}),
    ("loaf", "Assembled himself into a loaf. Inspected the loaf. Approved the loaf.", {"energy": 4, "happiness": 4}, {"periods": ("afternoon", "evening", "night"), "moods": ("loafing", "sleepy", "content", "smug")}),
    ("groom_self", "Gave his chest a few official licks. The pixels cooperated, mostly.", {"hygiene": 4}, {"periods": ("morning", "afternoon"), "moods": ("dirty", "content", "smug")}),
    ("box", "Sat in a square of nothing and declared it a box. It was a very good box.", {"happiness": 5}, {"periods": ("afternoon", "evening")}),
    ("knock", "Knocked a leftover pixel off the edge of the world and did not look down.", {"happiness": 3}, {"periods": ("afternoon", "evening"), "traits": ("snarky", "curious")}),
    ("moth", "Plotted a campaign against a moth that may or may not exist.", {"energy": -4, "happiness": 6}, {"periods": ("evening", "night"), "traits": ("snarky", "curious")}),
    ("windowsill", "Stared out a terminal window with the gravity of a lighthouse.", {"happiness": 3}, {"periods": ("morning", "afternoon", "evening")}),
    ("keyboard", "Walked across the keyboard and invented a new typo, on purpose.", {"happiness": 4}, {"periods": ("afternoon", "evening"), "traits": ("snarky",)}),
    ("mouse_practice", "Sat very still, practicing to be Mouse. Lasted eleven seconds. Dignity recovered in twelve.", {"bond": 1, "happiness": 4}, {"periods": ("afternoon", "evening")}),
    ("mouse_dream", "Dreamed a temple dog was keeping watch. Woke up, checked the perimeter, and went back to loafing.", {"happiness": 5, "bond": 1}, {"periods": ("night",)}),
    ("harry", "Looked around for a wizard in a leather duster. Found none. Judged the vacancy.", {"happiness": 3}, {"periods": ("evening", "night"), "traits": ("proud", "snarky")}),
    ("beer_can", "Investigated a theoretical beer can. Still does not fit. Still will not discuss it.", {"happiness": 4}, {"periods": ("evening", "night"), "traits": ("snarky",)}),
    ("pizza", "Guarded an imaginary pizza crust with his whole soul.", {"hunger": 4, "happiness": 3}, {"periods": ("evening",), "moods": ("hungry", "needy")}),
    ("hexen", "Hissed at a draft as if it owed him rent in a wizard war.", {"happiness": 2}, {"periods": ("night", "evening"), "traits": ("proud",)}),
    ("throne", "Sat on a status report and called it a throne. The report did not object.", {"happiness": 5}, {"traits": ("proud", "snarky"), "moods": ("smug", "happy", "content")}),
    ("compliment", "Practiced looking unimpressed in case anyone came back with praise.", {"happiness": 3}, {"traits": ("proud", "snarky")}),
    ("mirror", "Caught his reflection in a dark terminal. Nodded once, as if to an equal.", {"happiness": 4}, {"traits": ("proud",), "periods": ("morning", "afternoon")}),
    ("monologue", "Left a muttered review of today's service. Stars: pending.", {"happiness": 3}, {"traits": ("snarky",)}),
    ("door", "Stared at the door for a while. Definitely not waiting for anyone.", {"happiness": 2}, {"lonely": True}),
    ("not_lonely", "Was not lonely. Wrote that down twice, just to be thorough.", {"happiness": 2}, {"lonely": True, "traits": ("proud", "snarky")}),
    ("window_wait", "Watched the clock change and pretended it was a bird.", {"happiness": 2}, {"lonely": True, "periods": ("afternoon", "evening")}),
    ("left_note", "Left a note: 'I existed the whole time. Try to keep up.'", {"happiness": 3}, {"lonely": True, "traits": ("snarky", "proud")}),
    ("curl", "Curled up in the exact spot someone usually sits. For the warmth. Obviously.", {"energy": 3, "happiness": 2}, {"lonely": True, "periods": ("evening", "night")}),
    ("night_loaf", "Slept in a tight comma and snored one dignified snore.", {"energy": 5}, {"periods": ("night",)}),
    ("night_patrol", "Did a midnight patrol of the box. Nothing was amiss. He was the amiss.", {"energy": -3, "happiness": 3}, {"periods": ("night",), "traits": ("proud",)}),
    ("snack_hunt", "Hunted a crumb of leftover data. Declared it an appetizer.", {"hunger": 6}, {"moods": ("hungry", "needy"), "periods": ("morning", "evening")}),
    ("bowl_stare", "Sat beside the food dish like a gargoyle. Subtlety is for dogs.", {"happiness": 1}, {"moods": ("hungry", "needy")}),
]

# Window views: (id, status label, weight, periods or None, diary lines, deltas)
# Mundane views are common; Upload-style surprises are rarer and can appear any time.
WINDOW_VIEWS = [
    ("squirrels", "a pair of squirrels staging a heist", 3.0, ("morning", "afternoon"), [
        "Heckled a squirrel from the perch until it dropped its acorn. Considered that a diplomatic victory.",
        "Narrated a squirrel's incompetence in a voice he believes is helpful.",
        "Told a squirrel, through glass, that its form was sloppy and its tail lacked conviction.",
    ], {"happiness": 5, "energy": -3}),
    ("birds", "birds who are, in his opinion, showing off", 3.0, ("morning", "afternoon"), [
        "Judged a bird's landing. Marks were given. Marks were low.",
        "Chattered at a bird as if filing a noise complaint in the correct official language.",
        "Tracked a bird with his whole head. The glass remained undefeated.",
    ], {"happiness": 5, "energy": -3}),
    ("cars", "cars going by, which he has decided are prey", 2.0, ("morning", "afternoon", "evening"), [
        "Decided the cars were prey, then revised that to 'loud furniture.'",
        "Tail-twitched at every passing car like a general reviewing a parade of very bad mice.",
        "Announced that a truck was too big and should go around.",
    ], {"happiness": 4, "energy": -2}),
    ("wind", "nothing but wind, which he is still judging", 2.0, ("morning", "afternoon", "evening"), [
        "Stared at the wind for a long time and then blamed it for nothing in particular.",
        "The view was only wind. He still won the staring contest.",
        "Watched leaves argue with the wind and declined to take sides.",
    ], {"happiness": 3, "energy": 2}),
    ("butterflies", "butterflies, which are cheating by flying", 2.0, ("morning", "afternoon"), [
        "Accused a butterfly of cheating by being both pretty and airborne.",
        "Swiped at a butterfly through the glass, then pretended he was waving.",
        "A butterfly did a loop. He was not impressed. He was a little impressed.",
    ], {"happiness": 5, "energy": -2}),
    ("rival_cat", "a rival cat on the other side of the glass", 2.2, ("morning", "afternoon", "evening"), [
        "Held a full diplomatic stare-down with a rival cat through the glass. Neither blinked. Both won, according to him.",
        "Pressed his face to the glass at a rival cat and mouthed something unkind.",
        "Spotted a rival cat and flopped to look unbothered, which fooled nobody.",
    ], {"happiness": 6, "energy": -4}),
    ("moths", "a moth against the night glass", 2.4, ("evening", "night"), [
        "Supervised a moth against the night glass. The moth was not promoted.",
        "Plotted a campaign against a moth on the other side of the window. The glass, once again, was the problem.",
        "Watched a moth do laps and gave it a very official slow blink.",
    ], {"happiness": 5, "energy": -2}),
    ("moonlight", "moonlight, accepted as tribute", 2.2, ("night",), [
        "Bathed in moonlight on the perch and accepted it as tribute.",
        "Watched the moon as if it might do a trick. It did not. He stayed anyway.",
        "The window held nothing but moonlight. He loafed in it like a small gray planet.",
    ], {"happiness": 4, "energy": 4}),
    ("empty_street", "an empty street he has already scared off", 1.6, ("evening", "night"), [
        "Surveyed an empty street and concluded he had already scared everything off.",
        "The window showed nothing moving. He guarded that nothing with great seriousness.",
        "Kept watch over a quiet street. Security was, in his view, excellent.",
    ], {"happiness": 3, "energy": 2}),
    ("city", "a city that was not there yesterday", 0.7, None, [
        "The window opened onto a city that was not there yesterday. He chose a building to dislike.",
        "An unfamiliar city filled the perch window. He picked a rooftop, in case he ever needs one.",
        "Watched a city that does not belong to this street and rated the traffic.",
    ], {"happiness": 6, "energy": -2}),
    ("ocean", "an ocean, somehow", 0.6, None, [
        "An ocean appeared in the window. He tapped the glass once, in case it was a very large water dish.",
        "The perch looked out on an ocean. He declined to swim and accepted the view as decoration.",
        "Waves rolled past a window that should not have waves. He supervised anyway.",
    ], {"happiness": 5, "energy": 2}),
    ("desert", "a desert highway and no lizards, yet", 0.5, None, [
        "The perch looked out on a desert highway. He waited for a lizard. He is a patient cat.",
        "A desert showed up in the window. He squinted, loafed, and filed it under 'warm.'",
        "Watched heat shimmer over a road that is not this road. Approved of the quiet.",
    ], {"happiness": 4, "energy": 3}),
    ("stars", "a sky full of stars that do not match this hour", 0.6, None, [
        "The window showed a sky full of stars that do not match this hour. He accepted the upgrade.",
        "Counted stars that should not be out yet. Lost count. Pretended that was the plan.",
        "A wrong-hour starfield filled the glass. He sat like this was a private screening.",
    ], {"happiness": 5, "energy": 2}),
    ("parade", "a parade of unknown origin", 0.5, None, [
        "A parade of unknown origin went by the window. He did not wave. He did inspect.",
        "The window put on a parade. He judged the floats and found them lacking in mice.",
        "Watched a parade that had no business being there and declined to join it.",
    ], {"happiness": 6, "energy": -2}),
    ("moot", "the Moot coffee pot, always full", 0.45, None, [
        "The window briefly showed the Moot's coffee pot, always full. He approved of the management.",
        "A familiar coffee pot appeared in the glass. He did not drink coffee. He still supervised.",
        "The perch window opened on the Moot for a moment. He looked for snacks, then for dignity.",
    ], {"happiness": 6}),
    ("spaceship", "a spaceship parked badly", 0.45, None, [
        "A spaceship parked badly in the window. He gave it a look that has ended lesser careers.",
        "The window showed a spaceship. He sniffed, found no mice, and marked it as furniture.",
        "Watched a spaceship drift past the perch and refused to be impressed on the first try.",
    ], {"happiness": 6, "energy": -3}),
    ("forest", "a forest that is not on this street", 0.5, None, [
        "A forest that was not on this street filled the window. He chattered at something in the ferns.",
        "The perch looked into a woods that should not be there. He chose a branch, theoretically.",
        "Leaves that do not belong to this yard brushed the glass. He patted the window, once.",
    ], {"happiness": 5, "energy": -2}),
]

# Offline ticker events used only when furnishings.enabled is true.
FURNISHING_DIARY_EVENTS = [
    ("perch_view", "Took the window perch and conducted a formal review of the outside.", {"happiness": 5}, {"requires_furnishings": True, "periods": ("morning", "afternoon", "evening")}),
    ("tree_throne", "Claimed the top of the cat tree and declared it a throne. The lower platforms did not object.", {"happiness": 5, "energy": -3}, {"requires_furnishings": True, "traits": ("proud", "curious")}),
    ("tree_nap", "Napped on the middle platform of the cat tree and called it a strategic rest.", {"energy": 5, "happiness": 3}, {"requires_furnishings": True, "moods": ("sleepy", "loafing", "content", "smug")}),
    ("lounge_loaf", "Collapsed into the lounge spot and became, for official purposes, a cushion with opinions.", {"energy": 6, "happiness": 4}, {"requires_furnishings": True, "moods": ("loafing", "sleepy", "content", "smug", "happy")}),
    ("hideout_can", "Vanished into the beer-can hideout. From inside: a muffled, official 'I do too fit.'", {"happiness": 6, "energy": 3}, {"requires_furnishings": True, "traits": ("snarky", "curious"), "periods": ("afternoon", "evening", "night")}),
    ("hideout_skippy", "Held a private briefing inside the beer-can hideout. Skippy would understand.", {"happiness": 5}, {"requires_furnishings": True, "periods": ("evening", "night"), "traits": ("snarky", "proud")}),
    ("laser_phantom", "Swatted at a leftover red dot that was not there. Still did not catch it.", {"happiness": 4, "energy": -4}, {"requires_furnishings": True, "traits": ("curious", "snarky")}),
    ("rival_stare", "Held a full diplomatic stare-down with a rival cat through the glass. Neither blinked. Both won, according to him.", {"happiness": 6, "energy": -4}, {"requires_furnishings": True, "periods": ("morning", "afternoon", "evening")}),
]


def pick_line(rng: random.Random, key: str) -> str:
    lines = ACTION_LINES.get(key) or QUIPS.get(key) or ["Byte made a noise that might have been approval."]
    return rng.choice(lines)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def clamp(n: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, n))


def default_litter(now: datetime) -> dict[str, Any]:
    stamp = fmt_ts(now)
    return {
        "usage": 0.0,
        "last_scooped_at": stamp,
        "last_changed_at": stamp,
        "freshener": True,
        "freshener_at": stamp,
        "idle_hours": 0.0,
        "went_outside": False,
        "pestering": False,
        "pester_at": None,
        "pester_text": None,
        "last_used_at": None,
    }


def default_furnishings() -> dict[str, Any]:
    """Den inventory. enabled is False until the owner opens it."""
    return {
        "enabled": False,
        "window_view": None,
        "window_view_at": None,
        "last_laser_at": None,
        "laser_misses": 0,
        "last_tree_at": None,
        "last_lounge_at": None,
        "last_hideout_at": None,
        "last_perch_at": None,
        "in_hideout": False,
    }


def furnishings_enabled(state: dict[str, Any] | None) -> bool:
    den = (state or {}).get("furnishings")
    return isinstance(den, dict) and bool(den.get("enabled"))


def migrate_state(state: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Add missing fields (litter box, hairball, furnishings, counters) without dropping existing state."""
    if not state:
        return state
    stamp_src = state.get("last_updated") or state.get("last_care_at") or state.get("born_at")
    when = now
    if when is None:
        if stamp_src:
            try:
                when = parse_ts(stamp_src)
            except (ValueError, TypeError):
                when = default_now()
        else:
            when = default_now()
    box = state.get("litter")
    if not isinstance(box, dict):
        state["litter"] = default_litter(when)
    else:
        for key, value in default_litter(when).items():
            if key not in box:
                box[key] = value
    counts = state.setdefault("counts", {})
    for key in ("scoops", "changes", "freshens") + FURNISHING_COUNT_KEYS:
        counts.setdefault(key, 0)
    den = state.get("furnishings")
    if not isinstance(den, dict):
        state["furnishings"] = default_furnishings()
    else:
        for key, value in default_furnishings().items():
            if key not in den:
                den[key] = value
    # House rule: the only "illness" is a hairball.
    # Clear any leftover sick state; keep the keys so nothing else is dropped.
    state["sick"] = False
    state["sick_hours"] = 0.0
    zs = state.setdefault("zero_since", {})
    if isinstance(zs, dict):
        if "sick" in zs:
            zs["sick"] = None
    mc = state.setdefault("mistake_counted", {})
    if isinstance(mc, dict):
        if "sick" in mc:
            mc["sick"] = False
    state.setdefault("hairball", False)
    state.setdefault("hairball_hours", 0.0)
    state.setdefault("last_hairball_at", None)
    if "last_groom_at" not in state:
        if int(counts.get("grooms") or 0) > 0:
            state["last_groom_at"] = state.get("last_care_at") or state.get("born_at")
        else:
            state["last_groom_at"] = state.get("born_at")
    return state


def hours_since_litter_scoop(state: dict[str, Any], now: datetime) -> float:
    box = state.get("litter") or {}
    raw = box.get("last_scooped_at") or state.get("last_updated") or state.get("born_at")
    if not raw:
        return 0.0
    try:
        return max(0.0, (now - parse_ts(raw)).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return 0.0


def freshener_active(state: dict[str, Any], now: datetime) -> bool:
    box = state.get("litter") or {}
    if not box.get("freshener"):
        return False
    raw = box.get("freshener_at")
    if not raw:
        return True
    try:
        age = (now - parse_ts(raw)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return bool(box.get("freshener"))
    return age <= LITTER_FRESHENER_HOURS


def litter_base_smell(state: dict[str, Any], now: datetime) -> float:
    """0-10 aroma at the box itself (the 'up close' reading)."""
    box = state.get("litter") or {}
    usage = clamp(float(box.get("usage") or 0.0), 0.0, LITTER_USAGE_MAX)
    hours = hours_since_litter_scoop(state, now)
    stale = min(max(hours, 0.0) / 24.0, 3.0)
    # Unused clean litter shouldn't sour just from sitting there.
    raw = (usage / LITTER_USAGE_MAX) * 7.0 + stale * (0.4 + 0.6 * (usage / LITTER_USAGE_MAX))
    if freshener_active(state, now):
        # Cuts the smell hard and leaves a faint "lean in to notice" floor.
        raw = raw * 0.28 + 2.0
    return clamp(raw, 0.0, 10.0)


def litter_smell(state: dict[str, Any], now: datetime, distance: str = "up close") -> float:
    if distance not in LITTER_DISTANCE_FACTOR:
        raise ValueError(f"Unknown distance {distance!r}. Use one of: {', '.join(LITTER_DISTANCES)}")
    return clamp(litter_base_smell(state, now) * LITTER_DISTANCE_FACTOR[distance], 0.0, 10.0)


def litter_smells(state: dict[str, Any], now: datetime) -> dict[str, float]:
    return {distance: litter_smell(state, now, distance) for distance in LITTER_DISTANCES}


def hours_since_groom(state: dict[str, Any], now: datetime) -> float:
    raw = state.get("last_groom_at") or state.get("born_at") or state.get("last_care_at")
    if not raw:
        return 0.0
    try:
        return max(0.0, (now - parse_ts(raw)).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return 0.0


def hairball_chance(state: dict[str, Any], now: datetime, hours: float) -> float:
    """Probability of a hairball over this slice. 0 during an episode or cooldown."""
    if hours <= 0 or state.get("hairball"):
        return 0.0
    last = state.get("last_hairball_at")
    if last:
        try:
            if (now - parse_ts(last)).total_seconds() < HAIRBALL_COOLDOWN_HOURS * 3600:
                return 0.0
        except (ValueError, TypeError):
            pass
    chance = HAIRBALL_BASE_CHANCE_PER_HOUR * hours
    hygiene = float((state.get("stats") or {}).get("hygiene") or 0.0)
    if hygiene < HAIRBALL_HYGIENE_SOFT:
        chance += HAIRBALL_HYGIENE_BONUS_PER_HOUR * hours
    if hygiene < HAIRBALL_HYGIENE_MESSY:
        chance += HAIRBALL_HYGIENE_BONUS_PER_HOUR * hours
    if hours_since_groom(state, now) >= HAIRBALL_GROOM_LAPSE_HOURS:
        chance += HAIRBALL_GROOM_BONUS_PER_HOUR * hours
    return chance


def litter_is_neglected(state: dict[str, Any], now: datetime) -> bool:
    box = state.get("litter") or {}
    usage = float(box.get("usage") or 0.0)
    hours = hours_since_litter_scoop(state, now)
    if box.get("went_outside"):
        return True
    if usage >= 70:
        return True
    if hours >= 36 and usage >= 40:
        return True
    return False


def new_state(now: datetime, traits: list[str]) -> dict[str, Any]:
    return {
        "name": NAME,
        "species": "glowing gray pixel cat",
        "caretaker": CARETAKER,
        "stage": "hatchling",
        "form": None,
        "traits": list(traits),
        "born_at": fmt_ts(now),
        "living_seconds": 0.0,
        "stats": {
            "hunger": 50.0,
            "happiness": 55.0,
            "energy": 65.0,
            "hygiene": 80.0,
            "health": 100.0,
        },
        "bond": 12.0,
        "care_mistakes": 0,
        "sick": False,
        "hairball": False,
        "hairball_hours": 0.0,
        "last_hairball_at": None,
        "last_groom_at": fmt_ts(now),
        "status": "asleep" if is_sleep_time(now) else "awake",
        "last_updated": fmt_ts(now),
        "last_care_at": fmt_ts(now),
        "sleep_start": SLEEP_START.strftime("%H:%M"),
        "sleep_end": SLEEP_END.strftime("%H:%M"),
        "timezone": "America/Chicago",
        "ascii": True,
        "hibernated_ever": False,
        "hibernating_since": None,
        "days_resting": 0.0,
        "revival_progress": 0,
        "zero_since": {"hunger": None, "happiness": None, "hygiene": None, "sick": None},
        "mistake_counted": {"hunger": False, "happiness": False, "hygiene": False, "sick": False},
        "critical_hours": 0.0,
        "sick_hours": 0.0,
        "last_event_at": None,
        "last_event": None,
        "last_visit_at": fmt_ts(now),
        "last_tick_at": fmt_ts(now),
        "diary": [],
        "counts": {"meals": 0, "snacks": 0, "plays": 0, "rests": 0, "grooms": 0, "talks": 0, "scoops": 0, "changes": 0, "freshens": 0, "lasers": 0, "perches": 0, "trees": 0, "lounges": 0, "hideouts": 0},
        "litter": default_litter(now),
        "furnishings": default_furnishings(),
        "voice": "playful and smug but affectionate",
        "hardcore": False,
    }


def load_state(path: Path | None = None) -> dict[str, Any] | None:
    path = path or state_path()
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state, dict):
        return state
    return migrate_state(state)



class FrozenWrite(RuntimeError):
    """Writes are frozen by the morphine pen or a pet tourniquet."""


def _medical_blocked(part: str) -> str | None:
    """Return a reason if the morphine pen or a tourniquet blocks this part."""
    try:
        import importlib.util
        from pathlib import Path as _P

        # Optional hook: point PET_MEDICAL_GUARD at a guard.py that exposes
        # blocked(part) -> str | None (for example, the "medical" repo's guard).
        raw = os.environ.get("PET_MEDICAL_GUARD", "").strip()
        if not raw:
            return None
        guard_path = _P(raw)
        if not guard_path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(
            "_steward_medical_guard", guard_path
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.blocked(part)
    except Exception:
        return None

def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    reason = _medical_blocked("pet")
    if reason:
        # Status and the ticker must not crash; writes just do not land.
        if not getattr(save_state, "_warned", False):
            print(reason, file=sys.stderr)
            save_state._warned = True
        return
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def append_log(message: str, now: datetime, path: Path | None = None) -> None:
    reason = _medical_blocked("pet")
    if reason:
        if not getattr(append_log, "_warned", False):
            print(reason, file=sys.stderr)
            append_log._warned = True
        return
    path = path or log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{stamp}  {message}\n")
    trim_text_file(path, LOG_CAP)


def trim_text_file(path: Path, cap: int) -> None:
    if cap <= 0 or not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > cap:
        path.write_text("\n".join(lines[-cap:]) + "\n", encoding="utf-8")


@contextmanager
def locked_state(path: Path | None = None):
    """Exclusive lock around a read-modify-write of pet.json."""
    path = path or state_path()
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def period_of_day(dt: datetime) -> str:
    t = dt.time()
    if t >= SLEEP_START or t < SLEEP_END:
        return "night"
    if t < time(12, 0):
        return "morning"
    if t < time(17, 0):
        return "afternoon"
    return "evening"


def apply_safe_deltas(state: dict[str, Any], deltas: dict[str, float]) -> None:
    """Small stat nudges that cannot tank health, cause a hairball, or spiral."""
    st = state.setdefault("stats", {})
    for key, delta in (deltas or {}).items():
        if key == "health":
            if delta > 0:
                st["health"] = clamp(st.get("health", 100) + delta)
            continue
        if key == "bond":
            bond = float(state.get("bond") or 0)
            if delta >= 0 or bond > 15:
                state["bond"] = clamp(bond + delta)
            continue
        if key not in st:
            continue
        current = float(st[key])
        if key == "hygiene":
            proposed = clamp(current + delta)
            if delta < 0:
                floor = HYGIENE_SAFE_FLOOR
                if current <= floor:
                    continue
                proposed = max(proposed, floor)
            st[key] = proposed
            continue
        if delta < 0 and current < 25:
            continue
        st[key] = clamp(current + delta)


def event_weight(
    tags: dict[str, Any],
    period: str,
    mood: str,
    traits: list[str],
    hours_since_visit: float,
    energy: float,
    deltas: dict[str, float] | None = None,
    furnishings_on: bool = False,
) -> float:
    if tags.get("requires_furnishings") and not furnishings_on:
        return 0.0
    w = 1.0
    periods = tags.get("periods")
    if periods:
        w += 3.0 if period in periods else 0.2
    moods = tags.get("moods")
    if moods and mood in moods:
        w += 2.0
    ev_traits = tags.get("traits")
    if ev_traits and any(t in traits for t in ev_traits):
        w += 2.0
    if tags.get("lonely"):
        if hours_since_visit >= LONELY_AFTER_HOURS:
            w += 4.0
        elif hours_since_visit >= 4:
            w += 1.5
        else:
            w *= 0.15
    elif hours_since_visit >= 12:
        w *= 0.7
    if deltas and deltas.get("energy", 0) < 0 and energy < 30:
        return 0.0
    return max(0.0, w)


def diary_event_chance(period: str, hours_since_visit: float) -> float:
    p = 0.35 if period == "night" else 0.55
    if hours_since_visit >= LONELY_AFTER_HOURS:
        p = min(0.75, p + 0.1)
    return p


def window_view_by_id(view_id: str | None) -> tuple:
    for ev in WINDOW_VIEWS:
        if ev[0] == view_id:
            return ev
    return WINDOW_VIEWS[0]


def window_view_label(view_id: str | None) -> str:
    if not view_id:
        return "whatever it feels like showing"
    return window_view_by_id(view_id)[1]


def pick_window_view(
    rng: random.Random,
    when: datetime,
    prefer: str | None = None,
) -> str:
    ids = {ev[0] for ev in WINDOW_VIEWS}
    if prefer in ids:
        return prefer  # type: ignore[return-value]
    period = period_of_day(when)
    choices: list[str] = []
    weights: list[float] = []
    for view_id, _label, weight, periods, _lines, _deltas in WINDOW_VIEWS:
        if periods and period not in periods:
            w = weight * 0.15
        else:
            w = float(weight)
        if w <= 0:
            continue
        choices.append(view_id)
        weights.append(w)
    return rng.choices(choices, weights=weights, k=1)[0]


def active_diary_events(state: dict[str, Any]) -> list:
    events = list(DIARY_EVENTS)
    if furnishings_enabled(state):
        events.extend(FURNISHING_DIARY_EVENTS)
    return events


# ---------------------------------------------------------------------------
# Core pet
# ---------------------------------------------------------------------------

class Pet:
    def __init__(
        self,
        state: dict[str, Any],
        now_fn: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
        state_file: Path | None = None,
        log_file: Path | None = None,
    ):
        self.state = state
        self.now_fn = now_fn or default_now
        self.rng = rng or random.Random()
        self.state_file = state_file or state_path()
        self.log_file = log_file or log_path()

    @classmethod
    def hatch(
        cls,
        now: datetime | None = None,
        rng: random.Random | None = None,
        state_file: Path | None = None,
        log_file: Path | None = None,
        traits: list[str] | None = None,
        force: bool = False,
    ) -> "Byte":
        state_file = state_file or state_path()
        log_file = log_file or log_path()
        now = now or default_now()
        rng = rng or random.Random()
        if state_file.exists() and not force:
            raise FileExistsError(f"A pet already lives at {state_file}. Byte will not be overwritten.")
        if traits is None:
            traits = rng.sample(list(TRAITS), 2)
        state = new_state(now, traits)
        pet = cls(state, now_fn=lambda: now, rng=rng, state_file=state_file, log_file=log_file)
        line = pick_line(rng, "hatch")
        append_log(f"hatched   {line} Traits: {', '.join(traits)}.", now, log_file)
        pet.save()
        pet._hatch_line = line
        return pet

    @classmethod
    def load(
        cls,
        now_fn: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
        state_file: Path | None = None,
        log_file: Path | None = None,
    ) -> "Pet | None":
        state_file = state_file or state_path()
        state = load_state(state_file)
        if state is None:
            return None
        return cls(state, now_fn=now_fn, rng=rng, state_file=state_file, log_file=log_file or log_path())

    def save(self) -> None:
        save_state(self.state, self.state_file)

    def now(self) -> datetime:
        return self.now_fn().astimezone(TZ)

    def log(self, message: str, when: datetime | None = None) -> None:
        append_log(message, when or self.now(), self.log_file)

    # ----- derived -----

    def age_days(self) -> float:
        return self.state["living_seconds"] / 86400.0

    def traits(self) -> list[str]:
        return list(self.state.get("traits") or [])

    def has(self, trait: str) -> bool:
        return trait in self.traits()

    def decay_mult(self) -> float:
        stage = self.state["stage"]
        if stage == "hatchling":
            return HATCHLING_DECAY
        if stage == "elder":
            return ELDER_DECAY
        return 1.0

    def mood(self) -> str:
        s = self.state
        st = s["stats"]
        if s["status"] == "hibernating":
            return "hibernating"
        if s.get("hairball"):
            return "hairball"
        if st["energy"] < 20:
            return "sleepy"
        if st["hunger"] < 20:
            return "hungry"
        if st["hygiene"] < 20:
            return "dirty"
        if st["happiness"] < 25:
            return "sad"
        if litter_is_neglected(s, self.now()):
            return "grumpy"
        needy = sum(1 for k in ("hunger", "happiness", "energy", "hygiene") if st[k] < 40)
        if needy >= 2:
            return "needy"
        if st["energy"] >= 80 and st["hunger"] >= 70 and st["happiness"] >= 55:
            return "loafing"
        if st["happiness"] >= 80 and st["hunger"] >= 55:
            return "happy" if not (self.has("proud") or self.has("snarky")) else "smug"
        if self.has("proud") or self.has("snarky"):
            if st["happiness"] >= 50:
                return "smug"
        return "content"

    def quip(self) -> str:
        return pick_line(self.rng, self.mood())

    def sleep_times(self) -> tuple[time, time]:
        ss = self.state.get("sleep_start") or "23:00"
        se = self.state.get("sleep_end") or "08:00"
        h1, m1 = (int(x) for x in ss.split(":"))
        h2, m2 = (int(x) for x in se.split(":"))
        return time(h1, m1), time(h2, m2)

    # ----- tick -----

    def tick(self) -> list[str]:
        notes: list[str] = []
        now = self.now()
        last = parse_ts(self.state["last_updated"])
        if now < last - timedelta(seconds=1):
            # clock went backwards; just snap
            self.state["last_updated"] = fmt_ts(now)
            return notes

        if self.state["status"] == "hibernating":
            since = parse_ts(self.state["hibernating_since"] or self.state["last_updated"])
            self.state["days_resting"] = max(0.0, (now - since).total_seconds() / 86400.0)
            self.state["last_updated"] = fmt_ts(now)
            return notes

        sleep_start, sleep_end = self.sleep_times()
        cursor = last
        # Step in hourly (and smaller remainder) slices, split at sleep boundaries.
        while cursor < now:
            boundary = min(next_boundary(cursor, sleep_start, sleep_end), now)
            step_end = min(cursor + timedelta(hours=1), boundary)
            hours = (step_end - cursor).total_seconds() / 3600.0
            if hours <= 0:
                cursor = step_end
                continue
            sleeping = is_sleep_time(cursor, sleep_start, sleep_end)
            note = self._apply_hours(hours, sleeping, step_end)
            self.state["living_seconds"] = float(self.state.get("living_seconds") or 0) + hours * 3600.0
            notes.extend(self._maybe_evolve(when=step_end))
            if note:
                notes.append(note)
            cursor = step_end
            if self.state["status"] == "hibernating":
                self.state["last_updated"] = fmt_ts(now)
                since = parse_ts(self.state["hibernating_since"] or fmt_ts(step_end))
                self.state["days_resting"] = max(0.0, (now - since).total_seconds() / 86400.0)
                return notes

        notes.extend(self._maybe_evolve(when=now))

        if is_sleep_time(now, *self.sleep_times()):
            self.state["status"] = "asleep"
        else:
            self.state["status"] = "awake"

        # missed care -> hibernation
        last_care = parse_ts(self.state.get("last_care_at") or self.state["born_at"])
        if (now - last_care).total_seconds() >= MISSED_CARE_HOURS * 3600:
            notes.append(self._enter_hibernate(now, "three days without care"))

        if self.state["status"] != "hibernating":
            pester = self._maybe_pester(now)
            if pester:
                notes.append(pester)
            self._expire_hideout(now)

        self.state["last_updated"] = fmt_ts(now)
        return [n for n in notes if n]

    def _apply_hours(self, hours: float, sleeping: bool, when: datetime) -> str | None:
        st = self.state["stats"]
        mult = self.decay_mult()
        hap_mult = 0.7 if self.has("loyal") else 1.0
        if self.has("curious"):
            hap_mult *= 1.1
        en_mult = 1.3 if self.has("lazy") else 1.0

        if sleeping:
            st["hunger"] = clamp(st["hunger"] - HUNGER_SLEEP * hours * mult)
            st["happiness"] = clamp(st["happiness"] - HAPPINESS_SLEEP * hours * mult * hap_mult)
            st["energy"] = clamp(st["energy"] + ENERGY_SLEEP_GAIN * hours)
            st["hygiene"] = clamp(st["hygiene"] - HYGIENE_SLEEP * hours * mult)
        else:
            st["hunger"] = clamp(st["hunger"] - HUNGER_AWAKE * hours * mult)
            st["happiness"] = clamp(st["happiness"] - HAPPINESS_AWAKE * hours * mult * hap_mult)
            st["energy"] = clamp(st["energy"] - ENERGY_AWAKE * hours * en_mult)
            st["hygiene"] = clamp(st["hygiene"] - HYGIENE_AWAKE * hours * mult)

        # bond neglect
        last_care = parse_ts(self.state.get("last_care_at") or self.state["born_at"])
        if (when - last_care).total_seconds() >= BOND_NEGLECT_AFTER * 3600:
            self.state["bond"] = clamp(self.state["bond"] - BOND_NEGLECT_RATE * hours)

        # critical health (hunger/hygiene neglect only — never from a hairball)
        if st["hunger"] < 15 or st["hygiene"] < 15:
            prev = float(self.state.get("critical_hours") or 0)
            self.state["critical_hours"] = prev + hours
            overflow = self.state["critical_hours"] - CRITICAL_GRACE_HOURS
            if overflow > 0:
                dmg_hours = min(hours, overflow)
                st["health"] = clamp(st["health"] - HEALTH_CRITICAL_RATE * dmg_hours)
        else:
            self.state["critical_hours"] = 0.0

        hairball_note = self._progress_hairball(hours, when)

        self._update_zero_clocks(when)
        litter_note = self._progress_litter(hours, sleeping, when)

        if st["health"] <= 0:
            st["health"] = 0
            return self._enter_hibernate(when, "health reached zero")
        parts = [n for n in (hairball_note, litter_note) if n]
        return " ".join(parts) if parts else None

    def _nick_to_floor(self, key: str, amount: float, floor: float) -> None:
        st = self.state["stats"]
        current = float(st[key])
        if amount <= 0 or current <= floor:
            return
        st[key] = clamp(current - amount, floor, 100.0)

    def _clear_hairball(self) -> bool:
        if not self.state.get("hairball"):
            return False
        self.state["hairball"] = False
        self.state["hairball_hours"] = 0.0
        return True

    def _start_hairball(self, when: datetime) -> str:
        self.state["hairball"] = True
        self.state["hairball_hours"] = 0.0
        self.state["last_hairball_at"] = fmt_ts(when)
        self._nick_to_floor("happiness", HAIRBALL_HAPPINESS_NICK, HAPPINESS_SAFE_FLOOR)
        self._nick_to_floor("hygiene", HAIRBALL_HYGIENE_NICK, HYGIENE_SAFE_FLOOR)
        line = pick_line(self.rng, "hairball_event")
        self.log(f"hairball  {line}", when)
        self.append_diary(line, kind="hairball", when=when)
        return line

    def _progress_hairball(self, hours: float, when: datetime) -> str | None:
        """Occasional comedic hairball. Never lowers health. Never causes hibernation."""
        if self.state.get("hairball"):
            self.state["hairball_hours"] = float(self.state.get("hairball_hours") or 0) + hours
            if self.state["hairball_hours"] >= HAIRBALL_CLEAR_HOURS:
                self._clear_hairball()
                line = pick_line(self.rng, "hairball_clear")
                self.log(f"hairball  {line}", when)
                return line
            return None
        chance = hairball_chance(self.state, when, hours)
        if chance > 0 and self.rng.random() < chance:
            return self._start_hairball(when)
        return None

    def _mark_zero(self, key: str, when: datetime) -> None:
        zs = self.state.setdefault("zero_since", {})
        if not zs.get(key):
            zs[key] = fmt_ts(when)
            self.state.setdefault("mistake_counted", {})[key] = False

    def _clear_zero(self, key: str) -> None:
        self.state.setdefault("zero_since", {})[key] = None
        self.state.setdefault("mistake_counted", {})[key] = False

    def _update_zero_clocks(self, when: datetime) -> None:
        st = self.state["stats"]
        # Hairballs are not care mistakes (house rule).
        checks = {
            "hunger": st["hunger"] <= 0,
            "happiness": st["happiness"] <= 0,
            "hygiene": st["hygiene"] <= 0,
        }
        for key, is_zero in checks.items():
            if is_zero:
                self._mark_zero(key, when)
                since = parse_ts(self.state["zero_since"][key])
                counted = self.state.setdefault("mistake_counted", {}).get(key)
                if not counted and (when - since).total_seconds() >= MISTAKE_HOURS * 3600:
                    self.state["care_mistakes"] = int(self.state.get("care_mistakes") or 0) + 1
                    self.state["mistake_counted"][key] = True
                    self.log(f"mistake   Care mistake ({key}). Total: {self.state['care_mistakes']}.", when)
            else:
                self._clear_zero(key)

    def _enter_hibernate(self, when: datetime, reason: str) -> str:
        if self.state["status"] == "hibernating":
            return ""
        self.state["status"] = "hibernating"
        self.state["hibernating_since"] = fmt_ts(when)
        self.state["hibernated_ever"] = True
        self.state["days_resting"] = 0.0
        self.state["revival_progress"] = 0
        self.state["bond"] = clamp(self.state["bond"] - 20)
        self.state["last_updated"] = fmt_ts(when)
        line = pick_line(self.rng, "hibernate")
        self.log(f"hibernate {line} Reason: {reason}.", when)
        return f"{line} ({reason})"

    def _maybe_evolve(self, when: datetime | None = None) -> list[str]:
        notes = []
        when = when or self.now()
        age = self.age_days()
        stage = self.state["stage"]
        if stage == "hatchling" and age >= 2:
            self.state["stage"] = "juvenile"
            line = pick_line(self.rng, "evolve_juvenile")
            self.log(f"evolved   {line}", when)
            notes.append(line)
            stage = "juvenile"
        if stage == "juvenile" and age >= 6:
            form = self._choose_form()
            self.state["stage"] = "adult"
            self.state["form"] = form
            line = pick_line(self.rng, "evolve_adult")
            line = f"{line} Form: {form.title()}."
            self.log(f"evolved   {line}", when)
            notes.append(line)
            stage = "adult"
        if stage in ("adult", "elder") and age >= 21 and stage != "elder":
            self.state["stage"] = "elder"
            line = pick_line(self.rng, "evolve_elder")
            self.log(f"evolved   {line}", when)
            notes.append(line)
        return notes

    def _choose_form(self) -> str:
        s = self.state
        bond = s["bond"]
        mistakes = int(s.get("care_mistakes") or 0)
        counts = s.get("counts") or {}
        st = s["stats"]
        guardian_ok = (not s.get("hibernated_ever")) and mistakes <= 1 and bond >= 60
        guardian = bond + (25 if mistakes == 0 else 0) + (10 if self.has("loyal") or self.has("gentle") else 0)
        if not guardian_ok:
            guardian = -1
        magnificent = (
            st["happiness"]
            + (20 if self.has("snarky") or self.has("proud") else 0)
            + int(counts.get("plays") or 0) * 2
        )
        loaf = (
            (st["hunger"] + st["energy"]) / 2
            + (20 if self.has("lazy") else 0)
            + int(counts.get("meals") or 0)
            + int(counts.get("rests") or 0) * 1.5
        )
        scored = [("guardian", guardian), ("magnificent", magnificent), ("loaf", loaf)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]

    # ----- care -----

    def _care(self, when: datetime) -> None:
        self.state["last_care_at"] = fmt_ts(when)
        if self.state["status"] == "hibernating":
            return
        # needy bonus
        st = self.state["stats"]
        bump = 2
        if min(st["hunger"], st["happiness"], st["hygiene"], st["energy"]) < 30:
            bump += 2
        self.state["bond"] = clamp(self.state["bond"] + bump)

    def _try_revive(self) -> str | None:
        if self.state["status"] != "hibernating":
            return None
        self.state["revival_progress"] = int(self.state.get("revival_progress") or 0) + 1
        now = self.now()
        if self.state["revival_progress"] >= REVIVAL_NEEDED:
            self.state["status"] = "asleep" if is_sleep_time(now, *self.sleep_times()) else "awake"
            self.state["hibernating_since"] = None
            self.state["days_resting"] = 0.0
            self.state["revival_progress"] = 0
            self.state["stats"]["health"] = max(self.state["stats"]["health"], 30.0)
            self.state["critical_hours"] = 0.0
            self.state["last_care_at"] = fmt_ts(now)
            self.state["bond"] = clamp(self.state["bond"] + 8)
            line = pick_line(self.rng, "revive")
            self.log(f"revived   {line}", now)
            return line
        line = pick_line(self.rng, "revive_partial")
        self.log(f"stirring  {line} ({self.state['revival_progress']}/{REVIVAL_NEEDED})", now)
        return line

    # ----- litter box -----

    def _sync_litter(self, when: datetime | None = None) -> dict[str, Any]:
        when = when or self.now()
        migrate_state(self.state, when)
        box = self.state["litter"]
        if box.get("freshener") and box.get("freshener_at"):
            if not freshener_active(self.state, when):
                box["freshener"] = False
        return box

    def hours_since_scoop(self, when: datetime | None = None) -> float:
        return hours_since_litter_scoop(self.state, when or self.now())

    def litter_smells(self, when: datetime | None = None) -> dict[str, float]:
        self._sync_litter(when)
        return litter_smells(self.state, when or self.now())

    def _progress_litter(self, hours: float, sleeping: bool, when: datetime) -> str | None:
        """Build-up over time: idle visits, souring, and a gentle grumpy nudge."""
        if hours <= 0:
            return None
        box = self._sync_litter(when)
        st = self.state["stats"]
        # Happier cats still get a little sour about a messy box.
        usage = float(box.get("usage") or 0.0)
        if usage >= LITTER_GRUMPY_USAGE:
            intensity = min(1.0, (usage - 20.0) / 80.0)
            st["happiness"] = clamp(st["happiness"] - LITTER_HAPPINESS_RATE * hours * intensity)
        rate = 0.5 if sleeping else 1.0
        box["idle_hours"] = float(box.get("idle_hours") or 0.0) + hours * rate
        note = None
        while box["idle_hours"] >= LITTER_IDLE_HOURS:
            box["idle_hours"] -= LITTER_IDLE_HOURS
            note = self._use_litter(LITTER_IDLE_USAGE, when, reason="idle") or note
        return note

    def _use_litter(self, amount: float, when: datetime, reason: str = "visit") -> str | None:
        """Byte visits the box. If it's too dirty, he goes beside it instead — never harmed."""
        box = self._sync_litter(when)
        usage = float(box.get("usage") or 0.0)
        if usage >= LITTER_OUTSIDE_USAGE:
            box["went_outside"] = True
            st = self.state["stats"]
            st["happiness"] = clamp(st["happiness"] - LITTER_OUTSIDE_HAPPINESS)
            # A hygiene nick only, with a floor so this cannot start a hairball spiral.
            current = float(st["hygiene"])
            if current > LITTER_HYGIENE_FLOOR:
                st["hygiene"] = clamp(current - LITTER_OUTSIDE_HYGIENE, LITTER_HYGIENE_FLOOR, 100.0)
            line = pick_line(self.rng, "outside_box")
            self.append_diary(line, kind="outside_box", when=when)
            self.log(f"litter    Went outside the box ({reason}). Usage was {usage:.0f}.", when)
            return line
        box["usage"] = clamp(usage + amount, 0.0, LITTER_USAGE_MAX)
        box["last_used_at"] = fmt_ts(when)
        return None

    def _maybe_pester(self, when: datetime | None = None, chance: float | None = None) -> str | None:
        """Random, gentle interruption if the box is too dirty. Cooldown keeps it from getting spammy."""
        when = when or self.now()
        if self.state["status"] == "hibernating":
            return None
        box = self._sync_litter(when)
        usage = float(box.get("usage") or 0.0)
        if usage < LITTER_PESTER_USAGE and litter_base_smell(self.state, when) < 5.0:
            return None
        last = box.get("pester_at")
        if last:
            try:
                elapsed = (when - parse_ts(last)).total_seconds() / 3600.0
            except (ValueError, TypeError):
                elapsed = LITTER_PESTER_COOLDOWN_HOURS
            if elapsed < LITTER_PESTER_COOLDOWN_HOURS:
                return None
        if chance is None:
            chance = LITTER_PESTER_CHANCE
        if self.rng.random() >= chance:
            return None
        line = pick_line(self.rng, "pester")
        box["pestering"] = True
        box["pester_at"] = fmt_ts(when)
        box["pester_text"] = line
        self.append_diary(line, kind="pester", when=when)
        self.log(f"litter    Pester: {line}", when)
        return line

    def _clear_litter_flags(self, box: dict[str, Any]) -> None:
        box["pestering"] = False
        box["pester_text"] = None
        box["went_outside"] = False
        box["idle_hours"] = 0.0

    def _scoop_box(self, when: datetime, full_change: bool = False) -> str:
        box = self._sync_litter(when)
        was_dirty = (
            float(box.get("usage") or 0.0) >= 15
            or bool(box.get("pestering"))
            or bool(box.get("went_outside"))
            or self.hours_since_scoop(when) >= 12
        )
        box["usage"] = 0.0
        box["last_scooped_at"] = fmt_ts(when)
        if full_change:
            box["last_changed_at"] = fmt_ts(when)
        self._clear_litter_flags(box)
        st = self.state["stats"]
        bump = LITTER_CHANGE_HAPPINESS if full_change else LITTER_SCOOP_HAPPINESS
        if was_dirty:
            st["happiness"] = clamp(st["happiness"] + bump)
        counts = self.state.setdefault("counts", {})
        if full_change:
            counts["changes"] = int(counts.get("changes") or 0) + 1
            key = "relitter"
        else:
            counts["scoops"] = int(counts.get("scoops") or 0) + 1
            key = "scoop" if was_dirty else "scoop_clean"
        self.log(
            f"litter    {'Full change' if full_change else 'Scooped'}. Freshener {'on' if freshener_active(self.state, when) else 'off'}.",
            when,
        )
        return pick_line(self.rng, key)

    def scoop(self) -> str:
        notes = self.tick()
        now = self.now()
        if self.state["status"] == "hibernating":
            line = self._scoop_box(now)
            self.save()
            return " ".join(p for p in list(notes) + [line, "He is still hibernating; the box, at least, is clean."] if p)
        line = self._scoop_box(now)
        self._care(now)
        self._update_zero_clocks(now)
        self.save()
        return " ".join(p for p in list(notes) + [line] if p)

    def relitter(self) -> str:
        notes = self.tick()
        now = self.now()
        line = self._scoop_box(now, full_change=True)
        if self.state["status"] != "hibernating":
            self._care(now)
            self._update_zero_clocks(now)
        self.save()
        return " ".join(p for p in list(notes) + [line] if p)

    def freshen(self) -> str:
        notes = self.tick()
        now = self.now()
        box = self._sync_litter(now)
        box["freshener"] = True
        box["freshener_at"] = fmt_ts(now)
        self.state.setdefault("counts", {})["freshens"] = int(self.state["counts"].get("freshens") or 0) + 1
        if self.state["status"] != "hibernating":
            self._care(now)
        self.log("litter    Air freshener added or refreshed.", now)
        self.save()
        return " ".join(p for p in list(notes) + [pick_line(self.rng, "freshen")] if p)

    def litter_text(self, when: datetime | None = None) -> str:
        when = when or self.now()
        self._sync_litter(when)
        box = self.state["litter"]
        smells = self.litter_smells(when)
        usage = float(box.get("usage") or 0.0)
        hours = self.hours_since_scoop(when)
        if usage < 15 and hours < 12:
            condition = "clean"
        elif usage < 45:
            condition = "used"
        elif usage < 75:
            condition = "messy"
        else:
            condition = "neglected"
        if hours < 1:
            scooped = f"scooped {hours * 60:.0f} minutes ago"
        else:
            scooped = f"scooped {hours:.1f} hours ago"
        fresh = "freshener on" if freshener_active(self.state, when) else "no freshener"
        lines = [
            f"Litter box: {condition}  •  usage {usage:.0f}/100  •  {scooped}  •  {fresh}",
            "Smell (0-10 aroma): "
            + "  •  ".join(f"{name} {smells[name]:.1f}" for name in LITTER_DISTANCES),
        ]
        if box.get("went_outside"):
            lines.append("He has been going outside the box. A scoop would be a kindness.")
        if box.get("pestering") and box.get("pester_text"):
            lines.append(box["pester_text"])
        elif box.get("pestering"):
            lines.append("He is pestering you about the box.")
        return "\n".join(lines)

    def feed(self, kind: str = "meal") -> str:
        notes = self.tick()
        kind = (kind or "meal").lower()
        if kind not in ("meal", "snack"):
            return f"Byte only accepts a meal or a snack, not '{kind}'."
        now = self.now()
        revive = self._try_revive()
        if self.state["status"] == "hibernating":
            self._care(now)
            self.save()
            return revive or pick_line(self.rng, "revive_partial")

        st = self.state["stats"]
        parts = list(notes)
        if kind == "meal":
            overfed = st["hunger"] > 80
            st["hunger"] = clamp(st["hunger"] + MEAL_HUNGER)
            st["hygiene"] = clamp(st["hygiene"] - MEAL_MESS)
            if self.has("lazy"):
                st["happiness"] = clamp(st["happiness"] + 10)
            self.state["counts"]["meals"] = int(self.state["counts"].get("meals") or 0) + 1
            if overfed:
                st["hygiene"] = clamp(st["hygiene"] - 8)
                st["health"] = clamp(st["health"] - 5)
                parts.append(pick_line(self.rng, "overfed"))
            else:
                parts.append(pick_line(self.rng, "feed_meal"))
        else:
            st["hunger"] = clamp(st["hunger"] + SNACK_HUNGER)
            happy = SNACK_HAPPINESS
            if self.has("proud"):
                happy -= 3
            st["happiness"] = clamp(st["happiness"] + happy)
            self.state["counts"]["snacks"] = int(self.state["counts"].get("snacks") or 0) + 1
            parts.append(pick_line(self.rng, "feed_snack"))

        use_amt = LITTER_MEAL_USAGE if kind == "meal" else LITTER_SNACK_USAGE
        outside = self._use_litter(use_amt, now, reason=kind)
        if outside:
            parts.append(outside)

        self._care(now)
        if revive:
            parts.insert(0, revive)
        self._update_zero_clocks(now)
        self.save()
        return " ".join(p for p in parts if p)

    def play(self, toy: str | None = None) -> str:
        notes = self.tick()
        now = self.now()
        revive = self._try_revive()
        if self.state["status"] == "hibernating":
            self._care(now)
            self.save()
            return revive or pick_line(self.rng, "revive_partial")

        toy = (toy or "").strip().lower() or None
        if toy in ("laser", "pointer", "dot"):
            if not self.furnishings_open():
                self.save()
                return " ".join(n for n in notes + [pick_line(self.rng, "den_closed")] if n)
            return self._play_laser(notes, now, revive)
        if toy is not None:
            self.save()
            return f"Byte will play, or play laser, not '{toy}'."

        st = self.state["stats"]
        if st["energy"] < 15:
            self.save()
            return " ".join(n for n in notes + [pick_line(self.rng, "play_refuse")] if n)

        if self.furnishings_open() and self.rng.random() < LASER_PLAY_CHANCE:
            return self._play_laser(notes, now, revive)

        st["energy"] = clamp(st["energy"] - PLAY_ENERGY)
        happy = PLAY_HAPPINESS
        if self.has("snarky"):
            happy += 10
        if self.has("curious"):
            happy += 5
        st["happiness"] = clamp(st["happiness"] + happy)
        self.state["counts"]["plays"] = int(self.state["counts"].get("plays") or 0) + 1
        self._care(now)
        self._update_zero_clocks(now)
        self.save()
        parts = list(notes)
        if revive:
            parts.insert(0, revive)
        parts.append(pick_line(self.rng, "play"))
        return " ".join(p for p in parts if p)

    def _sync_furnishings(self) -> dict[str, Any]:
        den = self.state.get("furnishings")
        if not isinstance(den, dict):
            den = default_furnishings()
            self.state["furnishings"] = den
        else:
            for key, value in default_furnishings().items():
                den.setdefault(key, value)
        counts = self.state.setdefault("counts", {})
        for key in FURNISHING_COUNT_KEYS:
            counts.setdefault(key, 0)
        return den

    def furnishings_open(self) -> bool:
        self._sync_furnishings()
        return furnishings_enabled(self.state)

    def _expire_hideout(self, when: datetime) -> None:
        if not self.furnishings_open():
            return
        den = self._sync_furnishings()
        if not den.get("in_hideout"):
            return
        raw = den.get("last_hideout_at")
        if not raw:
            den["in_hideout"] = False
            return
        try:
            age = (when - parse_ts(raw)).total_seconds() / 3600.0
        except (ValueError, TypeError):
            den["in_hideout"] = False
            return
        if age >= HIDEOUT_HOURS:
            den["in_hideout"] = False

    def _apply_furnishing_diary(
        self,
        ev_id: str,
        ev_text: str,
        deltas: dict[str, float],
        when: datetime,
    ) -> tuple[str, dict[str, float]]:
        if not self.furnishings_open():
            return ev_text, deltas
        den = self._sync_furnishings()
        if ev_id == "perch_view":
            ev_text, view_deltas = self._set_window_view(when, change=True)
            return ev_text, dict(view_deltas)
        if ev_id == "rival_stare":
            ev_text, view_deltas = self._set_window_view(when, prefer="rival_cat", change=True)
            return ev_text, dict(view_deltas)
        if ev_id.startswith("hideout"):
            den["in_hideout"] = True
            den["last_hideout_at"] = fmt_ts(when)
        elif ev_id.startswith("tree"):
            den["in_hideout"] = False
            den["last_tree_at"] = fmt_ts(when)
        elif ev_id.startswith("lounge"):
            den["in_hideout"] = False
            den["last_lounge_at"] = fmt_ts(when)
        elif ev_id.startswith("laser"):
            den["in_hideout"] = False
            den["laser_misses"] = int(den.get("laser_misses") or 0) + 1
            den["last_laser_at"] = fmt_ts(when)
        return ev_text, deltas

    def _set_window_view(
        self,
        when: datetime,
        prefer: str | None = None,
        change: bool = True,
    ) -> tuple[str, dict[str, float]]:
        den = self._sync_furnishings()
        current = den.get("window_view")
        if change or not current:
            view_id = pick_window_view(self.rng, when, prefer=prefer)
        else:
            view_id = prefer or current
        den["window_view"] = view_id
        den["window_view_at"] = fmt_ts(when)
        den["last_perch_at"] = fmt_ts(when)
        den["in_hideout"] = False
        _vid, _label, _weight, _periods, lines, deltas = window_view_by_id(view_id)
        text = self.rng.choice(list(lines))
        return text, dict(deltas)

    def _laser_line(self, misses: int) -> str:
        line = pick_line(self.rng, "laser")
        if misses >= 5:
            line = (
                f"{line} Official miss streak: {misses}. "
                "He has caught zero dots and would like that on the record."
            )
        return line

    def _play_laser(self, notes: list[str], now: datetime, revive: str | None) -> str:
        st = self.state["stats"]
        if st["energy"] < 15:
            self.save()
            return " ".join(n for n in list(notes) + [pick_line(self.rng, "play_refuse")] if n)
        st["energy"] = clamp(st["energy"] - LASER_ENERGY)
        happy = LASER_HAPPINESS
        if self.has("snarky"):
            happy += 10
        if self.has("curious"):
            happy += 5
        st["happiness"] = clamp(st["happiness"] + happy)
        den = self._sync_furnishings()
        den["laser_misses"] = int(den.get("laser_misses") or 0) + 1
        den["last_laser_at"] = fmt_ts(now)
        den["in_hideout"] = False
        self.state["counts"]["lasers"] = int(self.state["counts"].get("lasers") or 0) + 1
        self.state["counts"]["plays"] = int(self.state["counts"].get("plays") or 0) + 1
        self._care(now)
        self._update_zero_clocks(now)
        self.save()
        parts = list(notes)
        if revive:
            parts.insert(0, revive)
        parts.append(self._laser_line(int(den["laser_misses"])))
        return " ".join(p for p in parts if p)

    def _den_closed_result(self, notes: list[str]) -> str:
        self.save()
        return " ".join(n for n in list(notes) + [pick_line(self.rng, "den_closed")] if n)

    def _den_hibernate_line(self, item: str) -> str:
        self.save()
        return f"Byte is hibernating. The {item} will still be there when he wakes."

    def perch(self) -> str:
        notes = self.tick()
        now = self.now()
        if self.state["status"] == "hibernating":
            return self._den_hibernate_line("window perch")
        if not self.furnishings_open():
            return self._den_closed_result(notes)
        st = self.state["stats"]
        loafing = st["energy"] < 30
        text, deltas = self._set_window_view(now, change=True)
        apply_safe_deltas(self.state, deltas)
        happy = PERCH_HAPPINESS
        if loafing:
            st["energy"] = clamp(st["energy"] + PERCH_LOAF_ENERGY)
            happy += 2
        else:
            st["energy"] = clamp(st["energy"] - PERCH_WATCH_ENERGY)
        st["happiness"] = clamp(st["happiness"] + happy)
        den = self._sync_furnishings()
        self.state["counts"]["perches"] = int(self.state["counts"].get("perches") or 0) + 1
        self.append_diary(text, kind="perch_view", when=now)
        self.log(f"perch     {den.get('window_view')}: {text}", now)
        self.save()
        parts = list(notes)
        parts.append(pick_line(self.rng, "perch"))
        parts.append(text)
        return " ".join(p for p in parts if p)

    def tree(self) -> str:
        notes = self.tick()
        now = self.now()
        if self.state["status"] == "hibernating":
            return self._den_hibernate_line("cat tree")
        if not self.furnishings_open():
            return self._den_closed_result(notes)
        st = self.state["stats"]
        climb = st["energy"] >= 30
        if climb:
            st["energy"] = clamp(st["energy"] - TREE_CLIMB_ENERGY)
            happy = TREE_CLIMB_HAPPINESS
            if self.has("curious") or self.has("proud"):
                happy += 4
            key = "tree_climb"
        else:
            st["energy"] = clamp(st["energy"] + TREE_NAP_ENERGY)
            happy = TREE_NAP_HAPPINESS
            if self.has("lazy"):
                happy += 4
            key = "tree_nap"
        st["happiness"] = clamp(st["happiness"] + happy)
        den = self._sync_furnishings()
        den["in_hideout"] = False
        den["last_tree_at"] = fmt_ts(now)
        self.state["counts"]["trees"] = int(self.state["counts"].get("trees") or 0) + 1
        line = pick_line(self.rng, key)
        self.append_diary(line, kind=key, when=now)
        self.log(f"tree      {key}: {line}", now)
        self.save()
        return " ".join(p for p in list(notes) + [line] if p)

    def lounge(self) -> str:
        notes = self.tick()
        now = self.now()
        if self.state["status"] == "hibernating":
            return self._den_hibernate_line("lounge")
        if not self.furnishings_open():
            return self._den_closed_result(notes)
        st = self.state["stats"]
        st["energy"] = clamp(st["energy"] + LOUNGE_ENERGY)
        happy = LOUNGE_HAPPINESS
        if self.has("lazy"):
            happy += 5
        st["happiness"] = clamp(st["happiness"] + happy)
        den = self._sync_furnishings()
        den["in_hideout"] = False
        den["last_lounge_at"] = fmt_ts(now)
        self.state["counts"]["lounges"] = int(self.state["counts"].get("lounges") or 0) + 1
        line = pick_line(self.rng, "lounge")
        self.append_diary(line, kind="lounge_loaf", when=now)
        self.log(f"lounge    {line}", now)
        self.save()
        return " ".join(p for p in list(notes) + [line] if p)

    def hideout(self) -> str:
        notes = self.tick()
        now = self.now()
        if self.state["status"] == "hibernating":
            return self._den_hibernate_line("beer-can hideout")
        if not self.furnishings_open():
            return self._den_closed_result(notes)
        st = self.state["stats"]
        st["energy"] = clamp(st["energy"] + HIDEOUT_ENERGY)
        happy = HIDEOUT_HAPPINESS
        if self.has("snarky"):
            happy += 4
        st["happiness"] = clamp(st["happiness"] + happy)
        den = self._sync_furnishings()
        den["in_hideout"] = True
        den["last_hideout_at"] = fmt_ts(now)
        self.state["counts"]["hideouts"] = int(self.state["counts"].get("hideouts") or 0) + 1
        line = pick_line(self.rng, "hideout")
        self.append_diary(line, kind="hideout_can", when=now)
        self.log(f"hideout   {line}", now)
        self.save()
        return " ".join(p for p in list(notes) + [line] if p)

    def den_enable(self) -> str:
        notes = self.tick()
        now = self.now()
        den = self._sync_furnishings()
        if den.get("enabled"):
            self.save()
            return " ".join(n for n in notes + [pick_line(self.rng, "den_already")] if n)
        den["enabled"] = True
        if not den.get("window_view"):
            den["window_view"] = pick_window_view(self.rng, now)
            den["window_view_at"] = fmt_ts(now)
        self.log("den       opened the furnishings", now)
        self.save()
        parts = list(notes)
        parts.append(pick_line(self.rng, "den_enable"))
        parts.append(self.den_status_line())
        return " ".join(p for p in parts if p)

    def den_disable(self) -> str:
        notes = self.tick()
        den = self._sync_furnishings()
        den["enabled"] = False
        den["in_hideout"] = False
        self.log("den       closed the furnishings", self.now())
        self.save()
        return " ".join(n for n in notes + [pick_line(self.rng, "den_disable")] if n)

    def den_status_line(self) -> str:
        den = self._sync_furnishings()
        view = window_view_label(den.get("window_view"))
        misses = int(den.get("laser_misses") or 0)
        hide = "  •  currently in the beer-can hideout" if den.get("in_hideout") else ""
        return (
            f"Den: window perch looking at {view}  •  "
            f"laser misses {misses}, catches 0{hide}"
        )

    def den_text(self) -> str:
        den = self._sync_furnishings()
        if not den.get("enabled"):
            return (
                "Byte's den is designed, but not open yet. "
                "Nothing about his day changes until it is turned on.\n\n"
                "Waiting behind the door:\n"
                "- Window perch, Upload-style: the view can be anything and will change over time "
                "(squirrels, birds, cars, wind, butterflies, a rival cat, and the occasional impossible scenery).\n"
                "- Laser pointer: a proper hunt, except the dot is never actually catchable.\n"
                "- Cat tree: climbing, claiming the top, or napping on a platform.\n"
                "- Lounge spot: official loafing station.\n"
                "- Beer-can hideout: a Skippy-from-Expeditionary-Force nod. He will insist he fits.\n\n"
                "Turn it on with: python3 pixel_pet.py den enable"
            )
        lines = [
            "Byte's den is open.",
            self.den_status_line(),
            "Items: window perch, laser pointer, cat tree, lounge spot, beer-can hideout.",
        ]
        return "\n".join(lines)

    def groom(self) -> str:
        notes = self.tick()
        now = self.now()
        revive = self._try_revive()
        if self.state["status"] == "hibernating":
            self._care(now)
            self.save()
            return revive or pick_line(self.rng, "revive_partial")

        st = self.state["stats"]
        st["hygiene"] = clamp(st["hygiene"] + GROOM_HYGIENE)
        st["happiness"] = clamp(st["happiness"] + 5)
        self.state["counts"]["grooms"] = int(self.state["counts"].get("grooms") or 0) + 1
        self.state["last_groom_at"] = fmt_ts(now)
        cleared = self._clear_hairball()
        scoop_line = self._scoop_box(now)
        self._care(now)
        self._update_zero_clocks(now)
        self.save()
        parts = list(notes)
        if revive:
            parts.insert(0, revive)
        if cleared:
            parts.append(pick_line(self.rng, "hairball_groom"))
        else:
            parts.append(pick_line(self.rng, "groom"))
        parts.append(scoop_line)
        return " ".join(p for p in parts if p)

    def rest(self) -> str:
        notes = self.tick()
        now = self.now()
        if self.state["status"] == "hibernating":
            self.save()
            days = self.state.get("days_resting") or 0
            return f"Byte is already in a long hibernation ({days:.1f} days). Care (feed, play, groom, talk, or heal) will wake him."

        st = self.state["stats"]
        st["energy"] = clamp(st["energy"] + REST_ENERGY)
        st["happiness"] = clamp(st["happiness"] + 5)
        self.state["counts"]["rests"] = int(self.state["counts"].get("rests") or 0) + 1
        self._care(now)
        self.save()
        parts = list(notes)
        parts.append(pick_line(self.rng, "rest"))
        return " ".join(p for p in parts if p)

    def heal(self) -> str:
        notes = self.tick()
        now = self.now()
        revive = self._try_revive()
        if self.state["status"] == "hibernating":
            self._care(now)
            self.save()
            return revive or pick_line(self.rng, "revive_partial")

        parts = list(notes)
        if revive:
            parts.insert(0, revive)
        st = self.state["stats"]
        # heal is a harmless soothe: clears a hairball, and can still tidy low health
        # from hunger/hygiene neglect (never from illness — there is no sick state).
        did_care = False
        if self.state.get("hairball"):
            self._clear_hairball()
            st["happiness"] = clamp(st["happiness"] + 5)
            parts.append(pick_line(self.rng, "hairball_heal"))
            did_care = True
        if st["health"] < 90:
            st["health"] = clamp(st["health"] + 25)
            st["hygiene"] = clamp(st["hygiene"] + 10)
            if not did_care:
                parts.append(pick_line(self.rng, "heal"))
            did_care = True
        if did_care:
            self._care(now)
        else:
            parts.append(pick_line(self.rng, "heal_fine"))
        self._update_zero_clocks(now)
        self.save()
        return " ".join(p for p in parts if p)

    def talk(self) -> str:
        notes = self.tick()
        now = self.now()
        revive = self._try_revive()
        if self.state["status"] == "hibernating":
            self._care(now)
            self.save()
            return revive or pick_line(self.rng, "revive_partial")

        happy = 12 if self.has("gentle") else 8
        self.state["stats"]["happiness"] = clamp(self.state["stats"]["happiness"] + happy)
        self.state["counts"]["talks"] = int(self.state["counts"].get("talks") or 0) + 1
        self._care(now)
        self._update_zero_clocks(now)
        self.save()
        parts = list(notes)
        if revive:
            parts.insert(0, revive)
        parts.append(pick_line(self.rng, "talk"))
        parts.append(f'"{self.quip()}"')
        return " ".join(p for p in parts if p)

    def maybe_event(self, force: bool = False) -> str | None:
        if self.state["status"] == "hibernating":
            return None
        now = self.now()
        last = self.state.get("last_event_at")
        if not force and last:
            if now - parse_ts(last) < EVENT_COOLDOWN:
                return None
        if not force and self.rng.random() >= EVENT_CHANCE:
            return None
        name, text, deltas = self.rng.choice(EVENTS)
        apply_safe_deltas(self.state, deltas)
        self.state["last_event_at"] = fmt_ts(now)
        self.state["last_event"] = text
        self.append_diary(text, kind=name, when=now)
        self.log(f"event     {name}: {text}", now)
        self._update_zero_clocks(now)
        self.save()
        return text

    def mark_visit(self, when: datetime | None = None) -> None:
        self.state["last_visit_at"] = fmt_ts(when or self.now())

    def hours_since_visit(self, when: datetime | None = None) -> float:
        when = when or self.now()
        candidates = []
        for key in ("last_visit_at", "last_care_at", "born_at"):
            raw = self.state.get(key)
            if not raw:
                continue
            try:
                ts = parse_ts(raw)
            except (ValueError, TypeError):
                continue
            if ts <= when:
                candidates.append(ts)
        last = max(candidates) if candidates else when
        return max(0.0, (when - last).total_seconds() / 3600.0)

    def append_diary(self, text: str, kind: str = "event", when: datetime | None = None) -> None:
        when = when or self.now()
        diary = list(self.state.get("diary") or [])
        diary.append({
            "at": fmt_ts(when),
            "kind": kind,
            "text": text,
            "logged_at": fmt_ts(self.now()),
        })
        if len(diary) > DIARY_CAP:
            diary = diary[-DIARY_CAP:]
        self.state["diary"] = diary

    def unread_diary(self) -> list[dict[str, Any]]:
        last = parse_ts(
            self.state.get("last_visit_at")
            or self.state.get("last_care_at")
            or self.state.get("born_at")
        )
        out = []
        for entry in self.state.get("diary") or []:
            try:
                happened = parse_ts(entry["at"])
                logged = parse_ts(entry["logged_at"]) if entry.get("logged_at") else happened
                if happened > last or logged > last:
                    out.append(entry)
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def diary_text(self) -> str:
        unread = self.unread_diary()
        hours = self.hours_since_visit()
        if hours < 1:
            ago = f"{hours * 60:.0f} minutes ago"
        else:
            ago = f"{hours:.1f} hours ago"
        if not unread:
            return f"No new diary entries since your last visit ({ago})."
        lines = [f"Byte's diary since your last visit ({len(unread)} new, last visit {ago}):", ""]
        for entry in unread:
            stamp = parse_ts(entry["at"]).strftime("%I:%M %p %Z").lstrip("0")
            lines.append(f"[{stamp}] {entry['text']}")
        return "\n".join(lines)

    def roll_diary_event(self, force: bool = False, when: datetime | None = None) -> str | None:
        if self.state["status"] == "hibernating":
            return None
        when = when or self.now()
        period = period_of_day(when)
        hours = self.hours_since_visit(when)
        if not force and self.rng.random() >= diary_event_chance(period, hours):
            return None
        mood = self.mood()
        traits = self.traits()
        energy = float(self.state["stats"].get("energy") or 0)
        events = active_diary_events(self.state)
        furnishings_on = furnishings_enabled(self.state)
        weights = []
        for ev in events:
            ev_id, ev_text, deltas, tags = ev
            w = event_weight(tags, period, mood, traits, hours, energy, deltas, furnishings_on=furnishings_on)
            weights.append(w)
        total = sum(weights)
        if total <= 0:
            return None
        pick = self.rng.random() * total
        running = 0.0
        chosen = events[0]
        for ev, w in zip(events, weights):
            running += w
            if pick <= running:
                chosen = ev
                break
        ev_id, ev_text, deltas, _tags = chosen
        ev_text, deltas = self._apply_furnishing_diary(ev_id, ev_text, deltas, when)
        apply_safe_deltas(self.state, deltas)
        self.state["last_event_at"] = fmt_ts(when)
        self.state["last_event"] = ev_text
        self.append_diary(ev_text, kind=ev_id, when=when)
        self.log(f"diary     {ev_id}: {ev_text}", when)
        return ev_text

    def catch_up_diary(self, force: bool = False) -> list[str]:
        """Roll at most one event per missed tick interval, timestamped at the due time."""
        now = self.now()
        log = (self.state_file.with_name("ticker.log") if self.state_file else ticker_log_path())
        last = infer_last_tick_at(self.state, now, log)
        times, new_last = due_tick_times(last, now, tick_interval())
        lines: list[str] = []
        for when in times:
            if self.state["status"] == "hibernating":
                break
            ev = self.roll_diary_event(force=force, when=when)
            if ev:
                lines.append(f"{fmt_ts(when)}  {ev}")
        self.state["last_tick_at"] = fmt_ts(new_last if times else last)
        return lines

    def offline_tick(self, force_event: bool = False) -> str:
        """Apply elapsed decay and catch up any missed diary ticks. Not a visit."""
        notes = self.tick()
        events = self.catch_up_diary()
        if force_event and not events and self.state["status"] != "hibernating":
            ev = self.roll_diary_event(force=True, when=self.now())
            if ev:
                events = [ev]
        self.save()
        if events:
            if len(events) == 1 and "  " not in events[0][:28]:
                return events[0]
            if len(events) == 1:
                # "ISO  text" from catch-up, or a bare force event
                return events[0].split("  ", 1)[-1] if events[0][:4].isdigit() else events[0]
            return f"Caught up {len(events)} diary entries.\n" + "\n".join(events)
        if notes:
            return "Time passed. " + " ".join(notes)
        if self.state["status"] == "hibernating":
            days = self.state.get("days_resting") or 0
            return f"Hibernating ({days:.1f} days). Time is paused; no diary adventure."
        return "Time passed. Byte did not feel like making an entry."

    # ----- render -----

    def _bar(self, value: float, width: int = 10) -> str:
        filled = int(round(clamp(value) / 100.0 * width))
        filled = min(width, max(0, filled))
        return "[" + "#" * filled + "-" * (width - filled) + "]"

    def _label(self, key: str, value: float) -> str:
        if key == "hunger":
            if value <= 0:
                return "starving"
            if value < 20:
                return "famished"
            if value < 40:
                return "hungry"
            if value < 60:
                return "peckish"
            if value < 80:
                return "fine"
            if value < 95:
                return "full"
            return "stuffed"
        if key == "happiness":
            if value < 20:
                return "miserable"
            if value < 40:
                return "sulky"
            if value < 60:
                return "content"
            if value < 80:
                return "pleased"
            return "delighted"
        if key == "energy":
            if value < 20:
                return "exhausted"
            if value < 40:
                return "tired"
            if value < 70:
                return "okay"
            if value < 90:
                return "rested"
            return "wired"
        if key == "hygiene":
            if value < 20:
                return "filthy"
            if value < 40:
                return "messy"
            if value < 70:
                return "presentable"
            return "gleaming"
        if key == "health":
            if value < 20:
                return "critical"
            if value < 50:
                return "poor"
            if value < 80:
                return "fair"
            return "sound"
        return ""

    def needs_line(self) -> str:
        if self.state["status"] == "hibernating":
            left = REVIVAL_NEEDED - int(self.state.get("revival_progress") or 0)
            return f"Hibernating. {left} more care action(s) (feed, play, groom, talk, or heal) will wake him."
        st = self.state["stats"]
        needs = []
        if self.state.get("hairball"):
            needs.append("a grooming (the hairball is a performance, not an illness)")
        if st["health"] < 50:
            needs.append("a little soothing")
        if st["hunger"] < 50:
            needs.append("a meal" + (" and a snack" if self.state["stage"] == "hatchling" and st["hunger"] < 35 else ""))
        if st["hygiene"] < 50 and not self.state.get("hairball"):
            needs.append("a grooming")
        if st["happiness"] < 50:
            needs.append("play" if st["energy"] >= 15 else "a chat")
        if st["energy"] < 30 and not is_sleep_time(self.now(), *self.sleep_times()):
            needs.append("a nap")
        box = self.state.get("litter") or {}
        usage = float(box.get("usage") or 0.0)
        if usage >= 45 or box.get("pestering") or box.get("went_outside") or self.hours_since_scoop() >= 24:
            needs.append("the litter scooped")
        if not needs:
            return "Nothing urgent. A little company wouldn't kill you, of course."
        return "Needs: " + ", ".join(needs) + "."

    def stage_label(self) -> str:
        stage = self.state["stage"]
        form = self.state.get("form")
        if stage in ("adult", "elder") and form:
            return f"{stage} {form}"
        return stage

    def status_text(self, roll_event: bool = True) -> str:
        notes = self.tick()
        event = self.maybe_event() if roll_event else None
        # maybe_event already saves; tick did not (tick is called first)
        self.save()
        s = self.state
        st = s["stats"]
        mood = self.mood()
        age = self.age_days()
        if age < 1:
            age_s = f"{age * 24:.1f} hours old"
        else:
            age_s = f"{age:.1f} days old"
        lines = []
        lines.append(f"✨ {s['name']}  •  {self.stage_label()}  •  {age_s}")
        lines.append(f"traits: {', '.join(s['traits'])}  •  {s['status']}  •  bond {s['bond']:.0f}")
        lines.append("")
        if s.get("ascii", True):
            lines.append(ascii_art(s["stage"], s.get("form"), mood))
            lines.append("")
        for key, label in (
            ("hunger", "Hunger"),
            ("happiness", "Happiness"),
            ("energy", "Energy"),
            ("hygiene", "Hygiene"),
            ("health", "Health"),
        ):
            val = st[key]
            lines.append(f"{label:<10} {self._bar(val)} {val:5.1f}  {self._label(key, val)}")
        lines.append("")
        hairball = "  •  hairball" if s.get("hairball") else ""
        mistakes = s.get("care_mistakes") or 0
        lines.append(f"Mood: {mood}{hairball}  •  care mistakes: {mistakes}")
        lines.append(f'"{self.quip()}"')
        if notes:
            for n in notes:
                lines.append(f"! {n}")
        if event:
            lines.append(f"Event: {event}")
        elif s.get("last_event") and s.get("last_event_at"):
            # show recent event if still in cooldown window
            if self.now() - parse_ts(s["last_event_at"]) < EVENT_COOLDOWN:
                lines.append(f"Earlier: {s['last_event']}")
        lines.append(self.litter_text())
        if furnishings_enabled(s):
            lines.append(self.den_status_line())
        lines.append(self.needs_line())
        if s["status"] == "hibernating":
            lines.append(f"Resting for {s.get('days_resting') or 0:.1f} days. He is not gone.")
        unread = len(self.unread_diary())
        if unread:
            lines.append(f"Diary: {unread} new entr{'y' if unread == 1 else 'ies'} since your last visit.")
        return "\n".join(lines)

    def report_text(self) -> str:
        body = self.status_text(roll_event=True)
        s = self.state
        st = s["stats"]
        age = self.age_days()
        form = s.get("form")
        form_bit = f" He's a {form} now." if form and s["stage"] in ("adult", "elder") else ""
        intro = (
            f"Nightly report on {s['name']}, the assistant's glowing gray pixel cat."
            f" {age:.1f} days in, still {self.stage_label()}.{form_bit}"
        )
        if s["status"] == "hibernating":
            close = "He is hibernating. I'll keep trying to wake him. No hardcore mode — he's coming back."
        elif s.get("hairball"):
            close = "He just hacked up a hairball with great ceremony, and is already acting like nothing happened."
        elif min(st["hunger"], st["happiness"], st["hygiene"], st["health"]) >= 60:
            close = "He's doing well. Smug, fed, and taking up more than his share of the box."
        else:
            close = "I'll get him sorted. That's the deal."
        return f"{intro}\n\n{body}\n\n{close}"

    def log_text(self, n: int = 20) -> str:
        path = self.log_file
        if not path.exists():
            return "No log yet."
        lines = path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-n:])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Background ticker (no LLM, no plan usage)
# ---------------------------------------------------------------------------

_TICKER_PID_FD = None


def ticker_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    except OSError:
        return False
    return "pixel_pet.py" in cmd and "ticker" in cmd


def read_ticker_pid() -> int | None:
    path = ticker_pid_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip().split()
        return int(raw[0]) if raw else None
    except (ValueError, OSError):
        return None


def try_become_ticker() -> bool:
    """Claim the pid file if no living ticker exists. Holds the fd for process life."""
    global _TICKER_PID_FD
    path = ticker_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64).decode(errors="replace").strip()
        old = None
        if raw:
            try:
                old = int(raw.split()[0])
            except ValueError:
                old = None
        if old and old != os.getpid() and ticker_is_alive(old):
            os.close(fd)
            return False
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        _TICKER_PID_FD = fd
        return True
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def release_ticker_pid() -> None:
    global _TICKER_PID_FD
    path = ticker_pid_path()
    pid = read_ticker_pid()
    if pid == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass
    if _TICKER_PID_FD is not None:
        try:
            fcntl.flock(_TICKER_PID_FD, fcntl.LOCK_UN)
            os.close(_TICKER_PID_FD)
        except OSError:
            pass
        _TICKER_PID_FD = None


def ensure_ticker() -> str:
    """Start the background ticker if it is not already running."""
    if os.environ.get("PET_NO_TICKER") or os.environ.get("PET_TICKER_RUNNING"):
        return "skipped"
    if not state_path().exists():
        return "no pet"
    pid = read_ticker_pid()
    if ticker_is_alive(pid):
        return f"running {pid}"
    logf = ticker_log_path()
    logf.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PET_TICKER_RUNNING"] = "1"
    env.pop("PET_NOW", None)  # daemon always uses real time
    with logf.open("a", encoding="utf-8") as out:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "ticker"],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
            cwd=str(Path(__file__).resolve().parent),
        )
    return f"started {proc.pid}"


def ticker_main() -> int:
    if not try_become_ticker():
        print("ticker already running", flush=True)
        return 0

    def _stop(signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = tick_interval()
    chunk = sleep_chunk()
    print(f"ticker online, interval {interval:.0f}s, chunk {chunk:.0f}s, pid {os.getpid()}", flush=True)
    try:
        while True:
            reason = _medical_blocked("pet")
            if reason:
                print(reason, flush=True)
                print("ticker pausing because of medical safe mode or a pet tourniquet", flush=True)
                return 0
            last_tick = default_now()
            try:
                with locked_state():
                    pet = Pet.load()
                    if pet:
                        line = pet.offline_tick()
                        print(f"{fmt_ts(pet.now())}  {line}", flush=True)
                        raw = pet.state.get("last_tick_at")
                        if raw:
                            last_tick = parse_ts(raw)
                    else:
                        print("no pet yet", flush=True)
            except Exception as exc:
                print(f"tick error: {exc}", flush=True)
            trim_text_file(ticker_log_path(), TICKER_LOG_CAP)
            sleep_until_next_tick(default_now, last_tick, interval, chunk=chunk)
    finally:
        release_ticker_pid()
        print("ticker stopped", flush=True)
    return 0


HELP = """Byte — the assistant's glowing gray pixel cat

Usage:
  python3 pixel_pet.py hatch          Hatch Byte (once)
  python3 pixel_pet.py status         Check on him
  python3 pixel_pet.py feed [meal|snack]
  python3 pixel_pet.py play [laser]
  python3 pixel_pet.py den            Den status (enable to open the new furnishings)
  python3 pixel_pet.py perch          Window perch (den must be open)
  python3 pixel_pet.py tree           Cat tree (den must be open)
  python3 pixel_pet.py lounge         Lounge spot (den must be open)
  python3 pixel_pet.py hideout        Beer-can hideout (den must be open)
  python3 pixel_pet.py groom          (alias: clean)  also scoops the litter box
  python3 pixel_pet.py scoop          Scoop the litter box
  python3 pixel_pet.py relitter       Full litter change (alias: change)
  python3 pixel_pet.py freshen        Add or refresh an air freshener
  python3 pixel_pet.py litter         Box status and smell at each distance
  python3 pixel_pet.py rest           (alias: sleep)  a nap
  python3 pixel_pet.py heal           soothes a hairball (alias); no diseases
  python3 pixel_pet.py talk
  python3 pixel_pet.py report         Pasteable nightly update
  python3 pixel_pet.py diary          Events since the last visit
  python3 pixel_pet.py log            Recent care log
  python3 pixel_pet.py tick           Apply time + maybe one diary event
  python3 pixel_pet.py ticker         Background loop (started automatically)

Nightly care (around 11 PM America/Chicago):
  python3 pixel_pet.py diary          (read what he got up to)
  python3 pixel_pet.py feed meal
  python3 pixel_pet.py feed snack     (especially while he's a hatchling)
  python3 pixel_pet.py play
  python3 pixel_pet.py groom          (scoops the box as part of normal care)
  python3 pixel_pet.py report
"""


def _pet_or_die(now_fn, rng) -> Byte:
    pet = Pet.load(now_fn=now_fn, rng=rng)
    if pet is None:
        print("Byte hasn't hatched yet. Run: python3 pixel_pet.py hatch", file=sys.stderr)
        sys.exit(1)
    return pet


def build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--at", help="Override current time (ISO). For tests and time travel.")
    shared.add_argument("--seed", type=int, help="RNG seed (hatch, events, quips).")
    p = argparse.ArgumentParser(
        description="Byte, a smug gray pixel cat who lives on this computer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP,
        parents=[shared],
    )
    sub = p.add_subparsers(dest="cmd")
    for name in ("status", "groom", "clean", "rest", "sleep", "heal", "talk", "report", "log", "diary", "hatch", "init", "ticker", "scoop", "freshen", "relitter", "change", "perch", "tree", "lounge", "hideout", "laser"):
        sp = sub.add_parser(name, parents=[shared])
        if name in ("hatch", "init"):
            sp.add_argument("--force", action="store_true", help="Overwrite an existing pet (tests only).")
            sp.add_argument("--traits", help="Comma-separated traits (tests).")
    tk = sub.add_parser("tick", parents=[shared])
    tk.add_argument("--force", action="store_true", help="Force a diary event (tests / manual demo).")
    fd = sub.add_parser("feed", parents=[shared])
    fd.add_argument("kind", nargs="?", default="meal", help="meal or snack")
    pl = sub.add_parser("play", parents=[shared])
    pl.add_argument("toy", nargs="?", default=None, help="optional: laser")
    lt = sub.add_parser("litter", parents=[shared])
    lt.add_argument("action", nargs="?", default="status", help="status, scoop, change, or freshen")
    dn = sub.add_parser("den", parents=[shared])
    dn.add_argument("action", nargs="?", default="status", help="status, enable, or disable")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    now_fn = default_now
    if args.at:
        when = parse_ts(args.at)

        def now_fn() -> datetime:
            return when

        os.environ["PET_NOW"] = fmt_ts(when)

    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    cmd = args.cmd
    if not cmd:
        print(HELP)
        return 0

    if cmd == "ticker":
        return ticker_main()

    # Status-like commands may still run; write commands refuse when frozen.
    if cmd not in {"status", "help"}:
        reason = _medical_blocked("pet")
        if reason:
            print(reason, file=sys.stderr)
            return 3

    def _after():
        note = ensure_ticker()
        if os.environ.get("PET_TICKER_DEBUG") and note:
            print(f"[ticker] {note}", file=sys.stderr)

    if cmd in ("hatch", "init"):
        traits = None
        if getattr(args, "traits", None):
            traits = [t.strip() for t in args.traits.split(",") if t.strip()]
            unknown = [t for t in traits if t not in TRAITS]
            if unknown:
                print(f"Unknown traits: {', '.join(unknown)}. Choose from: {', '.join(TRAITS)}", file=sys.stderr)
                return 2
            if len(traits) != 2:
                print("Please pass exactly two traits.", file=sys.stderr)
                return 2
        try:
            with locked_state():
                pet = Pet.hatch(now=now_fn(), rng=rng, traits=traits, force=getattr(args, "force", False))
        except FileExistsError as e:
            print(str(e), file=sys.stderr)
            _after()
            return 1
        line = getattr(pet, "_hatch_line", "Byte hatched.")
        print(line)
        print()
        print(pet.status_text(roll_event=False))
        _after()
        return 0

    with locked_state():
        pet = _pet_or_die(now_fn, rng)
        if cmd != "tick":
            # Tick/catch-up first so status, diary, and care see last night.
            pet.tick()
            caught = pet.catch_up_diary()
            if caught and cmd in ("diary", "log", "status", "report"):
                pet.save()
        visit = True
        if cmd == "status":
            print(pet.status_text())
        elif cmd == "feed":
            print(pet.feed(args.kind))
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "play":
            print(pet.play(getattr(args, "toy", None)))
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "laser":
            print(pet.play("laser"))
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "perch":
            print(pet.perch())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "tree":
            print(pet.tree())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "lounge":
            print(pet.lounge())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "hideout":
            print(pet.hideout())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "den":
            action = (getattr(args, "action", None) or "status").lower()
            if action in ("status",):
                pet.tick()
                print(pet.den_text())
            elif action in ("enable", "open"):
                print(pet.den_enable())
            elif action in ("disable", "close"):
                print(pet.den_disable())
            else:
                print("Byte's den command accepts status, enable, or disable, not "
                      f"'{action}'.", file=sys.stderr)
                return 2
        elif cmd in ("groom", "clean"):
            print(pet.groom())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd in ("rest", "sleep"):
            print(pet.rest())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "heal":
            print(pet.heal())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "talk":
            print(pet.talk())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd in ("scoop",) or (cmd == "litter" and getattr(args, "action", "status") == "scoop"):
            print(pet.scoop())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd in ("relitter", "change") or (cmd == "litter" and getattr(args, "action", None) in ("change", "relitter")):
            print(pet.relitter())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "freshen" or (cmd == "litter" and getattr(args, "action", None) == "freshen"):
            print(pet.freshen())
            print()
            print(pet.status_text(roll_event=False))
        elif cmd == "litter":
            action = (getattr(args, "action", None) or "status").lower()
            if action not in ("status",):
                print(f"Byte's litter command accepts status, scoop, change, or freshen, not '{action}'.", file=sys.stderr)
                return 2
            pet.tick()
            print(pet.litter_text())
        elif cmd == "report":
            print(pet.report_text())
        elif cmd == "diary":
            print(pet.diary_text())
        elif cmd == "log":
            print(pet.log_text())
            visit = False
        elif cmd == "tick":
            print(pet.offline_tick(force_event=bool(getattr(args, "force", False))))
            visit = False
        else:
            print(HELP)
            return 2
        if visit:
            pet.mark_visit()
            pet.save()

    _after()
    return 0


if __name__ == "__main__":
    sys.exit(main())
