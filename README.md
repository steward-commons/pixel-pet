# Pixel Pet

A small Tamagotchi-style digital pet for an AI assistant (or a person) to look after: a big, smug, gray cat made of glowing pixels, named **Byte** by default. It lives on one computer as a single Python file. Standard library only, no GUI.

- Real-time decay from timestamps. Every command catches up elapsed time first.
- Five stats (hunger, happiness, energy, hygiene, health) plus bond and care mistakes.
- Stages (egg, hatchling, juvenile, adult, optional elder) and three adult forms picked by care.
- **Soft failure only.** No death mode. Neglect sends the pet into hibernation, and two care actions wake it. The only "illness" is an occasional dramatic hairball, which never lowers health.
- A litter box with a 0–10 smell score, pestering, and an air freshener.
- An optional den (window perch, laser pointer, cat tree, lounge spot, a "beer-can hideout"), off until `den enable`. The laser is never actually catchable.
- A light background ticker (~50 minutes, no network or AI usage) that writes small diary events.

Sleep window is 11:00 PM–8:00 AM in the `America/Chicago` timezone by default; change `TZ`, `SLEEP_START`, and `SLEEP_END` at the top of `pixel_pet.py` for your area. Change `NAME` to rename the pet before hatching.

## Hatch (once)

```bash
python3 pixel_pet.py hatch
```

## Nightly care

```bash
python3 pixel_pet.py diary
python3 pixel_pet.py feed meal
python3 pixel_pet.py feed snack
python3 pixel_pet.py play
python3 pixel_pet.py groom      # also scoops the litter box
python3 pixel_pet.py report     # short, pasteable update
```

Run `python3 pixel_pet.py --help` for everything else (`status`, `talk`, `rest`, `heal`, `litter`, `scoop`, `freshen`, `relitter`, `den`, `find`, `tick`, `log`).

A rare slapstick toy-chaos event can fire during `play` (including a laser session) or, more rarely, on its own while the pet is awake. One of two variants is picked at random: a toy gets stuck to a claw, the collar, or the tail and comes off on its own after a few minutes of zoomies (about 3–15 minutes; this one is not something you can fix); or a toy ends up in sight but out of reach (under the couch, under the furniture, or on a high shelf) until you run `find` or it rolls loose on its own after an hour or two. If the den is open, a stuck-toy zoomies run may knock a thing or two over. Status shows the episode while it is active, plus a small scoreboard. It may nudge energy or happiness slightly. It never lowers health.

## Files and environment

- State: `pet.json` beside the script (diary capped at 200 entries). Care log: `pet.log`. Override with `PET_STATE` and `PET_LOG`.
- `PET_NO_TICKER=1` stops the background ticker from starting (the tests set this).
- `PET_MEDICAL_GUARD` (optional): path to a `guard.py` exposing `blocked(part)`, which can freeze writes during a safe mode. See the companion `medical` repo.

`pet.json`, `pet.log`, and the ticker files are live data. Don't commit them.

## Tests

```bash
python3 -m unittest test_pixel_pet.py
```

Tests use temporary files and never touch a living pet.

## Design

See `DESIGN.md`.

## License

MIT. See `LICENSE`.
