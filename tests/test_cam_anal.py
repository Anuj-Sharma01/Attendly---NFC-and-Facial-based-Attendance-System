import numpy as np

from Project import camAnal


class FakeCapture:
    def __init__(self):
        self.released = False
        self.reads = 0

    def isOpened(self):
        return True

    def set(self, *_args):
        return True

    def read(self):
        self.reads += 1
        return True, np.zeros((480, 640, 3), dtype=np.uint8)

    def release(self):
        self.released = True


class FakeCV2:
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    INTER_AREA = 5
    COLOR_BGR2RGB = 6

    def __init__(self):
        self.capture = FakeCapture()
        self.open_calls = 0

    def VideoCapture(self, _index):
        self.open_calls += 1
        return self.capture

    def resize(self, _frame, _size, **_kwargs):
        return np.zeros((120, 160, 3), dtype=np.uint8)

    def cvtColor(self, frame, _code):
        return frame


class FakeFaceRecognition:
    def load_image_file(self, _path):
        return "reference-image"

    def face_encodings(self, image, *_args, **_kwargs):
        return [np.array([0.1])] if (isinstance(image, str) and image == "reference-image") or isinstance(image, np.ndarray) else []

    def face_locations(self, *_args, **_kwargs):
        return [(1, 10, 10, 1)]

    def face_distance(self, _known, _encoding):
        return np.array([0.1])


def test_face_verifier_requires_stable_match_and_releases_camera(tmp_path, monkeypatch):
    photos = tmp_path / "Photos"
    photos.mkdir()
    (photos / "Ada Lovelace.jpg").write_bytes(b"mock image")
    cv2 = FakeCV2()
    monkeypatch.setattr(camAnal, "_face_libraries", lambda: (cv2, FakeFaceRecognition()))

    verifier = camAnal.FaceVerifier(photos_dir=photos, tolerance=0.4, required_matches=2)
    assert verifier.verify("Ada Lovelace", timeout_seconds=1, display=False) is True
    assert cv2.capture.released is True
    assert cv2.capture.reads >= 4


def test_face_verifier_rejects_name_without_reference_before_opening_camera(tmp_path, monkeypatch):
    photos = tmp_path / "Photos"
    photos.mkdir()
    (photos / "Ada Lovelace.jpg").write_bytes(b"mock image")
    cv2 = FakeCV2()
    monkeypatch.setattr(camAnal, "_face_libraries", lambda: (cv2, FakeFaceRecognition()))

    verifier = camAnal.FaceVerifier(photos_dir=photos, required_matches=1)
    assert verifier.verify("Grace Hopper", timeout_seconds=1, display=False) is False
    assert cv2.open_calls == 0


def test_face_verifier_loads_multiple_images_by_roll_number(tmp_path, monkeypatch):
    photos = tmp_path / "Photos"
    student_dir = photos / "CE42"
    student_dir.mkdir(parents=True)
    (student_dir / "face_01.jpg").write_bytes(b"image 1")
    (student_dir / "face_02.jpg").write_bytes(b"image 2")
    cv2 = FakeCV2()
    monkeypatch.setattr(camAnal, "_face_libraries", lambda: (cv2, FakeFaceRecognition()))

    verifier = camAnal.FaceVerifier(
        photos_dir=photos,
        students=[{"ROLLNO": "CE42", "NAME": "Ada Lovelace"}],
        required_matches=1,
    )
    assert len(verifier.known_faces) == 2
    assert {face.roll_no for face in verifier.known_faces} == {"CE42"}
    assert all(face.name == "Ada Lovelace" for face in verifier.known_faces)
    assert verifier.verify("Ada Lovelace", expected_roll_no="CE99", display=False) is False
    assert cv2.open_calls == 0
