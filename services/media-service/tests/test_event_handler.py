"""Exercise the real event handler against an isolated SQLite evidence table."""
import asyncio
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4


class EventHandlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {'DATABASE_URL':'sqlite://', 'RTSP_URL':'rtsp://media:8554/cam1'}):
            from service import main
        cls.main=main
        # Production workers only check the migrated schema; tests own their fixtures.
        main.Evidence.__table__.create(main.engine, checkfirst=True)

    def setUp(self):
        with self.main.Session.begin() as db:
            db.query(self.main.Evidence).delete()
        self.source=patch.object(self.main,'MEDIA_SOURCE_ID','cam1');self.source.start()
        self.wait=patch.object(self.main,'POST_SECONDS',0);self.wait.start()
        self.capture=patch.object(self.main,'extract_clip',return_value=True).start()
        self.upload=patch.object(self.main,'upload_evidence',return_value='videos/test.mp4').start()
        self.addCleanup(patch.stopall)

    def event(self, **changes):
        return {'event_id':str(uuid4()),'camera_id':'camera-001','risk_level':'high','status':'PENDING','occurred_at':'2026-10-08T00:00:00', **changes}

    def test_actual_handler_writes_evidence_once_and_normalizes_naive_utc(self):
        event=self.event()
        asyncio.run(self.main.handle_event(event))
        asyncio.run(self.main.handle_event(event))
        self.assertEqual(self.capture.call_count,1)
        self.assertEqual(self.capture.call_args.args[0],datetime(2026,10,8,tzinfo=timezone.utc).timestamp())
        with self.main.Session() as db:
            rows=db.query(self.main.Evidence).all()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0].event_id,event['event_id'])

    def test_cam_alias_reaches_the_actual_capture_handler(self):
        asyncio.run(self.main.handle_event(self.event(camera_id='cam1')))
        self.capture.assert_called_once()

    def test_another_camera_does_not_receive_the_current_cameras_clip(self):
        asyncio.run(self.main.handle_event(self.event(camera_id='camera-002')))
        self.capture.assert_not_called()
        self.upload.assert_not_called()

    def test_audio_simulation_and_processing_updates_are_not_video_discovery(self):
        for change in ({'source_type':'audio'},{'is_demo':'True'},{'status':'CONFIRMED'},{'event_type':'semantic_S06'}):
            asyncio.run(self.main.handle_event(self.event(**change)))
        self.capture.assert_not_called()

    def test_invalid_time_does_not_make_a_clip(self):
        asyncio.run(self.main.handle_event(self.event(occurred_at='invalid')))
        self.capture.assert_not_called()


if __name__=='__main__':unittest.main()
