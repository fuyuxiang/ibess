"""8.4 首批评测基准数据集完整性检查（纯本地，不依赖网络与模型）。"""
import json
import os
import unittest

SCENES = {"S06", "S07"}
CONCLUSIONS = {"normal", "suspected", "insufficient"}
TITLES = ["01_", "02_", "03_", "04_", "05_", "06_", "07_"]


def load_dataset():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "eval_dataset_8_4.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class EvalDatasetTest(unittest.TestCase):
    def test_structure_complete(self):
        data = load_dataset()
        self.assertEqual(data["dataset"], "8.4-first-batch")
        self.assertEqual(len(data["materials"]), 7)
        prefixes = [item["prefix"] for item in data["materials"]]
        self.assertEqual(prefixes, TITLES)

    def test_every_material_covers_both_scenes(self):
        data = load_dataset()
        for material in data["materials"]:
            scenes = {window["scene"] for window in material["windows"]}
            self.assertEqual(scenes, SCENES, material["prefix"])
            self.assertTrue(material.get("source_label"), material["prefix"])

    def test_expected_conclusions_valid(self):
        data = load_dataset()
        for material in data["materials"]:
            for window in material["windows"]:
                self.assertIn(window["expected_conclusion"], CONCLUSIONS)
                for allowed in window.get("boundary_allow", []):
                    self.assertIn(allowed, CONCLUSIONS - {window["expected_conclusion"]})

    def test_source_labels_reference_scenes(self):
        data = load_dataset()
        joined = json.dumps(data, ensure_ascii=False)
        for scene in ("S06", "S07"):
            self.assertIn(scene, joined)


if __name__ == "__main__":
    unittest.main()
