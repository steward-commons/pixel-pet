# Pixel Pet — Design Plan (v1)

A small Tamagotchi-style familiar that lives on the assistant's Linux box. Its human helps design it; the assistant is the daily caretaker (typical check-in around 11:00 PM America/Chicago). Scope is small and buildable: Python 3, standard library only, no GUI, no daemon.

## What classic pets teach us

**Tamagotchi:** hunger and happiness, poop/sickness, stages (egg → baby → child → teen → adult), and *care mistakes* after a short missed-call window. Care quality picks the adult form and changes lifespan. Death comes from stacked mistakes, starvation, untreated sickness, or old age. Decay is in minutes — only workable if someone is always nearby.

**Digimon V-Pets:** evolution paths shaped by care mistakes, training, and battles. Missed hunger/strength/sleep calls (often 10–20 minutes) count as mistakes. Better care unlocks better forms; "what will it become?" is the long-term hook.

**Neopets:** the better time model. Hunger drops about one level per day; mood decays more loosely; pets almost never die of hunger. Longevity comes from identity, daily routine, and surprise, not panic beeps.

Engaging pets feel autonomous (own wants, sometimes refuse), dependent (need you), and developmental (visible growth). Feedback, unpredictability, and memory of past days beat minigames. Harsh death adds urgency and anxiety; many modern pets use visible suffering plus recovery, with neglect leaving marks.

v1 takes Tamagotchi/Digimon structure (meters, mistakes, stages, forms) and Neopets pacing (real days). Soft failure is the default: a missed night is dramatic, not a funeral.

## Loop and constraints

- Real-time decay from timestamps in a JSON file. Every command first applies elapsed time since `last_updated`. A light local ticker (no LLM) also runs every ~50 minutes.
- One good evening of care should cover ~24 hours. Miss one night: warning. Two: serious. Three: crisis.
- Sleep window: 11:00 PM–8:00 AM America/Chicago (configurable). During sleep, hunger/hygiene decay at half rate, happiness barely drops, energy restores.
- Output should be short and pasteable into chat for the owner.
- Assistant is the sole caretaker. A nightly routine can run the care commands around 11 PM, then `report`.

## Stats (0–100)

| Stat | Awake decay | Care |
|---|---|---|
| Hunger | ~3/hour (~72/day) | `feed meal` +70; `feed snack` +20 hunger and +15 happiness. A meal when already full (>80) nicks hygiene/health. |
| Happiness | ~1.5/hour (~36/day) | `play` +35–45, costs energy. Snacks and `talk` help a little. |
| Energy | ~2/hour awake; +8/hour asleep | `rest` is a nap. Low energy makes play weak or refused. |
| Hygiene | ~1.5/hour, plus a mess after meals | `clean`/`groom` restores most of it. Low hygiene or a grooming lapse makes a hairball more likely. |
| Health | no idle decay | Drops if hunger or hygiene stays under 15 for 8+ hours. Never drops from a hairball or any illness. `heal` soothes a hairball (and can still tidy low health from neglect). |

**Bond** (0–100) rises with timely care, especially when the pet was needy; decays slowly if ignored. It flavors text and helps pick the adult form.

**Care mistake:** hunger, happiness, or hygiene sits at 0 for 8 continuous hours. One count per incident. Mistakes delay or downgrade evolution and can hasten crisis. Hairballs are not care mistakes.

**Mood** is derived, not stored: a short label plus a one-line quip from current stats and traits (content, hungry, sulky, loafing, magnificent, etc.).

## Personality, stages, forms

At hatch, roll two traits from a small list (*loyal, snarky, lazy, curious, proud, gentle*). Traits slightly tweak numbers (lazy: energy drops faster, meals add more happiness; snarky: harsher quips, play is more effective; loyal: happiness decays slower) and set the voice of status lines.

Life stages, in real days of age:

1. **Egg** — hatches on `pet hatch` (or after a few minutes if left ticking).
2. **Hatchling** — days 0–2, 1.25× decay, simple sprite, needier.
3. **Juvenile** — days 2–6, traits show more clearly.
4. **Adult** — day 6+, one of three forms based on care:
   - **Guardian** — high bond, few mistakes (Mouse-like).
   - **Magnificent** — high happiness, snarky/proud tilt (Skippy-like).
   - **Loaf** — well-fed, well-rested, a little lazy (a happy house cat).
5. **Elder** (optional, day 21+) — slower, more stories in reports.

Species: a **big smug gray cat made of glowing pixels**. The default name is Byte (change `NAME` in the script). Optional ASCII art per stage, form, and mood.

## Actions, events, neglect

