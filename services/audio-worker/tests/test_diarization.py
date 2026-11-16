import unittest
from worker.diarization import normalize_segments


class DiarizationTest(unittest.TestCase):
    def test_labels_follow_time_first_appearance_and_unknown_stays_unknown(self):
        result = {'sentence_info': [
            {'start':1000,'end':1500,'text':'第二个人','spk':4},
            {'start':0,'end':900,'text':'第一个人','spk':9},
            {'start':1800,'end':2300,'text':'同一个人','spk':9},
            {'start':2400,'end':2600,'text':'未确定'},
        ]}
        rows = normalize_segments(result, 3000)
        self.assertEqual([r['speaker'] for r in rows], ['A','B','A',None])
        self.assertEqual(rows[0]['start_ms'], 0)
        self.assertNotIn('embedding', rows[0])

    def test_no_fake_sentences_no_out_of_bounds_or_nonfinite_times(self):
        self.assertEqual(normalize_segments({'text':'有文本但无时间'}, 10000), [])
        for segment in [
            {'start':float('nan'),'end':1000,'text':'无效'},
            {'start':0,'end':float('inf'),'text':'无效'},
            {'start':4000,'end':5000,'text':'越界'},
            {'start':1000,'end':1000,'text':'零长'},
            {'start':-5,'end':100,'text':'负值'},
        ]:
            self.assertEqual(normalize_segments({'sentence_info':[segment]},3000), [])
        self.assertEqual(normalize_segments({'sentence_info':[{'start':0,'end':3100,'text':'结尾略超','spk':0}]},3000)[0]['end_ms'],3000)

    def test_labels_are_local_and_not_always_two_people(self):
        def run(spk):
            return normalize_segments({'sentence_info':[{'start':0,'end':1000,'spk':spk,'text':'一句话'}]},2000)
        self.assertEqual(run(8)[0]['speaker'],'A')
        self.assertEqual(run(2)[0]['speaker'],'A')
        self.assertEqual(run(-1)[0]['speaker'],None)


if __name__=='__main__':
    unittest.main()
