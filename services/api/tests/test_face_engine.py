"""使用真实官方模型及公开授权样例；样例不登记到正式人员库。"""
import io
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

from app.core.face_match import FaceEngine, FaceMatchError, decode_image

FIXTURES = Path(__file__).parent / 'face_fixtures'


def png(image):
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


class FaceEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = FaceEngine()
        cls.astronaut = Image.open(FIXTURES / 'astronaut.png').convert('RGB')
        cls.hopper = Image.open(FIXTURES / 'grace_hopper.jpg').convert('RGB')

    @classmethod
    def tearDownClass(cls):
        cls.astronaut.close()
        cls.hopper.close()

    def test_real_model_same_person_variant_and_different_person(self):
        first = self.engine.extract(png(self.astronaut))
        variant = self.astronaut.rotate(3, resample=Image.Resampling.BICUBIC)
        variant = ImageEnhance.Brightness(variant).enhance(0.8)
        second = self.engine.extract(png(variant))
        other = self.engine.extract(png(self.hopper))
        same = self.engine.compare(first.embedding, second.embedding)
        different = self.engine.compare(first.embedding, other.embedding)
        print(f'REAL_MODEL same_variant={same:.6f}, different={different:.6f}, threshold={self.engine.threshold}')
        self.assertGreaterEqual(same, self.engine.threshold)
        self.assertLess(different, self.engine.threshold)
        self.assertEqual(first.embedding.shape, (128,))
        self.assertAlmostEqual(float(np.linalg.norm(first.embedding)), 1, places=5)
        self.assertEqual(Image.open(io.BytesIO(first.photo)).size, (112, 112))

    def test_no_face_multiple_faces_and_small_face_are_rejected(self):
        multiple = Image.new('RGB', (1024, 512), 'white')
        multiple.paste(self.astronaut, (0, 0))
        multiple.paste(self.astronaut, (512, 0))
        for image, message in [
            (Image.new('RGB', (512, 512), 'white'), '未检测'),
            (multiple, '多张'),
            (self.astronaut.resize((256, 256)), '太小'),
        ]:
            with self.subTest(message=message), self.assertRaises(FaceMatchError) as error:
                self.engine.extract(png(image))
            self.assertEqual(error.exception.status_code, 422)
            self.assertIn(message, error.exception.message)

    def test_decode_limits_invalid_format_and_missing_model(self):
        for data in [b'not-an-image', png(Image.new('RGB', (4100, 80))), png(Image.new('RGB', (20, 20)))]:
            with self.assertRaises(FaceMatchError) as error:
                decode_image(data)
            self.assertEqual(error.exception.status_code, 422)
        output = io.BytesIO()
        self.astronaut.save(output, 'BMP')
        with self.assertRaises(FaceMatchError):
            decode_image(output.getvalue())
        with self.assertRaises(FaceMatchError) as error:
            FaceEngine('/nonexistent-face-model-directory')
        self.assertEqual(error.exception.status_code, 503)

    def test_blurred_face_and_invalid_vector_are_rejected(self):
        with self.assertRaises(FaceMatchError):
            self.engine.extract(png(self.astronaut.filter(ImageFilter.GaussianBlur(7))))
        with self.assertRaises(FaceMatchError) as error:
            self.engine.compare([float('nan')] * 128, [1] * 128)
        self.assertEqual(error.exception.status_code, 409)


if __name__ == '__main__':
    unittest.main()