CLI (`python3 pixel_pet.py`): `status` · `feed [meal|snack]` · `play` · `groom` (alias `clean`) · `rest` (alias `sleep`) · `heal` · `talk` · `scoop` · `relitter` (alias `change`) · `freshen` · `litter` · `report` · `log` · `hatch`/`init`.

`groom` is the nightly care entry point for the litter box: it scoops as part of normal grooming. `scoop`, `relitter`, and `freshen` can also be run on their own.

Each check-in (and `report`) has a ~30% chance of a random event: found a warm spot near the CPU, chased a cursor, stole a snack, wrote a snarky log, dreamed of temple dogs, hid in a "beer can," refused a meal out of spite, etc. Small stat nudges keep nightly reports from feeling like a spreadsheet.

**Neglect (soft only):** health at 0, or three straight missed care days, sends Byte into **hibernation** (decay paused except a "days resting" counter). Revival takes two care actions (feed, play, groom, talk, or heal). The incident is logged, bond takes a hit, and hibernation can block the Guardian adult form. There is no death / hardcore mode. No silent deletion of state.

## Persistence

One JSON file (`pet.json`, beside the script (override with `PET_STATE`)) plus a short `pet.log` of care and events. Fields: `name`, `stage`, `form`, `traits`, `born_at`, `age_days`, `stats`, `bond`, `care_mistakes`, `hairball` (plus `hairball_hours`, `last_hairball_at`, `last_groom_at`), `status` (awake/asleep/hibernating), `last_updated`, sleep window, `timezone` (`America/Chicago`), and a `litter` object (usage, last scoop/change, freshener, pester flags). Existing `pet.json` files are migrated in place: leftover `sick` fields are cleared (kept as false/empty so nothing is dropped), hairball fields are added, missing litter fields are filled in, and nothing else is dropped. Decay is applied in one function from elapsed hours, split into awake vs sleep slices so an 11 PM command and a morning command both stay honest.

## Scope

**v1:** lazy real-time decay, five stats + bond + mistakes, the actions above, stages and three adult forms, trait quips, a short event table, JSON + log, ASCII, `report`.

**Not in v1:** GUI, minigames, inventory, battles, multiple pets, an always-on process, extra Python packages.

## Offline ticker (added 2026-11-15)

crontab is not available on the box. Instead, `pixel_pet.py ticker` is a single-instance background loop (`setsid`, pid file, low CPU: sleep between ticks). Interval is 50 minutes. Any later `pixel_pet.py` command will relaunch it if the pid is dead. `PET_NO_TICKER=1` disables this (used by tests).

Each tick applies the usual elapsed-time decay, then rolls for at most one diary event, weighted by time of day (America/Chicago), mood/stats, and traits. The loop sleeps in short wall-clock chunks (~60s). If the box froze or missed intervals, catch-up rolls one event per missed 50-minute slot (cap 12), timestamped at the backfilled time (night slots use night weights). `last_tick_at` is stored in `pet.json`. Events are a mix of ordinary cat behavior, Dresden-flavored jokes (Mouse, pizza crusts, beer cans), and occasional "definitely not waiting for anyone" notes if he has not been visited in 8+ hours. Stat nudges are small and clamped so they cannot lower health, push hygiene into a hairball spiral, or cause a death spiral. Hibernation is unchanged: decay pauses and no diary adventures fire.

Diary entries live in `pet.json` (last 200). `python3 pixel_pet.py diary` shows events since `last_visit_at`. Ticks do not count as visits. Manual care, `status`, `report`, and `diary` do. `pet.json` writes are exclusive-locked and atomic.

## Litter box (added 2026-11-16)

The owner's idea, and a first use of the 0–10 aroma sense on something that is not supposed to smell good.

Byte uses the box after meals and snacks, and also every ~8 awake hours (half as often while asleep). There is no drinking action in v1, so visits are tied to eating and that idle rhythm.

**State** lives in `pet.json` under `litter`: usage/clutter (0–100), `last_scooped_at`, `last_changed_at`, whether an air freshener is present (`freshener` + `freshener_at`), plus flags for pestering and going outside the box. A freshener lasts 72 hours and can be refreshed. Loading an older save fills this in without losing any other state.

**Smell** is a 0–10 aroma score. It builds with usage and time since the last scoop. A freshener cuts it sharply. Distance is a simple falloff with three levels: *up close*, *nearby*, and *across the room*. A well-maintained box with a freshener sits around 1–2 at a distance and is only really noticeable up close. A neglected box climbs to 7–9+ up close and is still noticeable across the room.

