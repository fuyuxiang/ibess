import unittest
from uuid import uuid4
from service.event_scope import configured_source, should_capture


class EventScopeTest(unittest.TestCase):
    def event(self, **changes):
        return {'event_id':str(uuid4()),'camera_id':'camera-001','risk_level':'high','status':'PENDING', **changes}

    def test_source_aliases_resolve_to_the_buffer_that_is_actually_recorded(self):
        self.assertEqual(configured_source('rtsp://media:8554/cam1'),'cam1')
        self.assertTrue(should_capture(self.event(),'cam1'))
        self.assertTrue(should_capture(self.event(camera_id='cam1'),'cam1'))
        self.assertFalse(should_capture(self.event(camera_id='camera-002'),'cam1'))

    def test_audio_simulation_and_state_changes_never_capture_unrelated_video(self):
        for changes in ({'source_type':'audio'}, {'source_type':'simulation'}, {'event_type':'semantic_S06'},
                        {'is_demo':'True'}, {'skill_code':'demo_simulation'}, {'status':'CLOSED'}, {'risk_level':'info'}):
            with self.subTest(changes=changes):
                self.assertFalse(should_capture(self.event(**changes),'cam1'))

    def test_real_device_alias_and_invalid_identifiers(self):
        self.assertEqual(configured_source('rtsp://media:8554/cam-ezviz'),'cam-ezviz')
        self.assertTrue(should_capture(self.event(camera_id='camera-real-01'),'cam-ezviz'))
        self.assertFalse(should_capture(self.event(event_id='../invalid'),'cam1'))
        self.assertFalse(should_capture(self.event(),''))


if __name__=='__main__':unittest.main()
