import unittest
from server.boundaries import observe


class BoundaryTests(unittest.TestCase):
    def test_moving_seven_day_boundary_never_opens_cycle(self):
        state={}
        for t in range(1000,1600,30):
            state,create=observe(state,t+604800,10080,t)
            self.assertFalse(create)
        self.assertEqual(state['status'],'boundary_pending')

    def test_stability_requires_distinct_observations_and_elapsed_time(self):
        state,create=observe({},19000,300,1000)
        for _ in range(5):state,create=observe(state,19000,300,1000)
        self.assertEqual(state['candidate']['count'],1)
        state,create=observe(state,19001,300,1030);self.assertFalse(create)
        state,create=observe(state,19000,300,1060);self.assertTrue(create)

    def test_small_rounding_is_not_a_new_cycle(self):
        state={'current':dict(id='existing',reset=19000,duration=300),'observed_at':1000}
        for t,reset in [(1030,19001),(1060,18999),(1090,19000)]:
            state,create=observe(state,reset,300,t)
            self.assertFalse(create);self.assertEqual(state['status'],'available')
            self.assertEqual(state['current']['id'],'existing')

    def test_early_change_requires_review_and_preserves_original(self):
        state={'current':dict(id='existing',reset=19000,duration=300),'observed_at':1000}
        for t in (1030,1060,1090):state,create=observe(state,20000,300,t)
        self.assertFalse(create);self.assertEqual(state['status'],'boundary_review')
        self.assertEqual(state['current']['reset'],19000)

    def test_normal_rollover_is_stable_and_disjoint(self):
        state={'current':dict(id='existing',reset=19000,duration=300),'observed_at':1000}
        for t in (19000,19030,19060):state,create=observe(state,37000,300,t)
        self.assertTrue(create)
        self.assertGreaterEqual(state['candidate']['reset']-300*60,state['current']['reset'])

    def test_stale_replay_cannot_replace_current_observation(self):
        state={'current':dict(id='existing',reset=19000,duration=300),'observed_at':2000,'status':'available'}
        result,create=observe(state,20000,300,1900)
        self.assertEqual(result,state);self.assertFalse(create)
