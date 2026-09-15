"""Tests for the capacity advisor.

The properties that matter are less about arithmetic than about trust: the panel
shows this number next to a button that acts on it, so it has to be a number an
operator could plausibly accept, it has to say what limited it, and it must not
fall over when the install is too new to have any data.
"""

from __future__ import annotations

import unittest

from pilot_app import capacity

IDLE = {"cpu_percent": 3.0, "memory": {"percent": 31.0}, "disk": {"percent": 25.0}}
BUSY = {"cpu_percent": 95.0, "memory": {"percent": 92.0}, "disk": {"percent": 88.0}}


def volume(**overrides):
    base = {"window_days": 14, "messages": 31, "delivered": 30, "active_users": 1,
            "total_users": 2, "generation_gaps": [6, 5, 7, 6, 6, 9, 5]}
    base.update(overrides)
    return base


class CapacityAdviceTests(unittest.TestCase):
    def test_the_recommendation_is_a_number_someone_could_act_on(self):
        """The raw throughput arithmetic happily produces tens of thousands,
        because it assumes the box exists only to serve reports. A number like
        that is not advice — printing it once would cost the operator's trust in
        every other number on the panel."""
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6,
                                 current=5, source="environment")
        self.assertLessEqual(advice["recommended"], capacity.PILOT_COMFORTABLE_USERS)
        self.assertGreaterEqual(advice["recommended"], 10)

    def test_a_busy_machine_stops_the_advice_growing(self):
        advice = capacity.advise(volume=volume(), host=BUSY, workers=6,
                                 current=5, source="settings")
        self.assertEqual(advice["binding"], "cpu")
        self.assertEqual(advice["recommended"], 5)

    def test_it_never_suggests_shrinking_the_pilot(self):
        """Removing users is a decision, not a recommendation."""
        advice = capacity.advise(volume=volume(total_users=120), host=BUSY, workers=6,
                                 current=120, source="settings")
        self.assertGreaterEqual(advice["recommended"], 120)

    def test_more_traffic_per_user_means_fewer_users(self):
        quiet = capacity.advise(volume=volume(messages=30, active_users=1), host=IDLE,
                                workers=6, current=1, source="environment")
        busy = capacity.advise(volume=volume(messages=3000, active_users=1), host=IDLE,
                               workers=6, current=1, source="environment")
        self.assertLess(busy["measured"]["mails_per_user_day"],
                        quiet["measured"]["mails_per_user_day"] + 1e9)
        self.assertLessEqual(busy["recommended"], quiet["recommended"])

    def test_a_fresh_install_is_flagged_rather_than_guessed_silently(self):
        advice = capacity.advise(volume=volume(messages=0, delivered=0, active_users=0,
                                               total_users=2, generation_gaps=[]),
                                 host=IDLE, workers=6, current=5, source="environment")
        self.assertEqual(advice["confidence"], "low")
        self.assertFalse(advice["measured"]["report_seconds_from_data"])
        self.assertTrue(any("默认" in note for note in advice["notes"]),
                        "没有实测数据时必须明确说出来")

    def test_enough_samples_raises_confidence(self):
        advice = capacity.advise(volume=volume(generation_gaps=[6] * 12), host=IDLE,
                                 workers=6, current=5, source="environment")
        self.assertEqual(advice["confidence"], "high")
        self.assertTrue(advice["measured"]["report_seconds_from_data"])

    def test_the_binding_reason_explains_itself(self):
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6,
                                 current=5, source="environment")
        self.assertTrue(advice["binding_reason"])
        self.assertIn("判断", "判断依据")
        names = [item["name"] for item in advice["constraints"]]
        self.assertIn(advice["binding"], names)

    def test_it_reports_what_the_live_pressure_says(self):
        """The operator asked for advice based on server pressure, so the answer
        has to state it rather than leave it to be inferred."""
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6,
                                 current=5, source="environment")
        self.assertIn("CPU 3%", advice["load"])
        self.assertTrue(any("很轻松" in note for note in advice["notes"]))

    def test_missing_metrics_do_not_invent_a_constraint(self):
        """The macOS dev box cannot read /proc; null must mean unknown."""
        advice = capacity.advise(volume=volume(), host={"cpu_percent": None,
                                                       "memory": {}, "disk": {}},
                                 workers=6, current=5, source="environment")
        names = [item["name"] for item in advice["constraints"]]
        self.assertNotIn("cpu", names)
        self.assertNotIn("memory", names)
        self.assertNotIn("disk", names)

    def test_a_zero_worker_count_does_not_divide_by_zero(self):
        advice = capacity.advise(volume=volume(), host=IDLE, workers=0,
                                 current=5, source="environment")
        self.assertGreaterEqual(advice["recommended"], 1)

    def test_the_source_is_passed_through(self):
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6,
                                 current=9, source="settings")
        self.assertEqual(advice["source"], "settings")
        self.assertEqual(advice["current"], 9)


if __name__ == "__main__":
    unittest.main()
