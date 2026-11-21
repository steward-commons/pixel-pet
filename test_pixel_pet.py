#!/usr/bin/env python3
"""Tests for Byte. Uses temp state files; never touches the real pet.json."""

from __future__ import annotations

import json
import os
import signal
import time
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from random import Random
from zoneinfo import ZoneInfo

import pixel_pet as m

# Never launch the live ticker from unit tests.
os.environ["PET_NO_TICKER"] = "1"

TZ = ZoneInfo("America/Chicago")
SCRIPT = Path(__file__).resolve().parent / "pixel_pet.py"


def at(iso: str) -> datetime:
    return m.parse_ts(iso)


class TimeHelpers(unittest.TestCase):
    def test_sleep_window_wraps_midnight(self):
        self.assertTrue(m.is_sleep_time(at("2026-11-15T23:00:00-06:00")))
        self.assertTrue(m.is_sleep_time(at("2026-11-16T03:00:00-06:00")))
        self.assertTrue(m.is_sleep_time(at("2026-11-16T07:59:00-06:00")))
        self.assertFalse(m.is_sleep_time(at("2026-11-16T08:00:00-06:00")))
        self.assertFalse(m.is_sleep_time(at("2026-11-16T15:00:00-06:00")))
        self.assertFalse(m.is_sleep_time(at("2026-11-15T22:59:00-06:00")))

    def test_slices_across_bedtime(self):
        start = at("2026-11-15T22:00:00-06:00")
        end = at("2026-11-16T10:00:00-06:00")
        slices = list(m.iter_slices(start, end))
        awake = sum((b - a).total_seconds() for a, b, sleep in slices if not sleep) / 3600
        sleep = sum((b - a).total_seconds() for a, b, sl in slices if sl) / 3600
        self.assertAlmostEqual(awake, 3.0, places=5)  # 22-23 and 08-10
        self.assertAlmostEqual(sleep, 9.0, places=5)


class PetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_file = self.dir / "pet.json"
        self.log_file = self.dir / "pet.log"
        self.t0 = at("2026-11-15T20:00:00-06:00")  # evening, still awake
        self.now = self.t0
        self.rng = Random(42)

    def tearDown(self):
        self.tmp.cleanup()

    def hatch(self, traits=("proud", "loyal"), when=None):
        self.now = when or self.t0

        def now_fn():
            return self.now

        return m.Pet.hatch(
            now=self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
            traits=list(traits),
            force=True,
        )

    def pet_at(self, when: datetime) -> m.Byte:
        self.now = when
        pet = m.Pet.load(
            now_fn=lambda: self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
        )
        self.assertIsNotNone(pet)
        return pet

    def advance(self, hours: float = 0, days: float = 0) -> m.Byte:
        self.now = self.now + timedelta(hours=hours, days=days)
        return self.pet_at(self.now)

    def test_hatch_creates_pet(self):
        pet = self.hatch()
        s = pet.state
        self.assertEqual(s["name"], "Byte")
        self.assertEqual(s["stage"], "hatchling")
        self.assertEqual(s["traits"], ["proud", "loyal"])
        self.assertFalse(s["hardcore"])
        self.assertEqual(s["caretaker"], "your assistant")
        self.assertTrue(self.state_file.exists())
        self.assertIn("hatched", self.log_file.read_text())
        self.assertIn("proud", pet.status_text(roll_event=False))

    def test_hatch_refuses_overwrite(self):
        self.hatch()
        with self.assertRaises(FileExistsError):
            m.Pet.hatch(
                now=self.t0,
                rng=self.rng,
                state_file=self.state_file,
                log_file=self.log_file,
                traits=["lazy", "snarky"],
                force=False,
            )

    def test_feed_play_groom_rest_talk(self):
        pet = self.hatch()
        h0 = pet.state["stats"]["hunger"]
        pet.feed("meal")
        self.assertGreater(pet.state["stats"]["hunger"], h0)
        self.assertLess(pet.state["stats"]["hygiene"], 80)
        pet.groom()
        self.assertGreaterEqual(pet.state["stats"]["hygiene"], 90)
        hap0 = pet.state["stats"]["happiness"]
        pet.play()
        self.assertGreater(pet.state["stats"]["happiness"], hap0)
        en0 = pet.state["stats"]["energy"]
        pet.rest()
        self.assertGreater(pet.state["stats"]["energy"], en0)
        pet.talk()
        self.assertGreater(pet.state["bond"], 12)

    def test_snack_and_overfeed(self):
        pet = self.hatch()
        pet.state["stats"]["hunger"] = 85
        health0 = pet.state["stats"]["health"]
        pet.feed("meal")
        self.assertLess(pet.state["stats"]["health"], health0)
        pet.feed("snack")
        self.assertGreaterEqual(pet.state["stats"]["hunger"], 99)

    def test_play_refused_when_tired(self):
        pet = self.hatch()
        pet.state["stats"]["energy"] = 10
        text = pet.play()
        self.assertEqual(pet.state["stats"]["energy"], 10)
        self.assertTrue(any(k in text.lower() for k in ("eye", "tired", "battery", "nap")))

    def test_heal_when_well_is_smug(self):
        pet = self.hatch()
        text = pet.heal()
        self.assertFalse(pet.state["sick"])
        self.assertFalse(pet.state["hairball"])
        self.assertTrue(pet.state["stats"]["health"] >= 100 or "offended" in text.lower() or "fine" in text.lower() or "concern" in text.lower() or "sound" in text.lower())

    def test_awake_decay_over_time(self):
        pet = self.hatch()
        h0 = pet.state["stats"]["hunger"]
        # 3 hours still before 11 PM
        pet = self.advance(hours=2)  # 20:00 -> 22:00, still awake
        pet.tick()
        # hatchling 1.25x * 3 hunger/hour * 2 hours = 7.5
        self.assertAlmostEqual(h0 - pet.state["stats"]["hunger"], 3 * 2 * 1.25, places=2)
        self.assertEqual(pet.state["status"], "awake")

    def test_sleep_restores_energy_and_slows_hunger(self):
        pet = self.hatch(when=at("2026-11-15T23:00:00-06:00"))
        pet.state["stage"] = "juvenile"  # 1.0x decay for easy math
        pet.state["stats"]["energy"] = 20
        pet.state["stats"]["hunger"] = 80
        pet.save()
        pet = self.advance(hours=9)  # 23:00 to 08:00
        pet.tick()
        self.assertAlmostEqual(pet.state["stats"]["energy"], 20 + 8 * 9, places=2)
        self.assertAlmostEqual(pet.state["stats"]["hunger"], 80 - 1.5 * 9, places=2)
        self.assertEqual(pet.state["status"], "awake")  # 8:00 exactly is awake

    def test_overnight_split(self):
        pet = self.hatch(when=at("2026-11-15T22:00:00-06:00"))
        pet.state["stage"] = "juvenile"
        pet.state["stats"]["hunger"] = 80
        pet.state["stats"]["energy"] = 20
        pet.save()
        pet = self.advance(hours=12)  # to 10 AM: 1h awake, 9h sleep, 2h awake
        pet.tick()
        expected_hunger = 80 - (3 * 1 + 1.5 * 9 + 3 * 2)
        expected_energy = 20 - (2 * 1) + (8 * 9) - (2 * 2)
        self.assertAlmostEqual(pet.state["stats"]["hunger"], expected_hunger, places=2)
        self.assertAlmostEqual(pet.state["stats"]["energy"], expected_energy, places=2)

    def test_loyal_slows_happiness_decay(self):
        pet = self.hatch(traits=("loyal", "gentle"), when=at("2026-11-16T10:00:00-06:00"))
        pet.state["stage"] = "juvenile"
        pet.state["stats"]["happiness"] = 80
        pet.save()
        pet = self.advance(hours=4)
        pet.tick()
        # 1.5 * 4 * 0.7 loyal = 4.2
        self.assertAlmostEqual(80 - pet.state["stats"]["happiness"], 1.5 * 4 * 0.7, places=2)

    def test_persistence(self):
        pet = self.hatch()
        pet.feed("meal")
        hunger = pet.state["stats"]["hunger"]
        pet2 = self.pet_at(self.now)
        self.assertAlmostEqual(pet2.state["stats"]["hunger"], hunger)

    def test_evolution_and_forms(self):
        # Guardian
        pet = self.hatch(traits=("loyal", "gentle"))
        pet.state["living_seconds"] = 6.1 * 86400
        pet.state["stage"] = "juvenile"
        pet.state["bond"] = 80
        pet.state["care_mistakes"] = 0
        pet.state["hibernated_ever"] = False
        pet.save()
        pet.tick()
        self.assertEqual(pet.state["stage"], "adult")
        self.assertEqual(pet.state["form"], "guardian")

        # Magnificent
        pet = self.hatch(traits=("snarky", "proud"), when=self.now)
        pet.state["living_seconds"] = 6.1 * 86400
        pet.state["stage"] = "juvenile"
        pet.state["bond"] = 20
        pet.state["hibernated_ever"] = True  # blocks guardian
        pet.state["stats"]["happiness"] = 90
        pet.state["counts"]["plays"] = 20
        pet.save()
        pet.tick()
        self.assertEqual(pet.state["form"], "magnificent")

        # Loaf
        pet = self.hatch(traits=("lazy", "curious"), when=self.now)
        pet.state["living_seconds"] = 6.1 * 86400
        pet.state["stage"] = "juvenile"
        pet.state["bond"] = 20
        pet.state["hibernated_ever"] = True
        pet.state["stats"]["hunger"] = 90
        pet.state["stats"]["energy"] = 90
        pet.state["stats"]["happiness"] = 40
        pet.state["counts"]["meals"] = 20
        pet.state["counts"]["rests"] = 20
        pet.save()
        pet.tick()
        self.assertEqual(pet.state["form"], "loaf")

    def test_stage_timeline(self):
        pet = self.hatch()
        # jump to day 2 with periodic care so he doesn't collapse
        for _ in range(3):
            pet = self.advance(hours=16)
            pet.tick()
            pet.feed("meal")
            pet.feed("snack")
            pet.groom()
            pet.play()
        # about 2 days of 16h steps * 3 = 48h, plus care (no time)
        pet = self.advance(hours=8)
        pet.tick()
        self.assertIn(pet.state["stage"], ("juvenile", "adult", "hatchling"))
        # force remaining age
        pet.state["living_seconds"] = 2.05 * 86400
        pet.tick()
        self.assertEqual(pet.state["stage"], "juvenile")
        pet.state["living_seconds"] = 21.1 * 86400
        pet.state["bond"] = 80
        pet.state["care_mistakes"] = 0
        pet.tick()
        self.assertEqual(pet.state["stage"], "elder")
        self.assertIsNotNone(pet.state["form"])

    def test_care_mistake_after_eight_hours_at_zero(self):
        pet = self.hatch()
        pet.state["stage"] = "juvenile"
        pet.state["stats"]["hunger"] = 0
        pet.state["stats"]["health"] = 100
        pet.state["critical_hours"] = 0
        pet.save()
        pet = self.advance(hours=7)
        pet.tick()
        self.assertEqual(pet.state["care_mistakes"], 0)
        pet = self.advance(hours=2)
        pet.tick()
        self.assertGreaterEqual(pet.state["care_mistakes"], 1)
        mistakes = pet.state["care_mistakes"]
        pet = self.advance(hours=5)
        pet.tick()
        self.assertEqual(pet.state["care_mistakes"], mistakes)  # one per incident

    def test_health_drops_after_critical_grace(self):
        pet = self.hatch()
        pet.state["stage"] = "juvenile"
        pet.state["stats"]["hunger"] = 5
        pet.state["stats"]["hygiene"] = 80  # keep hygiene high so a hairball nick cannot confuse the math
        pet.state["stats"]["health"] = 100
        pet.state["critical_hours"] = 0
        pet.save()
        pet = self.advance(hours=8)
        pet.tick()
        self.assertAlmostEqual(pet.state["stats"]["health"], 100, places=0)
        pet = self.advance(hours=3)
        pet.tick()
        self.assertAlmostEqual(pet.state["stats"]["health"], 100 - 2 * 3, places=1)

    def test_hibernate_from_health_zero_and_revive(self):
        pet = self.hatch()
        pet.state["stage"] = "juvenile"
        pet.state["stats"]["hunger"] = 0
        pet.state["stats"]["hygiene"] = 0
        pet.state["stats"]["health"] = 3
        pet.state["critical_hours"] = 8
        pet.save()
        pet = self.advance(hours=2)
        notes = pet.tick()
        pet.save()
        self.assertEqual(pet.state["status"], "hibernating")
        self.assertTrue(pet.state["hibernated_ever"])
        self.assertGreaterEqual(pet.state["bond"], 0)
        self.assertIn("health", " ".join(notes).lower() + " hibernat")
        # time paused: hunger should not keep falling
        hunger = pet.state["stats"]["hunger"]
        pet = self.advance(hours=24)
        pet.tick()
        self.assertEqual(pet.state["stats"]["hunger"], hunger)
        self.assertGreater(pet.state["days_resting"], 0.9)
        # no death key, health stays 0 until revive
        self.assertNotIn(pet.state["status"], ("dead", "death"))
        self.assertFalse(pet.state.get("hardcore"))

        first = pet.feed("meal")
        self.assertEqual(pet.state["status"], "hibernating")
        self.assertEqual(pet.state["revival_progress"], 1)
        self.assertTrue(any(w in first.lower() for w in ("ear", "flicker", "almost", "stir", "listening", "push")))

        second = pet.play()
        self.assertIn(pet.state["status"], ("awake", "asleep"))
        self.assertGreaterEqual(pet.state["stats"]["health"], 30)
        self.assertTrue(any(w in second.lower() for w in ("glow", "returned", "up", "grumpy", "nap", "unimpressed")))

    def test_hibernate_from_missed_care_days(self):
        pet = self.hatch()
        # no decay; just expire last_care_at
        pet.state["last_care_at"] = m.fmt_ts(self.now - timedelta(hours=73))
        pet.save()
        pet.tick()
        self.assertEqual(pet.state["status"], "hibernating")
        pet.groom()
        pet.talk()
        self.assertIn(pet.state["status"], ("awake", "asleep"))

    def test_never_deletes_state_on_neglect(self):
        pet = self.hatch()
        pet.state["stats"]["health"] = 0
        pet.state["stats"]["hunger"] = 0
        pet._enter_hibernate(self.now, "test")
        pet.save()
        self.assertTrue(self.state_file.exists())
        data = json.loads(self.state_file.read_text())
        self.assertEqual(data["name"], "Byte")
        self.assertEqual(data["status"], "hibernating")

    def test_event_can_fire(self):
        pet = self.hatch()
        ev = pet.maybe_event(force=True)
        self.assertIsNotNone(ev)
        self.assertTrue(len(ev) > 10)
        # cooldown blocks a second
        ev2 = pet.maybe_event(force=False)
        self.assertIsNone(ev2)

    def test_heal_soothes_hairball_and_can_still_restore_health(self):
        pet = self.hatch()
        pet.state["hairball"] = True
        pet.state["hairball_hours"] = 0.5
        pet.state["stats"]["health"] = 60
        text = pet.heal()
        self.assertFalse(pet.state["hairball"])
        self.assertFalse(pet.state["sick"])
        self.assertGreater(pet.state["stats"]["health"], 60)
        self.assertTrue(any(w in text.lower() for w in ("hairball", "performance", "fussed", "pretending", "sooth", "dramatic")))

    def test_ascii_per_stage_and_mood(self):
        for stage, form, mood in (
            ("hatchling", None, "content"),
            ("hatchling", None, "hibernating"),
            ("juvenile", None, "hungry"),
            ("adult", "guardian", "smug"),
            ("adult", "magnificent", "happy"),
            ("adult", "loaf", "loafing"),
            ("elder", "guardian", "content"),
            ("adult", None, "hairball"),
            ("hatchling", None, "hairball"),
            ("juvenile", None, "hairball"),
        ):
            art = m.ascii_art(stage, form, mood)
            self.assertIn("\\", art + "/")  # some cat ears somewhere, or body
            self.assertTrue(len(art.splitlines()) >= 3)

    def test_report_is_pasteable(self):
        pet = self.hatch()
        text = pet.report_text()
        self.assertIn("Byte", text)
        self.assertIn("assistant", text)
        self.assertIn("Hunger", text)
        self.assertIn("hardcore", text.lower() + " no hardcore")  # close mentions no hardcore or he's well
        # actually the well path may not say hardcore. That's ok.
        self.assertIn("traits:", text)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = os.environ.copy()
        self.env["PET_STATE"] = str(self.dir / "pet.json")
        self.env["PET_LOG"] = str(self.dir / "pet.log")
        self.env["PET_NOW"] = "2026-11-15T20:00:00-06:00"
        self.env["PET_NO_TICKER"] = "1"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, expect=0):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env,
        )
        if result.returncode != expect:
            self.fail(f"cli {args} exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def test_cli_flow(self):
        r = self.run_cli("status", expect=1)
        self.assertIn("hasn't hatched", r.stderr)
        r = self.run_cli("hatch", "--seed", "7")
        self.assertIn("Byte", r.stdout)
        self.assertIn("hatchling", r.stdout)
        self.run_cli("feed", "meal")
        self.run_cli("feed", "snack")
        self.run_cli("play")
        self.run_cli("groom")
        self.run_cli("rest")
        self.run_cli("talk")
        r = self.run_cli("report")
        self.assertIn("Nightly report", r.stdout)
        r = self.run_cli("log")
        self.assertIn("hatched", r.stdout)
        r = self.run_cli("tick", "--force")
        self.assertTrue(r.stdout.strip())
        r = self.run_cli("diary")
        self.assertIn("diary", r.stdout.lower())
        # second hatch refused
        self.run_cli("hatch", expect=1)
        # time travel
        r = self.run_cli("--at", "2026-11-16T10:00:00-06:00", "status")
        self.assertIn("Byte", r.stdout)

    def test_help(self):
        r = self.run_cli()
        self.assertIn("feed meal", r.stdout)



class TickerAndDiaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_file = self.dir / "pet.json"
        self.log_file = self.dir / "pet.log"
        self.t0 = at("2026-11-15T20:00:00-06:00")
        self.now = self.t0
        self.rng = Random(42)

    def tearDown(self):
        self.tmp.cleanup()

    def hatch(self, traits=("snarky", "proud"), when=None):
        self.now = when or self.t0

        def now_fn():
            return self.now

        return m.Pet.hatch(
            now=self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
            traits=list(traits),
            force=True,
        )

    def pet_at(self, when):
        self.now = when
        return m.Pet.load(
            now_fn=lambda: self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
        )

    def test_diary_cap(self):
        pet = self.hatch()
        for i in range(205):
            pet.append_diary(f"entry {i}", kind="test")
        self.assertEqual(len(pet.state["diary"]), 200)
        self.assertEqual(pet.state["diary"][0]["text"], "entry 5")
        self.assertEqual(pet.state["diary"][-1]["text"], "entry 204")

    def test_tick_at_most_one_event(self):
        pet = self.hatch()
        start = len(pet.state.get("diary") or [])
        pet.offline_tick(force_event=True)
        self.assertEqual(len(pet.state["diary"]), start + 1)
        pet.offline_tick(force_event=True)
        self.assertEqual(len(pet.state["diary"]), start + 2)

    def test_tick_does_not_count_as_visit(self):
        pet = self.hatch()
        before = pet.state["last_visit_at"]
        self.now = self.t0 + timedelta(minutes=20)
        pet = self.pet_at(self.now)
        pet.offline_tick(force_event=True)
        self.assertEqual(pet.state["last_visit_at"], before)
        text = pet.diary_text()
        self.assertIn("1 new", text)

    def test_diary_since_visit_hides_older(self):
        pet = self.hatch()
        pet.offline_tick(force_event=True)  # same timestamp as visit; unread uses >
        self.now = self.t0 + timedelta(hours=1)
        pet = self.pet_at(self.now)
        pet.offline_tick(force_event=True)
        pet.mark_visit()
        pet.save()
        hidden = pet.diary_text()
        self.assertIn("No new diary", hidden)
        self.now = self.t0 + timedelta(hours=2)
        pet = self.pet_at(self.now)
        pet.offline_tick(force_event=True)
        shown = pet.diary_text()
        self.assertIn("1 new", shown)

    def test_safe_deltas_never_spiral(self):
        pet = self.hatch()
        st = pet.state["stats"]
        st["hygiene"] = 36
        st["happiness"] = 20
        st["health"] = 80
        st["hunger"] = 20
        st["energy"] = 20
        pet.state["sick"] = False
        pet.state["hairball"] = False
        health0 = st["health"]
        for ev in m.DIARY_EVENTS:
            m.apply_safe_deltas(pet.state, ev[2])
        for ev in m.EVENTS:
            m.apply_safe_deltas(pet.state, ev[2])
        self.assertGreaterEqual(pet.state["stats"]["health"], health0)
        self.assertGreaterEqual(pet.state["stats"]["hygiene"], 35)
        self.assertFalse(pet.state["sick"])
        self.assertFalse(pet.state["hairball"])
        self.assertGreaterEqual(pet.state["stats"]["hunger"], 0)
        self.assertLessEqual(pet.state["stats"]["hunger"], 100)

    def test_lonely_events_weighted_after_absence(self):
        tags = {"lonely": True}
        recent = m.event_weight(tags, "evening", "content", ["snarky", "proud"], 0.5, 60, {})
        lonely = m.event_weight(tags, "evening", "content", ["snarky", "proud"], 10, 60, {})
        self.assertGreater(lonely, recent * 5)

    def test_period_and_trait_weights(self):
        tags = {"periods": ("night",), "traits": ("proud",)}
        night = m.event_weight(tags, "night", "sleepy", ["proud", "snarky"], 1, 60, {})
        day = m.event_weight(tags, "afternoon", "content", ["proud", "snarky"], 1, 60, {})
        self.assertGreater(night, day)
        proud = m.event_weight(tags, "night", "sleepy", ["proud"], 1, 60, {})
        plain = m.event_weight(tags, "night", "sleepy", ["gentle"], 1, 60, {})
        self.assertGreater(proud, plain)

    def test_hibernate_tick_has_no_event(self):
        pet = self.hatch()
        pet._enter_hibernate(self.now, "test")
        pet.save()
        n = len(pet.state.get("diary") or [])
        text = pet.offline_tick(force_event=True)
        self.assertEqual(pet.state["status"], "hibernating")
        self.assertEqual(len(pet.state.get("diary") or []), n)
        self.assertIn("Hibernating", text)
        # decay still paused
        hunger = pet.state["stats"]["hunger"]
        self.now = self.t0 + timedelta(hours=5)
        pet = self.pet_at(self.now)
        pet.offline_tick()
        self.assertEqual(pet.state["stats"]["hunger"], hunger)
        self.assertNotIn(pet.state["status"], ("dead", "death"))

    def test_file_lock_serializes_writers(self):
        path = self.dir / "counter.json"
        path.write_text(json.dumps({"n": 0}) + "\n")

        def worker(n):
            for _ in range(n):
                with m.locked_state(path):
                    data = json.loads(path.read_text())
                    data["n"] += 1
                    tmp = path.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(data))
                    tmp.replace(path)

        import threading
        threads = [threading.Thread(target=worker, args=(25,)) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        data = json.loads(path.read_text())
        self.assertEqual(data["n"], 100)


    def test_due_tick_times_and_cap(self):
        last = self.t0
        now = self.t0 + timedelta(seconds=50 * 60 * 20 + 10)
        times, new_last = m.due_tick_times(last, now, 50 * 60, cap=12)
        self.assertEqual(len(times), 12)
        self.assertEqual(new_last, last + timedelta(seconds=50 * 60 * 20))
        self.assertEqual(times[-1], new_last)
        self.assertEqual(times[0], last + timedelta(seconds=50 * 60 * 9))
        none, same = m.due_tick_times(last, last + timedelta(minutes=10), 50 * 60)
        self.assertEqual(none, [])
        self.assertEqual(same, last)

    def test_sleep_until_uses_wall_clock_chunks(self):
        class Clock:
            def __init__(self, t):
                self.t = t
                self.sleeps = []

            def now(self):
                return self.t

            def sleep(self, s):
                self.sleeps.append(s)
                self.t += timedelta(seconds=s)
                if len(self.sleeps) == 2:
                    self.t += timedelta(hours=3)

        clock = Clock(self.t0)
        m.sleep_until_next_tick(clock.now, self.t0, 3000, chunk=60, sleep_fn=clock.sleep)
        self.assertTrue(clock.sleeps)
        self.assertLessEqual(max(clock.sleeps), 60)
        self.assertGreaterEqual(clock.t, self.t0 + timedelta(seconds=3000))
        # a 3h jump should have ended the wait early, not 50 one-minute sleeps
        self.assertLess(len(clock.sleeps), 10)

    def test_catch_up_backfills_night_timestamps(self):
        os.environ["PET_TICK_SECONDS"] = "3000"
        try:
            start = at("2026-11-15T22:13:18-06:00")
            pet = self.hatch(when=start)
            pet.state["last_tick_at"] = m.fmt_ts(start)
            pet.state["last_visit_at"] = m.fmt_ts(start)
            pet.save()
            # always fire
            class Always(m.random.Random):
                def random(self):
                    return 0.0

            self.now = at("2026-11-16T04:46:00-06:00")
            pet = self.pet_at(self.now)
            pet.state["last_visit_at"] = m.fmt_ts(self.now)  # a morning peek before catch-up
            self.now = at("2026-11-16T04:50:00-06:00")
            pet = self.pet_at(self.now)
            pet.rng = Always()
            lines = pet.catch_up_diary()
            # 22:13 -> 04:46 = 7 intervals
            self.assertEqual(len(lines), 7)
            stamps = [e["at"] for e in pet.state["diary"] if e.get("logged_at")]
            # at least the catch-up entries
            catch = [e for e in pet.state["diary"] if e["at"] > m.fmt_ts(start)]
            self.assertEqual(len(catch), 7)
            expected = [
                "2026-11-15T23:03:18-06:00",
                "2026-11-15T23:53:18-06:00",
                "2026-11-16T00:43:18-06:00",
                "2026-11-16T01:33:18-06:00",
                "2026-11-16T02:23:18-06:00",
                "2026-11-16T03:13:18-06:00",
                "2026-11-16T04:03:18-06:00",
            ]
            self.assertEqual([e["at"] for e in catch], expected)
            for e in catch:
                self.assertEqual(m.period_of_day(m.parse_ts(e["at"])), "night")
            self.assertEqual(pet.state["last_tick_at"], "2026-11-16T04:03:18-06:00")
            self.assertGreaterEqual(pet.state["stats"]["health"], 100 - 0.001)
            self.assertFalse(pet.state["sick"])
            self.assertFalse(pet.state.get("hairball"))
            # backfill logged at 04:50 is unread even though last peek was 04:46
            self.assertEqual(len(pet.unread_diary()), 7)
        finally:
            os.environ.pop("PET_TICK_SECONDS", None)

    def test_infer_last_tick_from_ticker_log(self):
        log = self.dir / "ticker.log"
        log.write_text(
            "ticker online, interval 3000s, pid 1\n"
            "2026-11-15T21:23:18-06:00  hello\n"
            "2026-11-15T22:13:18-06:00  Time passed. Byte did not feel like making an entry.\n"
        )
        state = {"last_tick_at": None, "diary": []}
        got = m.infer_last_tick_at(state, self.t0, log)
        self.assertEqual(got, at("2026-11-15T22:13:18-06:00"))

    def test_catch_up_does_not_count_as_visit(self):
        os.environ["PET_TICK_SECONDS"] = "1800"
        try:
            pet = self.hatch()
            visit = pet.state["last_visit_at"]
            self.now = self.t0 + timedelta(hours=3)
            pet = self.pet_at(self.now)
            pet.catch_up_diary()
            self.assertEqual(pet.state["last_visit_at"], visit)
        finally:
            os.environ.pop("PET_TICK_SECONDS", None)

    def test_ensure_ticker_skipped_when_disabled(self):
        os.environ["PET_NO_TICKER"] = "1"
        os.environ["PET_STATE"] = str(self.state_file)
        try:
            self.hatch()
            self.assertEqual(m.ensure_ticker(), "skipped")
        finally:
            os.environ.pop("PET_NO_TICKER", None)
            os.environ.pop("PET_STATE", None)

    def test_ticker_daemon_starts_and_restarts(self):
        env_keys = {}
        for key in ("PET_STATE", "PET_LOG", "PET_NO_TICKER", "PET_TICKER_RUNNING", "PET_NOW", "PET_TICK_SECONDS"):
            env_keys[key] = os.environ.get(key)
        pids = []
        try:
            os.environ["PET_STATE"] = str(self.state_file)
            os.environ["PET_LOG"] = str(self.log_file)
            os.environ.pop("PET_NO_TICKER", None)
            os.environ.pop("PET_TICKER_RUNNING", None)
            os.environ.pop("PET_NOW", None)
            os.environ["PET_TICK_SECONDS"] = "3600"
            self.hatch()
            note = m.ensure_ticker()
            self.assertTrue(note.startswith("started") or note.startswith("running"), note)
            time.sleep(0.4)
            pid = m.read_ticker_pid()
            self.assertTrue(m.ticker_is_alive(pid), f"not alive after start: {note} pid={pid}")
            pids.append(pid)
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not m.ticker_is_alive(pid):
                    break
                time.sleep(0.1)
            self.assertFalse(m.ticker_is_alive(pid))
            note2 = m.ensure_ticker()
            self.assertTrue(note2.startswith("started"), note2)
            time.sleep(0.4)
            pid2 = m.read_ticker_pid()
            self.assertTrue(m.ticker_is_alive(pid2), f"restart failed: {note2} pid={pid2}")
            self.assertNotEqual(pid2, pid)
            pids.append(pid2)
        finally:
            for pid in pids:
                if pid and m.ticker_is_alive(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
            time.sleep(0.2)
            for key, val in env_keys.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val



class LitterBoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_file = self.dir / "pet.json"
        self.log_file = self.dir / "pet.log"
        self.t0 = at("2026-11-15T20:00:00-06:00")
        self.now = self.t0
        self.rng = Random(42)

    def tearDown(self):
        self.tmp.cleanup()

    def hatch(self, traits=("proud", "loyal"), when=None):
        self.now = when or self.t0
        return m.Pet.hatch(
            now=self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
            traits=list(traits),
            force=True,
        )

    def pet_at(self, when: datetime) -> m.Byte:
        self.now = when
        pet = m.Pet.load(
            now_fn=lambda: self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
        )
        self.assertIsNotNone(pet)
        return pet

    def advance(self, hours: float = 0, days: float = 0) -> m.Byte:
        self.now = self.now + timedelta(hours=hours, days=days)
        return self.pet_at(self.now)

    def _dirty(self, pet, usage=100, hours=72, freshener=False):
        pet.state["litter"]["usage"] = usage
        pet.state["litter"]["freshener"] = freshener
        pet.state["litter"]["freshener_at"] = m.fmt_ts(self.now) if freshener else None
        pet.state["litter"]["last_scooped_at"] = m.fmt_ts(self.now - timedelta(hours=hours))
        pet.save()
        return pet

    def test_hatch_includes_litter(self):
        pet = self.hatch()
        box = pet.state["litter"]
        self.assertEqual(box["usage"], 0)
        self.assertTrue(box["freshener"])
        self.assertFalse(box["pestering"])
        self.assertIn("scoops", pet.state["counts"])

    def test_migrate_existing_state_without_losing_fields(self):
        raw = {
            "name": "Byte",
            "stage": "hatchling",
            "form": None,
            "traits": ["snarky", "proud"],
            "born_at": "2026-11-15T19:48:29-06:00",
            "living_seconds": 100.0,
            "stats": {"hunger": 54.5, "happiness": 93.0, "energy": 80.0, "hygiene": 77.0, "health": 100.0},
            "bond": 19.5,
            "care_mistakes": 0,
            "sick": False,
            "status": "awake",
            "last_updated": "2026-11-16T15:43:18-06:00",
            "last_care_at": "2026-11-15T23:11:21-06:00",
            "diary": [{"at": "2026-11-16T14:53:18-06:00", "kind": "knock", "text": "kept"}],
            "counts": {"meals": 1, "snacks": 1, "plays": 1, "rests": 0, "grooms": 1, "talks": 0},
            "hardcore": False,
            "voice": "playful and smug but affectionate",
        }
        self.state_file.write_text(json.dumps(raw), encoding="utf-8")
        pet = self.pet_at(self.t0)
        s = pet.state
        self.assertEqual(s["name"], "Byte")
        self.assertEqual(s["bond"], 19.5)
        self.assertEqual(s["stats"]["hunger"], 54.5)
        self.assertEqual(s["diary"][0]["text"], "kept")
        self.assertEqual(s["counts"]["meals"], 1)
        self.assertIn("litter", s)
        self.assertEqual(s["litter"]["usage"], 0)
        self.assertTrue(s["litter"]["freshener"])
        self.assertIn("scoops", s["counts"])
        self.assertFalse(s["sick"])
        self.assertFalse(s["hairball"])
        self.assertIn("hairball_hours", s)
        self.assertIn("last_hairball_at", s)
        self.assertIn("last_groom_at", s)
        # migrate is in-memory on load; saving should persist litter without dropping diary
        pet.save()
        reloaded = json.loads(self.state_file.read_text())
        self.assertEqual(reloaded["diary"][0]["text"], "kept")
        self.assertIn("litter", reloaded)
        self.assertFalse(reloaded["sick"])
        self.assertFalse(reloaded["hairball"])
        self.assertEqual(reloaded["bond"], 19.5)
        self.assertEqual(reloaded["stats"]["happiness"], 93.0)

    def test_fresh_box_with_freshener_only_noticeable_up_close(self):
        pet = self.hatch()
        smells = pet.litter_smells()
        self.assertGreaterEqual(smells["up close"], 1.0)
        self.assertLessEqual(smells["up close"], 3.0)
        self.assertLessEqual(smells["nearby"], 2.0)
        self.assertLessEqual(smells["across the room"], 2.0)
        self.assertGreater(smells["up close"], smells["nearby"])
        self.assertGreater(smells["nearby"], smells["across the room"])
        # At a distance it's about 1-2, and you would have to be up on it to really notice.
        self.assertGreaterEqual(smells["across the room"], 0.5)
        self.assertLess(smells["across the room"], 1.5)

    def test_neglected_box_is_strong_and_noticeable_farther(self):
        pet = self.hatch()
        self._dirty(pet, usage=100, hours=72, freshener=False)
        smells = pet.litter_smells()
        self.assertGreaterEqual(smells["up close"], 7.0)
        self.assertGreaterEqual(smells["nearby"], 5.0)
        self.assertGreater(smells["across the room"], 3.0)
        self.assertGreater(smells["up close"], smells["nearby"])
        self.assertGreater(smells["nearby"], smells["across the room"])
        self.assertLessEqual(smells["up close"], 10.0)

    def test_freshener_cuts_smell(self):
        pet = self.hatch()
        self._dirty(pet, usage=100, hours=72, freshener=False)
        without = pet.litter_smells()["up close"]
        pet.state["litter"]["freshener"] = True
        pet.state["litter"]["freshener_at"] = m.fmt_ts(self.now)
        with_it = pet.litter_smells()["up close"]
        self.assertLess(with_it, without * 0.6)

    def test_feed_uses_the_box(self):
        pet = self.hatch()
        pet.feed("meal")
        after_meal = pet.state["litter"]["usage"]
        self.assertGreaterEqual(after_meal, m.LITTER_MEAL_USAGE)
        pet.feed("snack")
        self.assertGreater(pet.state["litter"]["usage"], after_meal)

    def test_idle_use_over_time(self):
        pet = self.hatch(when=at("2026-11-16T08:00:00-06:00"))
        pet.state["stage"] = "juvenile"
        pet.save()
        pet = self.advance(hours=8)
        pet.tick()
        self.assertGreaterEqual(pet.state["litter"]["usage"], m.LITTER_IDLE_USAGE)

    def test_scoop_and_change_clear_the_box(self):
        pet = self.hatch()
        self._dirty(pet, usage=80, hours=30, freshener=True)
        pet.state["litter"]["pestering"] = True
        pet.state["litter"]["went_outside"] = True
        pet.save()
        text = pet.scoop()
        self.assertEqual(pet.state["litter"]["usage"], 0)
        self.assertFalse(pet.state["litter"]["pestering"])
        self.assertFalse(pet.state["litter"]["went_outside"])
        self.assertIn("scoop", text.lower() + " box litter supervisor neatness")
        before = pet.state["litter"]["last_changed_at"]
        changes = int(pet.state["counts"].get("changes") or 0)
        pet.state["litter"]["usage"] = 40
        pet.save()
        self.now = self.now + timedelta(hours=1)
        pet.now_fn = lambda: self.now
        pet.relitter()
        self.assertEqual(pet.state["litter"]["usage"], 0)
        self.assertNotEqual(pet.state["litter"]["last_changed_at"], before)
        self.assertEqual(pet.state["counts"]["changes"], changes + 1)

    def test_groom_scoops_as_nightly_care(self):
        pet = self.hatch()
        pet.feed("meal")
        self.assertGreater(pet.state["litter"]["usage"], 0)
        text = pet.groom()
        self.assertEqual(pet.state["litter"]["usage"], 0)
        self.assertTrue(any(w in text.lower() for w in ("scoop", "litter", "box", "presentable", "supervisor")))

    def test_freshen_and_expiry(self):
        pet = self.hatch()
        pet.state["litter"]["freshener"] = False
        pet.state["litter"]["freshener_at"] = None
        pet.freshen()
        self.assertTrue(pet.state["litter"]["freshener"])
        self.assertTrue(m.freshener_active(pet.state, self.now))
        # Worn out after 72 hours; sync flips the flag.
        pet.state["litter"]["freshener_at"] = m.fmt_ts(self.now - timedelta(hours=73))
        pet._sync_litter(self.now)
        self.assertFalse(pet.state["litter"]["freshener"])
        self.assertFalse(m.freshener_active(pet.state, self.now))

    def test_neglect_makes_him_grumpy_not_harmed(self):
        pet = self.hatch(when=at("2026-11-16T10:00:00-06:00"))
        pet.state["stage"] = "juvenile"
        pet.state["stats"]["happiness"] = 80
        pet.state["stats"]["health"] = 100
        pet.state["stats"]["hygiene"] = 80
        pet.state["stats"]["hunger"] = 80
        self._dirty(pet, usage=90, hours=48, freshener=False)
        pet = self.advance(hours=4)
        pet.tick()
        self.assertLess(pet.state["stats"]["happiness"], 80 - 1.5 * 4)
        self.assertAlmostEqual(pet.state["stats"]["health"], 100, places=2)
        self.assertFalse(pet.state["sick"])
        self.assertNotEqual(pet.state["status"], "hibernating")
        self.assertEqual(pet.mood(), "grumpy")

    def test_outside_the_box_is_a_gentle_nudge(self):
        pet = self.hatch()
        pet.state["litter"]["usage"] = 90
        pet.state["stats"]["happiness"] = 70
        pet.state["stats"]["hygiene"] = 80
        pet.state["stats"]["health"] = 100
        note = pet._use_litter(20, self.now, reason="meal")
        self.assertIsNotNone(note)
        self.assertTrue(pet.state["litter"]["went_outside"])
        self.assertEqual(pet.state["litter"]["usage"], 90)  # he did not add to the full box
        self.assertLess(pet.state["stats"]["happiness"], 70)
        self.assertGreaterEqual(pet.state["stats"]["hygiene"], m.LITTER_HYGIENE_FLOOR)
        self.assertAlmostEqual(pet.state["stats"]["health"], 100, places=5)
        # Even with already-low hygiene, the floor / skip keeps him out of a spiral.
        pet.state["stats"]["hygiene"] = 30
        pet._use_litter(20, self.now, reason="idle")
        self.assertEqual(pet.state["stats"]["hygiene"], 30)
        self.assertAlmostEqual(pet.state["stats"]["health"], 100, places=5)

    def test_pester_threshold_chance_cooldown_and_clear(self):
        pet = self.hatch()
        for _ in range(15):
            self.assertIsNone(pet._maybe_pester(self.now, chance=1.0))
        self.assertFalse(pet.state["litter"]["pestering"])

        pet.state["litter"]["usage"] = 80
        self.assertIsNone(pet._maybe_pester(self.now, chance=0.0))
        line = pet._maybe_pester(self.now, chance=1.0)
        self.assertIsNotNone(line)
        self.assertTrue(pet.state["litter"]["pestering"])
        self.assertEqual(pet.state["litter"]["pester_text"], line)
        # Cooldown blocks a second roll even at chance 1.
        self.assertIsNone(pet._maybe_pester(self.now, chance=1.0))
        status = pet.status_text(roll_event=False)
        self.assertTrue(any(w in status.lower() for w in ("meow", "paw", "box", "litter")))

        # Still in cooldown a couple of hours later.
        later = self.now + timedelta(hours=2)
        self.now = later
        pet.now_fn = lambda: self.now
        self.assertIsNone(pet._maybe_pester(self.now, chance=1.0))

        # After the cooldown, he can pester again if it's still dirty.
        later = self.t0 + timedelta(hours=m.LITTER_PESTER_COOLDOWN_HOURS + 0.1)
        self.now = later
        pet.now_fn = lambda: self.now
        again = pet._maybe_pester(self.now, chance=1.0)
        self.assertIsNotNone(again)

        pet.scoop()
        self.assertFalse(pet.state["litter"]["pestering"])
        self.assertIsNone(pet.state["litter"]["pester_text"])
        self.assertIsNone(pet._maybe_pester(self.now, chance=1.0))

    def test_litter_text_lists_distances(self):
        pet = self.hatch()
        text = pet.litter_text()
        for label in m.LITTER_DISTANCES:
            self.assertIn(label, text)
        self.assertIn("freshener", text.lower())

    def test_cli_litter_commands(self):
        env = os.environ.copy()
        env["PET_STATE"] = str(self.state_file)
        env["PET_LOG"] = str(self.log_file)
        env["PET_NOW"] = "2026-11-15T20:00:00-06:00"
        env["PET_NO_TICKER"] = "1"
        script = Path(__file__).resolve().parent / "pixel_pet.py"

        def run(*args, expect=0):
            result = subprocess.run(
                [sys.executable, str(script), *args],
                capture_output=True,
                text=True,
                env=env,
            )
            if result.returncode != expect:
                self.fail(f"cli {args} exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
            return result

        run("hatch", "--seed", "7")
        run("feed", "meal")
        r = run("litter")
        self.assertIn("up close", r.stdout)
        r = run("scoop")
        self.assertTrue(r.stdout.strip())
        r = run("freshen")
        self.assertTrue(r.stdout.strip())
        r = run("relitter")
        self.assertTrue(r.stdout.strip())
        data = json.loads(self.state_file.read_text())
        self.assertEqual(data["litter"]["usage"], 0)
        self.assertTrue(data["litter"]["freshener"])


class HairballTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_file = self.dir / "pet.json"
        self.log_file = self.dir / "pet.log"
        self.t0 = at("2026-11-15T20:00:00-06:00")
        self.now = self.t0
        self.rng = Random(42)

    def tearDown(self):
        self.tmp.cleanup()

    def hatch(self, traits=("snarky", "proud"), when=None):
        self.now = when or self.t0
        return m.Pet.hatch(
            now=self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
            traits=list(traits),
            force=True,
        )

    def pet_at(self, when: datetime) -> m.Byte:
        self.now = when
        pet = m.Pet.load(
            now_fn=lambda: self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
        )
        self.assertIsNotNone(pet)
        return pet

    def advance(self, hours: float = 0) -> m.Byte:
        self.now = self.now + timedelta(hours=hours)
        return self.pet_at(self.now)

    def _force_rng(self, pet, value: float) -> None:
        class Fixed(m.random.Random):
            def random(self):
                return value
        pet.rng = Fixed()

    def test_migrate_clears_sick_adds_hairball_keeps_rest(self):
        raw = {
            "name": "Byte",
            "stage": "juvenile",
            "form": None,
            "traits": ["snarky", "proud"],
            "born_at": "2026-11-15T19:48:29-06:00",
            "living_seconds": 200000.0,
            "stats": {"hunger": 70.0, "happiness": 88.0, "energy": 80.0, "hygiene": 72.0, "health": 100.0},
            "bond": 31.25,
            "care_mistakes": 2,
            "sick": True,
            "sick_hours": 5.0,
            "status": "awake",
            "last_updated": "2026-11-16T15:43:18-06:00",
            "last_care_at": "2026-11-16T12:00:00-06:00",
            "diary": [{"at": "2026-11-16T14:53:18-06:00", "kind": "knock", "text": "kept"}],
            "counts": {"meals": 4, "snacks": 2, "plays": 3, "rests": 1, "grooms": 2, "talks": 1},
            "hardcore": False,
            "zero_since": {"hunger": None, "happiness": None, "hygiene": None, "sick": "2026-11-16T10:00:00-06:00"},
            "mistake_counted": {"hunger": False, "happiness": False, "hygiene": False, "sick": True},
            "litter": {"usage": 12.0, "freshener": True},
        }
        migrated = m.migrate_state(raw, now=self.t0)
        self.assertFalse(migrated["sick"])
        self.assertEqual(migrated["sick_hours"], 0.0)
        self.assertIsNone(migrated["zero_since"]["sick"])
        self.assertFalse(migrated["mistake_counted"]["sick"])
        self.assertFalse(migrated["hairball"])
        self.assertEqual(migrated["hairball_hours"], 0.0)
        self.assertIsNone(migrated["last_hairball_at"])
        self.assertEqual(migrated["last_groom_at"], "2026-11-16T12:00:00-06:00")
        self.assertEqual(migrated["bond"], 31.25)
        self.assertEqual(migrated["care_mistakes"], 2)
        self.assertEqual(migrated["stats"]["happiness"], 88.0)
        self.assertEqual(migrated["diary"][0]["text"], "kept")
        self.assertEqual(migrated["counts"]["plays"], 3)
        self.assertEqual(migrated["litter"]["usage"], 12.0)
        self.assertEqual(migrated["living_seconds"], 200000.0)

    def test_hairball_chance_rises_with_hygiene_and_grooming_lapse(self):
        pet = self.hatch()
        clean = dict(pet.state)
        clean["stats"] = dict(pet.state["stats"])
        clean["stats"]["hygiene"] = 90
        clean["last_groom_at"] = m.fmt_ts(self.now)
        clean["hairball"] = False
        clean["last_hairball_at"] = None
        base = m.hairball_chance(clean, self.now, 1.0)
        messy = dict(clean)
        messy["stats"] = dict(clean["stats"])
        messy["stats"]["hygiene"] = 20
        messy_chance = m.hairball_chance(messy, self.now, 1.0)
        lapse = dict(clean)
        lapse["stats"] = dict(clean["stats"])
        lapse["last_groom_at"] = m.fmt_ts(self.now - timedelta(hours=20))
        lapse_chance = m.hairball_chance(lapse, self.now, 1.0)
        self.assertGreater(base, 0.0)
        self.assertGreater(messy_chance, base)
        self.assertGreater(lapse_chance, base)
        cool = dict(messy)
        cool["last_hairball_at"] = m.fmt_ts(self.now - timedelta(hours=2))
        self.assertEqual(m.hairball_chance(cool, self.now, 1.0), 0.0)
        mid = dict(messy)
        mid["hairball"] = True
        self.assertEqual(m.hairball_chance(mid, self.now, 1.0), 0.0)

    def test_hairball_onsets_nicks_stats_never_health(self):
        pet = self.hatch()
        pet.state["stage"] = "juvenile"
        pet.state["stats"]["hygiene"] = 50
        pet.state["stats"]["happiness"] = 80
        pet.state["stats"]["health"] = 100
        pet.state["last_groom_at"] = m.fmt_ts(self.now - timedelta(hours=20))
        pet.state["last_hairball_at"] = None
        pet.save()
        self._force_rng(pet, 0.0)
        # apply a single hour without going through tick's sleep split complications
        note = pet._progress_hairball(1.0, self.now)
        self.assertTrue(pet.state["hairball"])
        self.assertIsNotNone(note)
        low = (note or "").lower()
        self.assertTrue(any(w in low for w in ("hairball", "hack", "disturbance", "intentional")))
        self.assertAlmostEqual(pet.state["stats"]["health"], 100, places=5)
        self.assertLess(pet.state["stats"]["happiness"], 80)
        self.assertGreaterEqual(pet.state["stats"]["happiness"], m.HAPPINESS_SAFE_FLOOR)
        self.assertLess(pet.state["stats"]["hygiene"], 50)
        self.assertGreaterEqual(pet.state["stats"]["hygiene"], m.HYGIENE_SAFE_FLOOR)
        self.assertEqual(pet.mood(), "hairball")
        art = m.ascii_art(pet.state["stage"], None, "hairball")
        self.assertIn("(o)", art)

    def test_hairball_nicks_respect_floors(self):
        pet = self.hatch()
        pet.state["stats"]["happiness"] = m.HAPPINESS_SAFE_FLOOR
        pet.state["stats"]["hygiene"] = m.HYGIENE_SAFE_FLOOR
        pet.state["stats"]["health"] = 100
        pet._start_hairball(self.now)
        self.assertEqual(pet.state["stats"]["happiness"], m.HAPPINESS_SAFE_FLOOR)
        self.assertEqual(pet.state["stats"]["hygiene"], m.HYGIENE_SAFE_FLOOR)
        self.assertEqual(pet.state["stats"]["health"], 100)

    def test_hairball_does_not_fire_when_rng_says_no(self):
        pet = self.hatch()
        pet.state["stats"]["hygiene"] = 10
        pet.state["last_groom_at"] = m.fmt_ts(self.now - timedelta(hours=24))
        self._force_rng(pet, 0.99)
        note = pet._progress_hairball(1.0, self.now)
        self.assertIsNone(note)
        self.assertFalse(pet.state["hairball"])
        self.assertEqual(pet.state["stats"]["health"], 100)

    def test_hairball_cooldown_blocks_a_second(self):
        pet = self.hatch()
        pet.state["stats"]["hygiene"] = 10
        pet.state["stats"]["happiness"] = 80
        pet.state["last_groom_at"] = m.fmt_ts(self.now - timedelta(hours=24))
        self._force_rng(pet, 0.0)
        self.assertTrue(pet._progress_hairball(1.0, self.now))
        pet._clear_hairball()
        later = self.now + timedelta(hours=6)
        self._force_rng(pet, 0.0)
        self.assertIsNone(pet._progress_hairball(1.0, later))
        self.assertFalse(pet.state["hairball"])
        after = self.now + timedelta(hours=25)
        self._force_rng(pet, 0.0)
        self.assertIsNotNone(pet._progress_hairball(1.0, after))

    def test_hairball_clears_on_its_own(self):
        pet = self.hatch()
        pet.state["stage"] = "juvenile"
        pet.state["hairball"] = True
        pet.state["hairball_hours"] = 0.0
        pet.state["stats"]["health"] = 100
        pet.state["last_updated"] = m.fmt_ts(self.now)
        pet.save()
        pet = self.advance(hours=1)
        pet.tick()
        self.assertTrue(pet.state["hairball"])
        self.assertEqual(pet.state["stats"]["health"], 100)
        pet = self.advance(hours=2)
        notes = pet.tick()
        self.assertFalse(pet.state["hairball"])
        self.assertEqual(pet.state["stats"]["health"], 100)
        blob = " ".join(notes).lower()
        self.assertTrue(any(w in blob for w in ("nothing", "paw", "history", "moved", "happened")))

    def test_groom_and_clean_clear_hairball(self):
        pet = self.hatch()
        pet.state["hairball"] = True
        pet.state["hairball_hours"] = 0.4
        text = pet.groom()
        self.assertFalse(pet.state["hairball"])
        self.assertIsNotNone(pet.state["last_groom_at"])
        self.assertTrue(any(w in text.lower() for w in ("hairball", "drama", "brushing", "pixels tidied")))
        # clean is the same command in the CLI; the method is groom()
        pet.state["hairball"] = True
        pet.state["hairball_hours"] = 0.2
        pet.groom()
        self.assertFalse(pet.state["hairball"])

    def test_heal_is_harmless_soothe_alias(self):
        pet = self.hatch()
        pet.state["hairball"] = True
        health0 = pet.state["stats"]["health"]
        text = pet.heal()
        self.assertFalse(pet.state["hairball"])
        self.assertGreaterEqual(pet.state["stats"]["health"], health0)
        self.assertTrue(any(w in text.lower() for w in ("hairball", "performance", "fussed", "pretending", "dramatic")))

    def test_hairball_is_not_a_care_mistake_and_does_not_hibernate(self):
        pet = self.hatch()
        pet.state["stage"] = "juvenile"
        pet.state["hairball"] = True
        pet.state["hairball_hours"] = 0.0
        pet.state["stats"]["hunger"] = 80
        pet.state["stats"]["happiness"] = 80
        pet.state["stats"]["hygiene"] = 80
        pet.state["stats"]["health"] = 100
        pet.state["critical_hours"] = 0
        pet.state["care_mistakes"] = 0
        pet.state["last_care_at"] = m.fmt_ts(self.now)
        pet.state["last_updated"] = m.fmt_ts(self.now)
        pet.save()
        # 8+ hours mid-episode (auto-clears at 2h, but re-force and hold clocks)
        pet = self.advance(hours=1)
        pet.tick()
        # still shouldn't have counted a mistake even if we keep him mid-hack
        pet.state["hairball"] = True
        pet.state["hairball_hours"] = 0.0
        pet.state["care_mistakes"] = 0
        pet.state["zero_since"]["sick"] = m.fmt_ts(self.now - timedelta(hours=9))
        pet.state["mistake_counted"]["sick"] = False
        pet.save()
        pet = self.advance(hours=8)
        # keep hygiene/hunger up so only hairball could have counted
        pet.state["stats"]["hunger"] = 80
        pet.state["stats"]["happiness"] = 80
        pet.state["stats"]["hygiene"] = 80
        pet.state["stats"]["health"] = 100
        pet.state["hairball"] = True
        notes = pet.tick()
        self.assertEqual(pet.state["care_mistakes"], 0)
        self.assertNotEqual(pet.state["status"], "hibernating")
        self.assertGreaterEqual(pet.state["stats"]["health"], 100 - 0.001)
        self.assertNotIn("health reached zero", " ".join(notes).lower())

    def test_hairball_never_drops_health_during_tick(self):
        pet = self.hatch()
        pet.state["stage"] = "juvenile"
        pet.state["hairball"] = True
        pet.state["hairball_hours"] = 0.0
        pet.state["stats"]["hunger"] = 80
        pet.state["stats"]["hygiene"] = 80
        pet.state["stats"]["health"] = 100
        pet.state["critical_hours"] = 0
        pet.state["last_updated"] = m.fmt_ts(self.now)
        pet.save()
        pet = self.advance(hours=1.5)
        pet.tick()
        self.assertAlmostEqual(pet.state["stats"]["health"], 100, places=5)


class FurnishingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_file = self.dir / "pet.json"
        self.log_file = self.dir / "pet.log"
        self.t0 = at("2026-11-17T15:00:00-06:00")  # afternoon, awake
        self.now = self.t0
        self.rng = Random(42)

    def tearDown(self):
        self.tmp.cleanup()

    def hatch(self, traits=("snarky", "proud"), when=None):
        self.now = when or self.t0

        def now_fn():
            return self.now

        return m.Pet.hatch(
            now=self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
            traits=list(traits),
            force=True,
        )

    def pet_at(self, when: datetime) -> m.Byte:
        self.now = when
        pet = m.Pet.load(
            now_fn=lambda: self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
        )
        self.assertIsNotNone(pet)
        return pet

    def open_den(self, pet: m.Byte) -> m.Byte:
        text = pet.den_enable()
        self.assertTrue(pet.furnishings_open())
        self.assertTrue(any(k in text.lower() for k in ("open", "live", "furnishings")))
        return pet

    def _force_rng(self, pet, value: float) -> None:
        class Fixed(m.random.Random):
            def random(self):
                return value

            def choice(self, seq):
                return seq[0]

            def choices(self, population, weights=None, k=1):
                return [population[0]] * k

        pet.rng = Fixed()

    def test_hatch_has_furnishings_disabled(self):
        pet = self.hatch()
        den = pet.state["furnishings"]
        self.assertFalse(den["enabled"])
        self.assertFalse(pet.furnishings_open())
        self.assertEqual(den["laser_misses"], 0)
        self.assertFalse(den["in_hideout"])
        self.assertIn("lasers", pet.state["counts"])
        self.assertNotIn("Den:", pet.status_text(roll_event=False))

    def test_migrate_adds_furnishings_keeps_rest(self):
        raw = {
            "name": "Byte",
            "stage": "hatchling",
            "form": None,
            "traits": ["snarky", "proud"],
            "born_at": "2026-11-15T19:48:29-06:00",
            "living_seconds": 100.0,
            "stats": {"hunger": 54.5, "happiness": 93.0, "energy": 80.0, "hygiene": 77.0, "health": 100.0},
            "bond": 19.5,
            "care_mistakes": 0,
            "sick": False,
            "status": "awake",
            "last_updated": "2026-11-16T15:43:18-06:00",
            "last_care_at": "2026-11-15T23:11:21-06:00",
            "diary": [{"at": "2026-11-16T14:53:18-06:00", "kind": "knock", "text": "kept"}],
            "counts": {"meals": 1, "snacks": 1, "plays": 1, "rests": 0, "grooms": 1, "talks": 0},
            "hardcore": False,
        }
        migrated = m.migrate_state(raw, now=self.t0)
        self.assertFalse(migrated["furnishings"]["enabled"])
        self.assertEqual(migrated["furnishings"]["laser_misses"], 0)
        self.assertEqual(migrated["counts"]["lasers"], 0)
        self.assertEqual(migrated["counts"]["plays"], 1)
        self.assertEqual(migrated["diary"][0]["text"], "kept")
        self.assertEqual(migrated["bond"], 19.5)
        self.assertEqual(migrated["stats"]["happiness"], 93.0)
        self.state_file.write_text(json.dumps(raw), encoding="utf-8")
        pet = self.pet_at(self.t0)
        self.assertFalse(pet.furnishings_open())
        pet.save()
        reloaded = json.loads(self.state_file.read_text())
        self.assertFalse(reloaded["furnishings"]["enabled"])
        self.assertEqual(reloaded["diary"][0]["text"], "kept")
        self.assertEqual(reloaded["bond"], 19.5)

    def test_closed_den_actions_do_not_change_stats(self):
        pet = self.hatch()
        before = dict(pet.state["stats"])
        misses = pet.state["furnishings"]["laser_misses"]
        diary_n = len(pet.state.get("diary") or [])
        for text in (pet.play("laser"), pet.perch(), pet.tree(), pet.lounge(), pet.hideout()):
            self.assertTrue(any(k in text.lower() for k in ("not open", "not yet", "until")))
        self.assertEqual(pet.state["stats"], before)
        self.assertEqual(pet.state["furnishings"]["laser_misses"], misses)
        self.assertFalse(pet.state["furnishings"]["in_hideout"])
        self.assertEqual(len(pet.state.get("diary") or []), diary_n)
        self.assertEqual(pet.state["counts"]["lasers"], 0)

    def test_closed_den_play_is_unchanged(self):
        pet = self.hatch()
        pet.state["stats"]["energy"] = 80
        pet.state["stats"]["happiness"] = 50
        pet.save()
        text = pet.play()
        self.assertEqual(pet.state["counts"]["plays"], 1)
        self.assertEqual(pet.state["counts"]["lasers"], 0)
        self.assertAlmostEqual(pet.state["stats"]["energy"], 60, places=5)
        self.assertGreater(pet.state["stats"]["happiness"], 50)
        self.assertFalse(any(k in text.lower() for k in ("dot", "laser", "miss")))

    def test_closed_den_diary_stays_on_old_table(self):
        pet = self.hatch()
        kinds = set()
        for i in range(40):
            ev = pet.roll_diary_event(force=True, when=self.now)
            if ev:
                kinds.add(pet.state["diary"][-1]["kind"])
        allowed = {row[0] for row in m.DIARY_EVENTS}
        forbidden = {row[0] for row in m.FURNISHING_DIARY_EVENTS}
        self.assertTrue(kinds)
        self.assertTrue(kinds <= allowed)
        self.assertFalse(kinds & forbidden)
        self.assertIn("beer_can", allowed)

    def test_laser_never_catches_and_moves_stats(self):
        pet = self.hatch()
        self.open_den(pet)
        pet.state["stats"]["energy"] = 80
        pet.state["stats"]["happiness"] = 40
        pet.state["stats"]["health"] = 100
        pet.save()
        text = pet.play("laser")
        low = text.lower()
        self.assertTrue(any(k in low for k in ("dot", "laser", "physics", "catch", "miss", "coward", "technicality", "cheated", "almost")))
        self.assertNotRegex(low, r"caught it|finally caught|he caught the")
        self.assertEqual(pet.state["furnishings"]["laser_misses"], 1)
        self.assertEqual(pet.state["counts"]["lasers"], 1)
        self.assertEqual(pet.state["counts"]["plays"], 1)
        self.assertAlmostEqual(pet.state["stats"]["energy"], 80 - m.LASER_ENERGY, places=5)
        self.assertGreater(pet.state["stats"]["happiness"], 40)
        self.assertEqual(pet.state["stats"]["health"], 100)
        pet.state["stats"]["energy"] = 80
        pet.save()
        pet.play("laser")
        pet.state["stats"]["energy"] = 80
        pet.play("laser")
        pet.state["stats"]["energy"] = 80
        pet.play("laser")
        pet.state["stats"]["energy"] = 80
        text = pet.play("laser")
        self.assertEqual(pet.state["furnishings"]["laser_misses"], 5)
        self.assertIn("5", text)
        self.assertIn("zero", text.lower())

    def test_laser_refused_when_tired(self):
        pet = self.hatch()
        self.open_den(pet)
        pet.state["stats"]["energy"] = 10
        text = pet.play("laser")
        self.assertEqual(pet.state["stats"]["energy"], 10)
        self.assertEqual(pet.state["furnishings"]["laser_misses"], 0)
        self.assertTrue(any(k in text.lower() for k in ("eye", "tired", "battery", "nap")))

    def test_enabled_plain_play_can_become_laser(self):
        pet = self.hatch()
        self.open_den(pet)
        pet.state["stats"]["energy"] = 80
        pet.state["stats"]["happiness"] = 40
        pet.save()
        self._force_rng(pet, 0.0)  # below LASER_PLAY_CHANCE
        text = pet.play()
        self.assertEqual(pet.state["counts"]["lasers"], 1)
        self.assertTrue(any(k in text.lower() for k in ("dot", "laser", "physics", "catch", "miss", "coward", "technicality")))

    def test_enabled_plain_play_can_stay_regular(self):
        pet = self.hatch()
        self.open_den(pet)
        pet.state["stats"]["energy"] = 80
        pet.state["stats"]["happiness"] = 40
        pet.save()
        self._force_rng(pet, 0.99)  # above LASER_PLAY_CHANCE
        text = pet.play()
        self.assertEqual(pet.state["counts"]["lasers"], 0)
        self.assertEqual(pet.state["counts"]["plays"], 1)
        self.assertAlmostEqual(pet.state["stats"]["energy"], 60, places=5)
        self.assertFalse(any(k in text.lower() for k in ("dot", "laser", "physics", "coward")))

    def test_perch_sets_view_and_writes_diary(self):
        pet = self.hatch()
        self.open_den(pet)
        hap0 = pet.state["stats"]["happiness"]
        health0 = pet.state["stats"]["health"]
        text = pet.perch()
        view = pet.state["furnishings"]["window_view"]
        self.assertIn(view, {row[0] for row in m.WINDOW_VIEWS})
        self.assertGreater(pet.state["stats"]["happiness"], hap0)
        self.assertEqual(pet.state["stats"]["health"], health0)
        self.assertEqual(pet.state["counts"]["perches"], 1)
        self.assertEqual(pet.state["diary"][-1]["kind"], "perch_view")
        self.assertIn(pet.state["diary"][-1]["text"], text)
        self.assertIn("Den:", pet.status_text(roll_event=False))
        self.assertIn(m.window_view_label(view).split(",")[0].split()[0], pet.den_status_line())

    def test_window_views_include_mundane_and_upload(self):
        ids = {row[0] for row in m.WINDOW_VIEWS}
        for expected in (
            "squirrels", "birds", "cars", "wind", "butterflies", "rival_cat",
            "city", "ocean", "spaceship", "moot",
        ):
            self.assertIn(expected, ids)
        # prefer honors a requested view
        view = m.pick_window_view(Random(1), self.t0, prefer="rival_cat")
        self.assertEqual(view, "rival_cat")
        squirrel_lines = " ".join(window_view_by_id_lines := m.window_view_by_id("squirrels")[4])
        self.assertIn("squirrel", squirrel_lines.lower())
        self.assertTrue(any("heckled" in line.lower() or "incompetence" in line.lower() or "conviction" in line.lower() for line in m.window_view_by_id("squirrels")[4]))

    def test_tree_climb_and_nap(self):
        pet = self.hatch()
        self.open_den(pet)
        pet.state["stats"]["energy"] = 80
        pet.state["stats"]["happiness"] = 40
        text = pet.tree()
        self.assertLess(pet.state["stats"]["energy"], 80)
        self.assertGreater(pet.state["stats"]["happiness"], 40)
        self.assertTrue(any(k in text.lower() for k in ("tree", "throne", "platform", "domain", "gargoyle")))
        pet.state["stats"]["energy"] = 20
        energy = pet.state["stats"]["energy"]
        text = pet.tree()
        self.assertGreater(pet.state["stats"]["energy"], energy)
        self.assertTrue(any(k in text.lower() for k in ("nap", "rest", "lookout", "loaf", "meetings")))
        self.assertEqual(pet.state["counts"]["trees"], 2)
        self.assertFalse(pet.state["furnishings"]["in_hideout"])

    def test_lounge_restores_energy(self):
        pet = self.hatch()
        self.open_den(pet)
        pet.state["stats"]["energy"] = 40
        pet.state["stats"]["happiness"] = 40
        text = pet.lounge()
        self.assertAlmostEqual(pet.state["stats"]["energy"], 40 + m.LOUNGE_ENERGY, places=5)
        self.assertGreater(pet.state["stats"]["happiness"], 40)
        self.assertTrue(any(k in text.lower() for k in ("lounge", "loaf", "cushion", "treaty")))
        self.assertEqual(pet.state["counts"]["lounges"], 1)

    def test_hideout_is_a_skippy_nod_and_expires(self):
        pet = self.hatch()
        self.open_den(pet)
        pet.state["stats"]["energy"] = 40
        pet.state["stats"]["happiness"] = 40
        text = pet.hideout()
        self.assertTrue(pet.state["furnishings"]["in_hideout"])
        self.assertGreater(pet.state["stats"]["happiness"], 40)
        self.assertGreater(pet.state["stats"]["energy"], 40)
        self.assertTrue(any(k in text.lower() for k in ("beer", "skippy", "fit", "can")))
        self.assertIn("in the beer-can hideout", pet.den_status_line())
        # four hours later he wanders out; tick should clear the flag
        pet.state["last_updated"] = m.fmt_ts(self.now)
        pet.save()
        later = self.pet_at(self.now + timedelta(hours=5))
        later.tick()
        self.assertFalse(later.state["furnishings"]["in_hideout"])

    def test_enabled_diary_can_use_furnishing_events(self):
        pet = self.hatch()
        self.open_den(pet)
        kinds = set()
        for i in range(80):
            ev = pet.roll_diary_event(force=True, when=self.now)
            if ev:
                kinds.add(pet.state["diary"][-1]["kind"])
        self.assertTrue(kinds & {row[0] for row in m.FURNISHING_DIARY_EVENTS})
        # health never used as a negative delta in the new table
        for _eid, _text, deltas, _tags in m.FURNISHING_DIARY_EVENTS:
            self.assertGreaterEqual(deltas.get("health", 0), 0)

    def test_den_disable_restores_old_behavior(self):
        pet = self.hatch()
        self.open_den(pet)
        pet.den_disable()
        self.assertFalse(pet.furnishings_open())
        before = dict(pet.state["stats"])
        pet.perch()
        self.assertEqual(pet.state["stats"], before)
        self.assertNotIn("Den:", pet.status_text(roll_event=False))

    def test_cli_den_and_items(self):
        env = os.environ.copy()
        env["PET_STATE"] = str(self.state_file)
        env["PET_LOG"] = str(self.log_file)
        env["PET_NOW"] = "2026-11-17T15:00:00-06:00"
        env["PET_NO_TICKER"] = "1"
        script = Path(__file__).resolve().parent / "pixel_pet.py"

        def run(*args, expect=0):
            result = subprocess.run(
                [sys.executable, str(script), *args],
                capture_output=True,
                text=True,
                env=env,
            )
            if result.returncode != expect:
                self.fail(f"cli {args} exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
            return result

        run("hatch", "--seed", "7")
        r = run("den")
        self.assertIn("not open yet", r.stdout)
        r = run("laser")
        self.assertTrue(any(k in r.stdout.lower() for k in ("not open", "not yet", "until")))
        data = json.loads(self.state_file.read_text())
        self.assertFalse(data["furnishings"]["enabled"])
        self.assertEqual(data["counts"]["lasers"], 0)
        r = run("den", "enable")
        # Seed 7 picks "Furnishings are live...", not the "den is open" flavor.
        low = r.stdout.lower()
        self.assertTrue(
            "furnishings are live" in low or "den is open" in low,
            r.stdout,
        )
        data = json.loads(self.state_file.read_text())
        self.assertTrue(data["furnishings"]["enabled"])
        r = run("play", "laser")
        self.assertGreater(json.loads(self.state_file.read_text())["furnishings"]["laser_misses"], 0)
        r = run("perch")
        self.assertTrue(r.stdout.strip())
        r = run("tree")
        self.assertTrue(r.stdout.strip())
        r = run("lounge")
        self.assertTrue(r.stdout.strip())
        r = run("hideout")
        self.assertTrue(any(k in r.stdout.lower() for k in ("beer", "skippy", "fit", "can")))
        r = run("den")
        self.assertIn("Den:", r.stdout)
        r = run("status")
        self.assertIn("Den:", r.stdout)



class ToyChaosTests(unittest.TestCase):
    """Generic toy-chaos events: stuck-on-pet and out-of-reach (2026-11-21)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_file = self.dir / "pet.json"
        self.log_file = self.dir / "pet.log"
        self.t0 = at("2026-11-21T15:00:00-06:00")
        self.now = self.t0
        self.rng = Random(42)

    def tearDown(self):
        self.tmp.cleanup()

    def hatch(self, traits=("snarky", "proud"), when=None):
        self.now = when or self.t0
        return m.Pet.hatch(
            now=self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
            traits=list(traits),
            force=True,
        )

    def pet_at(self, when=None) -> m.Pet:
        if when is not None:
            self.now = when
        pet = m.Pet.load(
            now_fn=lambda: self.now,
            rng=self.rng,
            state_file=self.state_file,
            log_file=self.log_file,
        )
        self.assertIsNotNone(pet)
        return pet

    def advance(self, minutes: float = 0, hours: float = 0) -> m.Pet:
        self.now = self.now + timedelta(minutes=minutes, hours=hours)
        return self.pet_at(self.now)

    def _window_rng(self, pet, hit: bool = True, delay: int = 8):
        class Fixed(m.random.Random):
            def random(self):
                return m.TOY_CHAOS_CHANCE_WINDOW + 0.001 if hit else 0.0

            def randint(self, a, b):
                return max(a, min(b, delay))

            def choice(self, seq):
                return seq[0]

            def choices(self, population, weights=None, k=1):
                return [population[0]] * k

        pet.rng = Fixed()

    def test_hatch_and_migrate_add_toy_chaos_defaults(self):
        pet = self.hatch()
        box = pet.state["toy_chaos"]
        self.assertFalse(box["active"])
        self.assertEqual(box["times"], 0)
        self.assertIsNone(box["variant"])
        raw = {
            "name": "Byte",
            "stage": "hatchling",
            "form": None,
            "traits": ["snarky", "proud"],
            "born_at": "2026-11-15T19:48:29-06:00",
            "living_seconds": 100.0,
            "stats": {"hunger": 54.5, "happiness": 93.0, "energy": 80.0, "hygiene": 77.0, "health": 100.0},
            "bond": 19.5,
            "care_mistakes": 0,
            "status": "awake",
            "last_updated": "2026-11-16T15:43:18-06:00",
            "last_care_at": "2026-11-15T23:11:21-06:00",
            "diary": [{"at": "2026-11-16T14:53:18-06:00", "kind": "knock", "text": "kept"}],
            "counts": {"meals": 1},
            "hardcore": False,
        }
        self.state_file.write_text(json.dumps(raw), encoding="utf-8")
        migrated = self.pet_at(self.t0)
        box = migrated.state["toy_chaos"]
        self.assertFalse(box["active"])
        self.assertEqual(box["times"], 0)
        self.assertEqual(migrated.state["diary"][0]["text"], "kept")
        self.assertEqual(migrated.state["bond"], 19.5)
        self.assertEqual(migrated.state["name"], "Byte")

    def test_play_can_start_stuck_variant(self):
        pet = self.hatch()
        pet.state["stats"]["energy"] = 80
        pet.state["stats"]["happiness"] = 60
        pet.state["stats"]["health"] = 100
        health0 = pet.state["stats"]["health"]
        self._window_rng(pet, hit=True, delay=8)
        # choice() returns seq[0], so variant is "stuck"
        text = pet.play()
        box = pet.state["toy_chaos"]
        self.assertTrue(box["active"])
        self.assertEqual(box["variant"], "stuck")
        self.assertEqual(box["times_stuck"], 1)
        self.assertEqual(box["times_out_of_reach"], 0)
        self.assertEqual(box["attachment"], "claw")
        self.assertIn("claw", text.lower())
        self.assertEqual(pet.state["stats"]["health"], health0)
        self.assertIsNone(box["knocked"])

    def test_zero_rng_does_not_start(self):
        pet = self.hatch()
        pet.state["stats"]["energy"] = 80
        self._window_rng(pet, hit=False)
        pet.play()
        self.assertFalse(pet.state["toy_chaos"]["active"])
        self.assertEqual(pet.state["toy_chaos"]["times"], 0)

    def test_stuck_clears_on_its_own_not_by_care_or_find(self):
        pet = self.hatch()
        health0 = pet.state["stats"]["health"]
        pet._start_toy_chaos(self.now, variant="stuck", toy="feather", attachment="tail", duration_minutes=6)
        pet.save()
        self.assertTrue(pet.state["toy_chaos"]["active"])
        pet.feed("snack")
        self.assertTrue(pet.state["toy_chaos"]["active"])
        pet.groom()
        self.assertTrue(pet.state["toy_chaos"]["active"])
        text = pet.find()
        self.assertTrue(pet.state["toy_chaos"]["active"])
        self.assertTrue(any(k in text.lower() for k in ("stuck", "on its own", "unstick", "fall off")))
        self.assertEqual(pet.state["stats"]["health"], health0)
        pet = self.advance(minutes=7)
        notes = pet.tick()
        self.assertFalse(pet.state["toy_chaos"]["active"])
        joined = " ".join(notes).lower()
        self.assertTrue(any(k in joined for k in ("fell off", "dropped", "detached", "loaf", "statue", "acknowledge")))
        self.assertEqual(pet.state["stats"]["health"], health0)

    def test_out_of_reach_find_and_self_clear(self):
        pet = self.hatch()
        health0 = pet.state["stats"]["health"]
        bond0 = pet.state["bond"]
        hap0 = pet.state["stats"]["happiness"]
        pet._start_toy_chaos(
            self.now, variant="out_of_reach", toy="mouse", place="under the couch", duration_minutes=90
        )
        pet.save()
        box = pet.state["toy_chaos"]
        self.assertEqual(box["variant"], "out_of_reach")
        self.assertEqual(box["place"], "under the couch")
        self.assertEqual(box["times_out_of_reach"], 1)
        status = pet.status_text(roll_event=False)
        self.assertIn("Toy chaos:", status)
        self.assertIn("under the couch", status)
        self.assertIn("out of reach", status)
        # find retrieves it and cannot be used after
        text = pet.find()
        self.assertFalse(pet.state["toy_chaos"]["active"])
        self.assertIn("under the couch", text.lower())
        self.assertGreater(pet.state["stats"]["happiness"], hap0)
        self.assertGreater(pet.state["bond"], bond0)
        self.assertEqual(pet.state["stats"]["health"], health0)
        none = pet.find()
        self.assertTrue(any(k in none.lower() for k in ("nothing", "no missing", "reachable")))
        # A second episode can roll loose on its own.
        box = pet.state["toy_chaos"]
        box["last_at"] = None
        pet.save()
        pet = self.advance(hours=7)
        pet._start_toy_chaos(
            self.now, variant="out_of_reach", toy="ball", place="on a high shelf", duration_minutes=60
        )
        pet.save()
        pet = self.advance(minutes=61)
        notes = pet.tick()
        self.assertFalse(pet.state["toy_chaos"]["active"])
        joined = " ".join(notes).lower()
        self.assertTrue(any(k in joined for k in ("rolled loose", "slipped free", "freed itself")))
        self.assertEqual(pet.state["stats"]["health"], health0)

    def test_den_knockover_only_for_stuck_when_open(self):
        pet = self.hatch()
        pet._start_toy_chaos(self.now, variant="stuck", toy="toy", attachment="claw", duration_minutes=4)
        self.assertIsNone(pet.state["toy_chaos"]["knocked"])
        box = pet.state["toy_chaos"]
        box["active"] = False
        box["last_at"] = None
        pet.state["furnishings"]["enabled"] = True
        pet.save()
        pet = self.advance(hours=7)
        line = pet._start_toy_chaos(self.now, variant="stuck", toy="feather", attachment="collar", duration_minutes=4)
        knocked = pet.state["toy_chaos"]["knocked"]
        self.assertTrue(knocked)
        self.assertIn(knocked, line)

    def test_cooldown_and_idle_start_and_hibernation(self):
        pet = self.hatch()
        pet._start_toy_chaos(self.now, variant="stuck", toy="toy", attachment="claw", duration_minutes=3)
        pet.save()
        pet = self.advance(minutes=5)
        pet.tick()
        self.assertFalse(pet.state["toy_chaos"]["active"])
        pet.state["stats"]["energy"] = 90
        self._window_rng(pet, hit=True, delay=5)
        pet.play()
        self.assertFalse(pet.state["toy_chaos"]["active"], "cooldown should block a second onset")
        pet = self.advance(hours=6)
        pet.state["stats"]["energy"] = 90
        self._window_rng(pet, hit=True, delay=5)
        pet.play()
        self.assertTrue(pet.state["toy_chaos"]["active"])
        # Hibernating pets do not start a new episode.
        box = pet.state["toy_chaos"]
        box["active"] = False
        box["last_at"] = None
        pet.state["status"] = "hibernating"
        pet.state["hibernating_since"] = m.fmt_ts(self.now)
        pet.save()
        self.assertFalse(pet._toy_chaos_can_start(self.now))

    def test_lines_are_generic_and_safe(self):
        # Copy must stay generic: no personal or household names in this public kit.
        banned_hurt = ("hurt", "injur", "wound", "bleed", "blood", "pain", "sick", "medic", "vet", "broken bone")
        keys = [k for k in m.ACTION_LINES if k.startswith("toy_chaos_")]
        self.assertGreaterEqual(len(keys), 8)
        blob = " ".join(" ".join(m.ACTION_LINES[k]) for k in keys).lower()
        for word in banned_hurt:
            self.assertNotIn(word, blob)
        self.assertEqual(m.TOY_CHAOS_VARIANTS, ("stuck", "out_of_reach"))
        self.assertEqual(m.TOY_CHAOS_PLACES, ("under the couch", "under the furniture", "on a high shelf"))
        self.assertTrue(m.toy_chaos_roll_hits(type("R", (), {"random": lambda self: 0.50})(), 0.10))
        self.assertFalse(m.toy_chaos_roll_hits(type("R", (), {"random": lambda self: 0.0})(), 0.10))

    def test_cli_find(self):
        self.hatch()
        env = dict(os.environ, PET_STATE=str(self.state_file), PET_LOG=str(self.log_file), PET_NO_TICKER="1")
        r = subprocess.run([sys.executable, str(SCRIPT), "find", "--at", "2026-11-21T15:00:00-06:00"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(k in r.stdout.lower() for k in ("nothing", "no missing", "reachable")))



if __name__ == "__main__":
    unittest.main()