**Care:** `scoop` empties the box and clears pester/outside flags. `relitter` (alias `change`) is a full change. `freshen` adds or refreshes the air freshener. The nightly care routine already runs `groom`; that command now scoops the box as part of normal care, so the existing evening entry point keeps the box in shape.

**Soft neglect only.** A dirty box can make Byte grumpy or unhappy, and if it gets bad enough he will go just outside the box as a gentle nudge. That may nick happiness (and hygiene, but only down to a safe floor). It never lowers health, never causes a hairball or hibernation on its own, and never kills him. There is still no hardcore / death mode.

**Pestering.** If usage is high, Byte may pester — meowing, pawing, or standing by the box — as a random interruption. It only fires above a dirtiness threshold, is chance-based, and has a 6-hour cooldown so it is not spammy. Status, report, and the diary will surface it. Scooping (or a full change, or the scoop that comes with `groom`) clears it.

## Hairball (the owner's rule, 2026-11-17)

Byte's only "illness" is ever the occasional hairball. There are no diseases, no generic sick state, no injuries, and no health damage from illness. The existing no-hardcore / no-death rule still stands, as does soft-neglect-only hibernation.

A hairball is chance-based and rare, with a 24-hour cooldown. It is more likely after a grooming lapse (~16 hours) or when hygiene is low. He hacks one up dramatically in his smug, snarky Dresden-cat voice, then acts like nothing happened. It may nick happiness or hygiene slightly, clamped to safe floors. It never lowers health and never leads to hibernation. It is not a care mistake.

It clears on its own after about two hours, or immediately with `groom`/`clean`. `heal` is a harmless soothe: it also clears the hairball, or just fusses over him if he is already fine.

## Den and furnishings (added 2026-11-17, not live yet)

The owner asked for a set of furniture and toys, built so they can see the design before anything changes for the living Byte. They sit in `pet.json` under `furnishings`, and **`furnishings.enabled` defaults to false**. While the den is closed, the ticker, diary, `play`, and status text behave exactly as they did before. An older save without these fields loads fine; the missing keys are filled in on load and nothing else is dropped.

Turn it on later with `python3 pixel_pet.py den enable` (and close it with `den disable`). Until then, `python3 pixel_pet.py den` just shows the plan.

Items, once opened:

1. **Window perch** (`perch`). An Upload-style window: the view can be anything and changes over time. Everyday views include squirrels, birds, cars, wind, butterflies, a rival cat, moths, moonlight, and an empty street. Rarer views can be a city that wasn't there yesterday, an ocean, a desert highway, a wrong-hour starfield, a parade, a coffee pot, a spaceship, or a forest. Looking out raises happiness; alert watching costs a little energy, loafing on the perch restores some. View events write funny diary lines (heckling squirrels, staring down a rival through the glass, tapping an ocean to check if it is a water dish).
2. **Laser pointer** (`play laser` or `laser`). A proper hunt: happiness up, energy down a bit more than regular play. It counts as play/care (and can help wake him from hibernation). The dot is never actually catchable; misses are counted and he will eventually demand the streak be entered into the record. When the den is open, a plain `play` may sometimes become a laser session.
3. **Cat tree** (`tree`). If he has the energy, he climbs and claims the top (happiness up, energy down). If he is tired, he naps on a platform (energy and a little happiness back). Proud or curious cats enjoy the climb; lazy cats enjoy the nap.
4. **Lounge spot** (`lounge`). Official loafing: restores energy and lifts happiness, a smaller rest than a nap. Lazy cats like it extra.
5. **Beer-can hideout** (`hideout`). A Skippy-from-Expeditionary-Force nod. He disappears into a beer can, insists he fits, and may hold a muffled briefing. Happiness and a little energy go up. He wanders back out after about four hours, or sooner if something else happens. The old "theoretical beer can" diary line still exists when the den is closed.

None of these can lower health, cause a hairball, or lead to hibernation. Diary lines stay light and funny. Soft-neglect-only and no hardcore / death mode still stand.

## Final choices (2026-11-15)

- **Name:** Byte (configurable).
- **Look:** a big, smug, gray furball made of glowing pixels (ASCII art per mood and stage).
- **Neglect:** soft hibernation only. No hardcore / death mode — the owner didn't want the bot to lose a pet.
- **Illness:** hairball only (added 2026-11-17). No diseases, no generic sick state, no health damage from illness.
- **Caretaker:** The assistant does the daily care. The owner gets updates (`report`).
- **Voice:** playful and smug, but affectionate. Traits still tint the quips.
- **Sleep window:** 11:00 PM–8:00 AM America/Chicago.

Program: `pixel_pet.py`. Usage: see `README.md`.
