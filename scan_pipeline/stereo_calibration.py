"""Стереокалибровка двух RGB-камер по плоской шахматной мишени.

Калибровка намеренно не «подгоняет» результат: если мишень плохо видна,
видов мало, ошибка репроекции или эпиполярная ошибка велики, запись помечается
недействительной и стереоглубина не вычисляется.

OpenCV используется только для геометрии и захвата UVC-камер. Depth Studio
ничего не устанавливает во время калибровки. Физические Kinect и Logitech
C930e в этом репозитории не проверялись, поэтому ``verifiedOnHardware``
всегда ``False``.

Единицы:
    * координаты мишени и трансляции — метры;
    * размеры изображений — пиксели;
    * углы — градусы (в отчёте), внутренние матрицы — пиксели.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------
# Порог качества. Значения подобраны консервативно для стереопары с малым
# базисом; они не гарантируют точность на конкретном оборудовании.
# --------------------------------------------------------------------------
MIN_VIEWS = 5
RECOMMENDED_VIEWS = 8
MAX_INTRINSIC_RMS_PX = 1.5
MAX_STEREO_RMS_PX = 1.5
MAX_EPIPOLAR_MEDIAN_PX = 0.5
MAX_EPIPOLAR_P95_PX = 1.5
MIN_BASELINE_M = 0.03
MAX_BASELINE_M = 2.0
MAX_TRANSLATION_SPREAD_M = 0.010
MAX_ROTATION_SPREAD_DEG = 1.0
MIN_SPAN_U = 0.20
MIN_SPAN_V = 0.20
MIN_MEAN_COVERAGE = 0.01
MIN_MAX_BOARD_TILT_DEG = 10.0
MAX_VIEW_SKEW_MS = 150.0
WORKING_SIZE = (640, 480)
SCHEMA_VERSION = 1


class StereoUnavailable(RuntimeError):
    """OpenCV/NumPy недоступны или калибровка невозможна.

    ``code`` стабилен для HTTP-слоя и интерфейса.
    """

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def _cv2():
    try:
        import cv2  # noqa: F401
        import numpy as np  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - точная причина важна пользователю
        raise StereoUnavailable(
            "opencv_missing",
            "Для стереорежима не найден OpenCV (cv2).",
            "OpenCV входит в установщик Depth Studio. Нажмите «Восстановить среду»; "
            "в dev-окружении установите opencv-python закреплённой версии.",
        ) from exc
    import cv2
    import numpy as np

    return cv2, np


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


# --------------------------------------------------------------------------
# Мишень и поиск углов
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardSpec:
    """Шахматная доска: ``cols``/``rows`` — число *внутренних* углов."""

    cols: int = 9
    rows: int = 6
    square_mm: float = 25.0
    target_kind: str = "screen"  # screen | printed | unknown

    def validate(self) -> "BoardSpec":
        if isinstance(self.cols, bool) or not isinstance(self.cols, int) or not 4 <= self.cols <= 14:
            raise ValueError("Число внутренних углов по горизонтали должно быть целым от 4 до 14.")
        if isinstance(self.rows, bool) or not isinstance(self.rows, int) or not 3 <= self.rows <= 14:
            raise ValueError("Число внутренних углов по вертикали должно быть целым от 3 до 14.")
        if self.cols == self.rows:
            # Квадратная сетка неоднозначна при повороте на 90°. Разрешаем,
            # но решение об ориентации проверяется по изображению.
            pass
        if isinstance(self.square_mm, bool) or not isinstance(self.square_mm, (int, float)):
            raise ValueError("Размер квадрата мишени должен быть числом в миллиметрах.")
        if not math.isfinite(float(self.square_mm)) or not 5.0 <= float(self.square_mm) <= 200.0:
            raise ValueError("Размер квадрата мишени должен быть в пределах 5–200 мм.")
        if self.target_kind not in ("screen", "printed", "unknown"):
            raise ValueError("Неизвестный тип мишени.")
        return self

    @property
    def square_m(self) -> float:
        return float(self.square_mm) / 1000.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BoardSpec":
        return cls(int(data.get("cols", 9)), int(data.get("rows", 6)),
                   float(data.get("square_mm", 25.0)), str(data.get("target_kind", "screen"))).validate()


def board_object_points(board: BoardSpec) -> Any:
    """Nx3 float32, ряд за рядом, начиная с верхнего левого угла мишени."""
    _, np = _cv2()
    points = np.zeros((board.rows * board.cols, 3), dtype=np.float32)
    grid = np.mgrid[0:board.rows, 0:board.cols].reshape(2, -1).T
    points[:, 0] = grid[:, 1] * board.square_m
    points[:, 1] = grid[:, 0] * board.square_m
    return points


def _rotation_angle_deg(rotation: Any) -> float:
    np = _cv2()[1]
    value = float((np.trace(np.asarray(rotation, dtype=np.float64)) - 1.0) / 2.0)
    return float(math.degrees(math.acos(max(-1.0, min(1.0, value)))))


def _canonical_candidates(rows: int, cols: int) -> list[tuple[str, Any]]:
    """Все разумные переназначения порядка сетки.

    Обычно OpenCV уже возвращает согласованный порядок, но камера может
    увидеть доску с другого края. Для стереопары важно, чтобы физический угол
    мишени имел одинаковые координаты в обеих камерах; ориентация выбирается по
    изображению (верхний левый угол кадра).
    """
    _, np = _cv2()
    index = np.arange(rows * cols).reshape(rows, cols)
    candidates = [
        ("identity", index),
        ("flip_rows", index[::-1, :]),
        ("flip_cols", index[:, ::-1]),
        ("rotate_180", index[::-1, ::-1]),
    ]
    if rows == cols:
        candidates.extend([
            ("transpose", index.T),
            ("transpose_flip_rows", index.T[::-1, :]),
            ("transpose_flip_cols", index.T[:, ::-1]),
            ("transpose_rotate_180", index.T[::-1, ::-1]),
        ])
    return candidates


def canonicalize_board_order(points: Any, board: BoardSpec, *, threshold: float = 1e-9) -> tuple[Any, str]:
    """Привести сетку к порядку «слева-сверху → вправо-вниз».

    Это не подгонка измерений: используются только сами найденные углы, меняется
    лишь их нумерация. Если доска наклонена больше ~45°, ориентация может быть
    неоднозначной; такая пара обычно отбрасывается проверкой эпиполярной ошибки.
    """
    _, np = _cv2()
    value = np.asarray(points, dtype=np.float64).reshape(board.rows, board.cols, 2)
    flat = value.reshape(-1, 2)
    best_name = "identity"
    best_score = float("inf")
    best_points = flat
    for name, order in _canonical_candidates(board.rows, board.cols):
        candidate = flat[order.reshape(-1)]
        top_left = candidate[0]
        top_right = candidate[board.cols - 1]
        bottom_left = candidate[(board.rows - 1) * board.cols]
        # Оси доски в пикселях. Идеал: +x вправо, +y вниз.
        x_axis = top_right - top_left
        y_axis = bottom_left - top_left
        if np.linalg.norm(x_axis) < threshold or np.linalg.norm(y_axis) < threshold:
            continue
        x_angle = abs(math.atan2(float(x_axis[1]), float(x_axis[0])))
        y_angle = abs(math.atan2(float(y_axis[1]), float(y_axis[0])) - math.pi / 2.0)
        score = x_angle + y_angle
        if score < best_score:
            best_score = score
            best_name = name
            best_points = candidate
    return best_points.astype(np.float32, copy=False), best_name


def _refine_corners(gray: Any, points: Any, board: BoardSpec) -> Any:
    cv2, np = _cv2()
    if gray is None:
        return points
    value = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2).copy()
    try:
        cv2.cornerSubPix(
            gray,
            value,
            (11, 11),
            (-1, -1),
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 0.01),
        )
    except cv2.error:
        pass
    return value.reshape(-1, 2)


def _find_chessboard(gray: Any, board: BoardSpec) -> tuple[Any | None, str]:
    cv2, _ = _cv2()
    pattern = (board.cols, board.rows)
    flags_sb = getattr(cv2, "CALIB_CB_NORMALIZE_IMAGE", 0)
    flags_sb |= getattr(cv2, "CALIB_CB_ACCURACY", 0)
    try:
        found, corners = cv2.findChessboardCornersSB(gray, pattern, flags=flags_sb)
        if found and corners is not None and len(corners) == board.cols * board.rows:
            return corners.astype("float32").reshape(-1, 2), "findChessboardCornersSB"
    except (AttributeError, cv2.error):
        pass
    flags = getattr(cv2, "CALIB_CB_ADAPTIVE_THRESH", 0) | getattr(cv2, "CALIB_CB_NORMALIZE_IMAGE", 0)
    try:
        found, corners = cv2.findChessboardCorners(gray, pattern, flags=flags)
        if found and corners is not None and len(corners) == board.cols * board.rows:
            return _refine_corners(gray, corners, board), "findChessboardCorners"
    except cv2.error:
        pass
    return None, ""


def _gray_input(image: Any) -> Any:
    cv2, np = _cv2()
    value = np.asarray(image)
    if value.ndim == 3:
        if value.shape[2] == 4:
            value = cv2.cvtColor(value, cv2.COLOR_BGRA2GRAY)
        elif value.shape[2] == 3:
            value = cv2.cvtColor(value, cv2.COLOR_BGR2GRAY)
    if value.dtype != np.uint8:
        value = np.clip(value, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(value)


def detect_corners(gray_or_bgr: Any, board: BoardSpec) -> dict[str, Any]:
    """Найти внутренние углы доски и вернуть диагностику без выдуманных точек."""
    cv2, np = _cv2()
    board.validate()
    gray = _gray_input(gray_or_bgr)
    if gray.ndim != 2 or min(gray.shape) < 32:
        return {"found": False, "reason": "image_too_small", "points": None}
    attempts = [("original", gray)]
    try:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        attempts.append(("clahe", clahe.apply(gray)))
    except cv2.error:
        pass
    reasons = []
    for name, candidate in attempts:
        found, method = _find_chessboard(candidate, board)
        if found is None:
            reasons.append(name)
            continue
        ordered, orientation = canonicalize_board_order(found, board)
        return {
            "found": True,
            "reason": "",
            "points": ordered,
            "method": f"{method}:{name}",
            "orientation": orientation,
            "imageSize": [int(gray.shape[1]), int(gray.shape[0])],
        }
    return {"found": False, "reason": "not_found", "attempts": reasons, "points": None}


def points_from_four_corners(four: Sequence[Sequence[float]], board: BoardSpec, image_size: Sequence[int],
                             gray: Any | None = None) -> dict[str, Any]:
    """Развернуть четыре отмеченных угла в полную сетку доски.

    Пользователь отмечает четыре *крайних внутренних* угла в порядке
    TL → TR → BR → BL. Гомография строится по координатам сетки, поэтому
    размер квадрата нужен только для восстановления трёхмерной позы.
    """
    cv2, np = _cv2()
    board.validate()
    if len(four) != 4:
        raise ValueError("Нужно ровно четыре угла: TL, TR, BR, BL.")
    src = np.array([[0.0, 0.0], [board.cols - 1.0, 0.0],
                    [board.cols - 1.0, board.rows - 1.0], [0.0, board.rows - 1.0]], dtype=np.float32)
    try:
        dst = np.asarray(four, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("Координаты углов должны быть числами.") from exc
    if dst.shape != (4, 2) or not np.isfinite(dst).all():
        raise ValueError("Координаты углов должны быть четырьмя парами чисел.")
    width, height = int(image_size[0]), int(image_size[1])
    if width <= 0 or height <= 0:
        raise ValueError("Неизвестен размер изображения для ручной разметки.")
    if not ((dst[:, 0] >= -1).all() and (dst[:, 0] <= width).all() and
            (dst[:, 1] >= -1).all() and (dst[:, 1] <= height).all()):
        raise ValueError("Углы должны находиться внутри изображения.")
    # Проверка порядка и выпуклости: TL → TR → BR → BL.
    def cross(a, b, c):
        return float((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))

    signs = [cross(dst[i], dst[(i + 1) % 4], dst[(i + 2) % 4]) for i in range(4)]
    if not (all(value > 0 for value in signs) or all(value < 0 for value in signs)):
        raise ValueError("Точки не образуют выпуклый четырёхугольник в порядке TL, TR, BR, BL.")
    area = abs(float(cv2.contourArea(dst.reshape(-1, 1, 2)))) / float(width * height)
    if area < 0.005:
        raise ValueError("Мишень занимает слишком малую часть кадра; поднесите её ближе или отметьте точнее.")
    homography = cv2.getPerspectiveTransform(src, dst)
    grid = np.mgrid[0:board.rows, 0:board.cols].reshape(2, -1).T.astype(np.float32)
    projected = cv2.perspectiveTransform(grid.reshape(-1, 1, 2), homography).reshape(-1, 2)
    if gray is not None:
        projected = _refine_corners(_gray_input(gray), projected, board)
    ordered, orientation = canonicalize_board_order(projected, board)
    return {
        "points": ordered,
        "method": "manual_four_corners",
        "orientation": orientation,
        "coverage": area,
    }


def board_coverage(points: Any, image_size: Sequence[int]) -> float:
    cv2, np = _cv2()
    value = np.asarray(points, dtype=np.float32)
    if value.size == 0:
        return 0.0
    area = abs(float(cv2.contourArea(cv2.convexHull(value.reshape(-1, 1, 2)))))
    return area / float(max(1, int(image_size[0]) * int(image_size[1])))


def _span(points: Any, image_size: Sequence[int]) -> tuple[float, float]:
    np = _cv2()[1]
    value = np.asarray(points, dtype=np.float64)
    return (float(value[:, 0].max() - value[:, 0].min()) / max(1, int(image_size[0])),
            float(value[:, 1].max() - value[:, 1].min()) / max(1, int(image_size[1])))


# --------------------------------------------------------------------------
# Калибровка
# --------------------------------------------------------------------------


def _camera_calibration(views: Iterable[dict[str, Any]], key: str, board: BoardSpec,
                        image_size: Sequence[int]) -> dict[str, Any]:
    cv2, np = _cv2()
    object_points = board_object_points(board)
    objpoints = []
    imgpoints = []
    for view in views:
        points = np.asarray(view[key], dtype=np.float32)
        if points.shape != (board.rows * board.cols, 2):
            raise ValueError("Внутренняя ошибка: неверное число углов в виде.")
        objpoints.append(object_points.copy())
        imgpoints.append(points.reshape(-1, 1, 2))
    flags = getattr(cv2, "CALIB_FIX_K3", 0)
    criteria = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 100, 1e-9)
    try:
        result = cv2.calibrateCameraExtended(objpoints, imgpoints, tuple(int(v) for v in image_size),
                                             None, None, flags=flags, criteria=criteria)
        rms, matrix, distortion, rvecs, tvecs, std_intr, _std_extr, per_view = result
    except (AttributeError, cv2.error):
        rms, matrix, distortion, rvecs, tvecs = cv2.calibrateCamera(
            objpoints, imgpoints, tuple(int(v) for v in image_size), None, None,
            flags=flags, criteria=criteria)
        std_intr = None
        per_view = [
            float(np.sqrt(np.mean(np.sum((cv2.projectPoints(object_points, rvecs[index], tvecs[index],
                                                            matrix, distortion)[0].reshape(-1, 2) -
                                           imgpoints[index].reshape(-1, 2)) ** 2, axis=1))))
            for index in range(len(imgpoints))
        ]
    per_view = [float(value) for value in np.asarray(per_view).reshape(-1)]
    if not np.isfinite(matrix).all() or not np.isfinite(distortion).all() or not math.isfinite(float(rms)):
        raise ValueError("OpenCV вернул нечисловые параметры камеры.")
    return {
        "matrix": np.asarray(matrix, dtype=np.float64),
        "distortion": np.asarray(distortion, dtype=np.float64).reshape(-1),
        "rms": float(rms),
        "meanError": float(np.mean(per_view)) if per_view else float(rms),
        "maxError": float(np.max(per_view)) if per_view else float(rms),
        "perViewErrors": per_view,
        "stdIntrinsics": None if std_intr is None else np.asarray(std_intr, dtype=np.float64).reshape(-1).tolist(),
        "rvecs": [np.asarray(value, dtype=np.float64).reshape(3) for value in rvecs],
        "tvecs": [np.asarray(value, dtype=np.float64).reshape(3) for value in tvecs],
    }


def _epipolar_errors(points_a: Any, points_b: Any, fundamental: Any) -> Any:
    _, np = _cv2()
    a = np.asarray(points_a, dtype=np.float64).reshape(-1, 2)
    b = np.asarray(points_b, dtype=np.float64).reshape(-1, 2)
    x1 = np.column_stack((a, np.ones(len(a))))
    x2 = np.column_stack((b, np.ones(len(b))))
    f = np.asarray(fundamental, dtype=np.float64)
    fx1 = x1 @ f.T
    ftx2 = x2 @ f
    numerator = np.sum(x2 * (x1 @ f.T), axis=1) ** 2
    denominator = fx1[:, 0] ** 2 + fx1[:, 1] ** 2 + ftx2[:, 0] ** 2 + ftx2[:, 1] ** 2
    valid = denominator > 1e-18
    if not valid.any():
        return np.array([], dtype=np.float64)
    return np.sqrt(numerator[valid] / denominator[valid])


def _relative_pose_errors(views: Sequence[dict[str, Any]], board: BoardSpec, left: dict[str, Any],
                          right: dict[str, Any], rotation: Any, translation: Any) -> dict[str, float]:
    cv2, np = _cv2()
    obj = board_object_points(board)
    target_rotation = np.asarray(rotation, dtype=np.float64)
    target_translation = np.asarray(translation, dtype=np.float64).reshape(3)
    translation_errors = []
    rotation_errors = []
    board_tilts = []
    for view in views:
        try:
            ok_a, rvec_a, tvec_a = cv2.solvePnP(obj, np.asarray(view["a"], dtype=np.float64).reshape(-1, 1, 2),
                                               left["matrix"], left["distortion"], flags=cv2.SOLVEPNP_ITERATIVE)
            ok_b, rvec_b, tvec_b = cv2.solvePnP(obj, np.asarray(view["b"], dtype=np.float64).reshape(-1, 1, 2),
                                               right["matrix"], right["distortion"], flags=cv2.SOLVEPNP_ITERATIVE)
            if not ok_a or not ok_b:
                continue
            rot_a = cv2.Rodrigues(rvec_a)[0]
            rot_b = cv2.Rodrigues(rvec_b)[0]
            rel_rotation = rot_a @ rot_b.T
            rel_translation = np.asarray(tvec_a).reshape(3) - rel_rotation @ np.asarray(tvec_b).reshape(3)
            translation_errors.append(float(np.linalg.norm(rel_translation - target_translation)))
            rotation_errors.append(_rotation_angle_deg(rel_rotation @ target_rotation.T))
            normal = rot_a[:, 2]
            board_tilts.append(float(math.degrees(math.acos(min(1.0, max(-1.0, abs(float(normal[2]))))))))
        except cv2.error:
            continue
    if not translation_errors:
        return {"translationSpreadM": float("inf"), "rotationSpreadDeg": float("inf"),
                "tiltMinDeg": 0.0, "tiltMaxDeg": 0.0}
    return {
        "translationSpreadM": float(np.std(translation_errors)),
        "translationMaxM": float(np.max(translation_errors)),
        "rotationSpreadDeg": float(np.std(rotation_errors)),
        "rotationMaxDeg": float(np.max(rotation_errors)),
        "tiltMinDeg": float(np.min(board_tilts)),
        "tiltMaxDeg": float(np.max(board_tilts)),
    }


def calibrate_stereo(views: Sequence[dict[str, Any]], board: BoardSpec,
                     image_size: Sequence[int] = WORKING_SIZE) -> dict[str, Any]:
    """Полная калибровка: обе камеры, взаимное положение, качество, ректификация.

    ``views`` — последовательность словарей с полями ``a``, ``b`` (Nx2 углы)
    и, для отчёта, ``skewMs``. Функция не сохраняет файлы и не включает
    стереоглубину: это делают сервис и интерфейс после проверки ``quality.valid``.
    """
    cv2, np = _cv2()
    board.validate()
    if len(views) < MIN_VIEWS:
        raise ValueError(f"Для калибровки нужно не менее {MIN_VIEWS} пар с доской; сейчас {len(views)}.")
    image_size = (int(image_size[0]), int(image_size[1]))
    object_points = board_object_points(board)
    objpoints = []
    imgpoints_a = []
    imgpoints_b = []
    for view in views:
        points_a = np.asarray(view["a"], dtype=np.float32)
        points_b = np.asarray(view["b"], dtype=np.float32)
        if points_a.shape != (board.rows * board.cols, 2) or points_b.shape != (board.rows * board.cols, 2):
            raise ValueError("Внутренняя ошибка: неверное число углов в паре.")
        objpoints.append(object_points.copy())
        imgpoints_a.append(points_a.reshape(-1, 1, 2))
        imgpoints_b.append(points_b.reshape(-1, 1, 2))

    left = _camera_calibration(views, "a", board, image_size)
    right = _camera_calibration(views, "b", board, image_size)
    criteria = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 200, 1e-9)
    try:
        stereo_result = cv2.stereoCalibrateExtended(
            objpoints, imgpoints_a, imgpoints_b,
            left["matrix"], left["distortion"], right["matrix"], right["distortion"],
            image_size, None, None, flags=cv2.CALIB_FIX_INTRINSIC, criteria=criteria,
        )
        stereo_rms, _, _, _, _, rotation, translation, essential, fundamental, _, _, _ = stereo_result
    except (AttributeError, cv2.error):
        stereo_rms, _, _, _, _, rotation, translation, essential, fundamental = cv2.stereoCalibrate(
            objpoints, imgpoints_a, imgpoints_b,
            left["matrix"], left["distortion"], right["matrix"], right["distortion"],
            image_size, flags=cv2.CALIB_FIX_INTRINSIC, criteria=criteria,
        )
    rotation = np.asarray(rotation, dtype=np.float64)
    translation = np.asarray(translation, dtype=np.float64).reshape(3)
    fundamental = np.asarray(fundamental, dtype=np.float64)
    baseline = float(np.linalg.norm(translation))
    if not (np.isfinite(rotation).all() and np.isfinite(translation).all() and
            np.isfinite(fundamental).all() and math.isfinite(float(stereo_rms))):
        raise ValueError("OpenCV вернул нечисловые параметры стереопары.")

    epipolar = []
    for view in views:
        epipolar.extend(_epipolar_errors(view["a"], view["b"], fundamental).tolist())
    epipolar = np.asarray(epipolar, dtype=np.float64)
    epipolar_median = float(np.median(epipolar)) if len(epipolar) else float("inf")
    epipolar_p95 = float(np.percentile(epipolar, 95)) if len(epipolar) else float("inf")
    relative = _relative_pose_errors(views, board, left, right, rotation, translation)

    all_a = np.concatenate([np.asarray(view["a"], dtype=np.float64) for view in views], axis=0)
    all_b = np.concatenate([np.asarray(view["b"], dtype=np.float64) for view in views], axis=0)
    span_au, span_av = _span(all_a, image_size)
    span_bu, span_bv = _span(all_b, image_size)
    coverage_a = [board_coverage(view["a"], image_size) for view in views]
    coverage_b = [board_coverage(view["b"], image_size) for view in views]
    coverage_mean = float((np.mean(coverage_a) + np.mean(coverage_b)) / 2.0)
    skews = [float(view.get("skewMs", 0.0) or 0.0) for view in views]

    reasons: list[str] = []
    warnings: list[str] = []
    if len(views) < RECOMMENDED_VIEWS:
        warnings.append(f"Видов {len(views)}; для устойчивой калибровки рекомендуется не менее {RECOMMENDED_VIEWS}.")
    if left["rms"] > MAX_INTRINSIC_RMS_PX:
        reasons.append(f"Ошибка репроекции камеры A {left['rms']:.2f} px > {MAX_INTRINSIC_RMS_PX} px.")
    if right["rms"] > MAX_INTRINSIC_RMS_PX:
        reasons.append(f"Ошибка репроекции камеры B {right['rms']:.2f} px > {MAX_INTRINSIC_RMS_PX} px.")
    if float(stereo_rms) > MAX_STEREO_RMS_PX:
        reasons.append(f"Ошибка стереокалибровки {float(stereo_rms):.2f} px > {MAX_STEREO_RMS_PX} px.")
    if epipolar_median > MAX_EPIPOLAR_MEDIAN_PX:
        reasons.append(f"Медианная эпиполярная ошибка {epipolar_median:.2f} px > {MAX_EPIPOLAR_MEDIAN_PX} px.")
    if epipolar_p95 > MAX_EPIPOLAR_P95_PX:
        reasons.append(f"95-й процентиль эпиполярной ошибки {epipolar_p95:.2f} px > {MAX_EPIPOLAR_P95_PX} px.")
    if baseline < MIN_BASELINE_M:
        reasons.append(f"Базис {baseline * 1000:.0f} мм меньше {MIN_BASELINE_M * 1000:.0f} мм: глубина по такой паре ненадёжна.")
    if baseline > MAX_BASELINE_M:
        reasons.append(f"Базис {baseline:.2f} м превышает {MAX_BASELINE_M:.1f} м: проверьте единицы и крепление.")
    if relative["translationSpreadM"] > MAX_TRANSLATION_SPREAD_M:
        reasons.append("Взаимное положение камер по видам расходится сильнее допустимого; "
                       "камеры или мишень двигались во время съёмки.")
    if relative["rotationSpreadDeg"] > MAX_ROTATION_SPREAD_DEG:
        reasons.append("Ориентация камер по видам расходится сильнее допустимого; "
                       "камеры или мишень двигались во время съёмки.")
    if min(span_au, span_av, span_bu, span_bv) < min(MIN_SPAN_U, MIN_SPAN_V):
        reasons.append("Мишень покрывает слишком малую часть кадра: не хватает разных положений доски.")
    if coverage_mean < MIN_MEAN_COVERAGE:
        reasons.append("Средняя площадь мишени в кадре слишком мала.")
    if relative["tiltMaxDeg"] < MIN_MAX_BOARD_TILT_DEG:
        reasons.append("Во всех видах доска почти параллельна камере: нужны наклоны доски не менее "
                       f"{MIN_MAX_BOARD_TILT_DEG:.0f}° для устойчивой оценки фокусного расстояния.")
    if max(skews) > MAX_VIEW_SKEW_MS:
        reasons.append(f"Разница времени в паре достигала {max(skews):.0f} мс; для калибровки нужны неподвижные камеры и доска.")
    if board.target_kind == "screen":
        warnings.append("Доска показана на экране: возможны блики, неравномерный масштаб и ШИМ-подсветка. "
                        "Измерьте сторону квадрата линейкой и выключите автояркость/HDR.")
    if left["maxError"] > 2.0 * MAX_INTRINSIC_RMS_PX or right["maxError"] > 2.0 * MAX_INTRINSIC_RMS_PX:
        warnings.append("В отдельных видах ошибка репроекции значительно выше средней; удалите такие пары и повторите.")

    # Ректификация. alpha=-1 сохраняет все пиксели (с чёрными полями), чтобы
    # не выдавать обрезанное поле зрения за полное. Допустимая область
    # вычисляется маской, а не подразумевается.
    try:
        rectification = cv2.stereoRectify(
            left["matrix"], left["distortion"], right["matrix"], right["distortion"],
            image_size, rotation, translation,
            flags=cv2.CALIB_ZERO_DISPARITY, alpha=-1.0, newImageSize=image_size,
        )
        rect_left, rect_right, proj_left, proj_right, q_matrix, roi_left, roi_right = rectification
    except cv2.error as exc:
        raise ValueError(f"Не удалось построить ректификацию: {exc}") from exc
    proj_left = np.asarray(proj_left, dtype=np.float64)
    proj_right = np.asarray(proj_right, dtype=np.float64)
    if abs(float(proj_right[1, 3])) > 1e-6:
        reasons.append("Ректификация не обеспечила нулевое вертикальное смещение; калибровка отклонена.")
    fx_rect = float(proj_left[0, 0])
    fy_rect = float(proj_left[1, 1])
    if fx_rect <= 0 or fy_rect <= 0:
        reasons.append("Некорректные фокусные расстояния после ректификации.")

    quality = {
        "valid": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "views": len(views),
        "recommendedViews": RECOMMENDED_VIEWS,
        "boardRmsPx": {"a": left["rms"], "b": right["rms"]},
        "boardMeanErrorPx": {"a": left["meanError"], "b": right["meanError"]},
        "boardMaxErrorPx": {"a": left["maxError"], "b": right["maxError"]},
        "stereoRmsPx": float(stereo_rms),
        "epipolarMedianPx": epipolar_median,
        "epipolarP95Px": epipolar_p95,
        "baselineM": baseline,
        "translationSpreadM": relative["translationSpreadM"],
        "translationMaxM": relative.get("translationMaxM"),
        "rotationSpreadDeg": relative["rotationSpreadDeg"],
        "rotationMaxDeg": relative.get("rotationMaxDeg"),
        "boardTiltMinDeg": relative["tiltMinDeg"],
        "boardTiltMaxDeg": relative["tiltMaxDeg"],
        "spanAU": span_au, "spanAV": span_av, "spanBU": span_bu, "spanBV": span_bv,
        "coverageMean": coverage_mean,
        "maxPairSkewMs": max(skews) if skews else 0.0,
        "depthResolutionAt1mMm": (1000.0 ** 2) / (fx_rect * baseline) if fx_rect * baseline > 0 else None,
        "depthResolutionAt3mMm": (3000.0 ** 2) / (fx_rect * baseline) if fx_rect * baseline > 0 else None,
        "thresholds": {
            "intrinsicRmsPx": MAX_INTRINSIC_RMS_PX,
            "stereoRmsPx": MAX_STEREO_RMS_PX,
            "epipolarMedianPx": MAX_EPIPOLAR_MEDIAN_PX,
            "epipolarP95Px": MAX_EPIPOLAR_P95_PX,
            "baselineM": [MIN_BASELINE_M, MAX_BASELINE_M],
            "translationSpreadM": MAX_TRANSLATION_SPREAD_M,
            "rotationSpreadDeg": MAX_ROTATION_SPREAD_DEG,
            "minViews": MIN_VIEWS,
        },
    }
    return {
        "board": board.to_dict(),
        "imageSize": [image_size[0], image_size[1]],
        "intrinsics": {
            "a": {"K": left["matrix"], "D": left["distortion"], "rms": left["rms"],
                   "meanError": left["meanError"], "maxError": left["maxError"],
                   "perViewErrors": left["perViewErrors"]},
            "b": {"K": right["matrix"], "D": right["distortion"], "rms": right["rms"],
                   "meanError": right["meanError"], "maxError": right["maxError"],
                   "perViewErrors": right["perViewErrors"]},
        },
        "extrinsics": {"R": rotation, "T": translation, "baselineM": baseline,
                       "essential": np.asarray(essential, dtype=np.float64),
                       "fundamental": fundamental},
        "rectification": {"R1": np.asarray(rect_left, dtype=np.float64),
                          "R2": np.asarray(rect_right, dtype=np.float64),
                          "P1": proj_left, "P2": proj_right,
                          "Q": np.asarray(q_matrix, dtype=np.float64),
                          "fx": fx_rect, "fy": fy_rect,
                          "roi1": list(roi_left), "roi2": list(roi_right)},
        "quality": quality,
        "verifiedOnHardware": False,
        "hardwareSync": False,
        "kinectDepthUsed": False,
    }


# --------------------------------------------------------------------------
# Ректификация и сериализация
# --------------------------------------------------------------------------


def build_rectification_maps(calibration: dict[str, Any]) -> dict[str, Any]:
    cv2, np = _cv2()
    width, height = (int(v) for v in calibration["imageSize"])
    rect = calibration["rectification"]
    maps = {}
    for key, rotation in (("a", rect["R1"]), ("b", rect["R2"])):
        matrix = np.asarray(calibration["intrinsics"][key]["K"], dtype=np.float64)
        distortion = np.asarray(calibration["intrinsics"][key]["D"], dtype=np.float64)
        projection = np.asarray(rect["P1" if key == "a" else "P2"], dtype=np.float64)
        map_x, map_y = cv2.initUndistortRectifyMap(matrix, distortion, np.asarray(rotation, dtype=np.float64),
                                                   projection, (width, height), cv2.CV_32FC1)
        maps[key] = (map_x, map_y)
    return maps


def rectify_image(image: Any, maps: dict[str, Any], key: str) -> Any:
    cv2, np = _cv2()
    value = np.asarray(image)
    map_x, map_y = maps[key]
    return cv2.remap(value, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def build_rectification_validity(maps: dict[str, Any], source_valid: dict[str, Any],
                                 image_size: Sequence[int]) -> dict[str, Any]:
    """Маска пикселей, которые реально есть в исходном кадре после ректификации.

    Чёрные поля letterbox — тоже исходные пиксели; отдельная маска содержимого
    камеры передаётся в ``source_valid``. Так чёрная рамка не превращается в
    валидную глубину.
    """
    cv2, np = _cv2()
    width, height = (int(v) for v in image_size)
    result = {}
    for key in ("a", "b"):
        mask = np.asarray(source_valid.get(key), dtype=np.uint8)
        if mask.shape != (height, width):
            raise ValueError("Маска содержимого кадра не совпадает с рабочим размером.")
        mask = np.where(mask > 0, 255, 0).astype(np.uint8)
        map_x, map_y = maps[key]
        remapped = cv2.remap(mask, map_x, map_y, interpolation=cv2.INTER_NEAREST,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        result[key] = remapped > 0
    return result


def _jsonable(value: Any) -> Any:
    np = _cv2()[1]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, (np.floating, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Калибровка содержит нечисловое значение.")
        return number
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (bool, str)) or value is None:
        return value
    return value


def make_calibration_record(calibration: dict[str, Any], *, cameras: dict[str, Any],
                            kinect: dict[str, Any], view_skews: Sequence[float] | None = None,
                            clock_note: str = "software_monotonic_epoch") -> dict[str, Any]:
    """Собрать сохраняемую запись с подписью конфигурации и честными флагами."""
    payload = _jsonable(calibration)
    payload["schemaVersion"] = SCHEMA_VERSION
    payload["createdAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload["cameras"] = _jsonable(cameras)
    payload["kinect"] = _jsonable(kinect)
    payload["viewSkewsMs"] = _jsonable(list(view_skews or []))
    payload["timestampSource"] = clock_note
    payload["exposureTimestamps"] = False
    payload["valid"] = bool(payload.get("quality", {}).get("valid"))
    payload["invalidatedAt"] = None
    payload["invalidationReason"] = None
    if not payload["valid"]:
        # Недействительная калибровка не должна попасть в активную.
        raise ValueError("Нельзя сохранить недействительную калибровку как активную.")
    canonical = json.dumps({
        "board": payload["board"],
        "cameras": payload["cameras"],
        "intrinsics": payload["intrinsics"],
        "extrinsics": payload["extrinsics"],
        "createdAt": payload["createdAt"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return payload


def validate_calibration_record(record: dict[str, Any]) -> dict[str, Any]:
    """Проверить загруженный JSON, не доверяя файлу на диске."""
    _, np = _cv2()
    if not isinstance(record, dict) or int(record.get("schemaVersion", 0)) != SCHEMA_VERSION:
        raise ValueError("Неподдерживаемая версия файла калибровки.")
    if not record.get("valid"):
        raise ValueError("Калибровка помечена недействительной.")
    image_size = record.get("imageSize")
    if not (isinstance(image_size, list) and len(image_size) == 2 and
            all(isinstance(v, int) and 64 <= v <= 4096 for v in image_size)):
        raise ValueError("Некорректный размер изображения калибровки.")
    for key in ("a", "b"):
        intr = record["intrinsics"][key]
        matrix = np.asarray(intr["K"], dtype=np.float64)
        distortion = np.asarray(intr["D"], dtype=np.float64)
        if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or matrix[0, 0] <= 0 or matrix[1, 1] <= 0:
            raise ValueError(f"Некорректная матрица камеры {key}.")
        if distortion.ndim != 1 or not np.isfinite(distortion).all():
            raise ValueError(f"Некорректные коэффициенты дисторсии {key}.")
    extr = record["extrinsics"]
    rotation = np.asarray(extr["R"], dtype=np.float64)
    translation = np.asarray(extr["T"], dtype=np.float64).reshape(-1)
    if rotation.shape != (3, 3) or translation.size != 3 or not np.isfinite(rotation).all() or not np.isfinite(translation).all():
        raise ValueError("Некорректное взаимное положение камер.")
    if abs(float(np.linalg.det(rotation)) - 1.0) > 1e-3:
        raise ValueError("Матрица поворота калибровки не является вращением.")
    baseline = float(np.linalg.norm(translation))
    if not MIN_BASELINE_M <= baseline <= MAX_BASELINE_M:
        raise ValueError("Базис калибровки вне допустимого диапазона.")
    rect = record.get("rectification") or {}
    for key in ("R1", "R2", "P1", "P2", "Q"):
        value = np.asarray(rect.get(key), dtype=np.float64)
        if value.shape not in ((3, 3), (3, 4), (4, 4)) or not np.isfinite(value).all():
            raise ValueError(f"Некорректная ректификация: {key}.")
    quality = record.get("quality") or {}
    if not quality.get("valid"):
        raise ValueError("Файл калибровки не содержит действительной проверки качества.")
    return record


def calibration_summary(record: dict[str, Any] | None) -> dict[str, Any]:
    """Краткая сводка для интерфейса; матрицы не пересылаются в UI."""
    if not record:
        return {"present": False, "valid": False, "state": "missing", "reasons": ["Калибровка не выполнялась."]}
    quality = record.get("quality") or {}
    return {
        "present": True,
        "valid": bool(record.get("valid") and quality.get("valid")),
        "state": "valid" if record.get("valid") and quality.get("valid") else "invalid",
        "id": record.get("id"),
        "createdAt": record.get("createdAt"),
        "invalidatedAt": record.get("invalidatedAt"),
        "invalidationReason": record.get("invalidationReason"),
        "board": record.get("board"),
        "cameras": record.get("cameras"),
        "kinect": record.get("kinect"),
        "quality": quality,
        "baselineM": (record.get("extrinsics") or {}).get("baselineM"),
        "rectifiedSize": record.get("imageSize"),
        "verifiedOnHardware": False,
        "hardwareSync": False,
        "kinectDepthUsed": False,
        "exposureTimestamps": False,
    }


__all__ = [
    "BoardSpec",
    "StereoUnavailable",
    "MAX_VIEW_SKEW_MS",
    "MIN_VIEWS",
    "RECOMMENDED_VIEWS",
    "WORKING_SIZE",
    "board_object_points",
    "board_coverage",
    "build_rectification_maps",
    "build_rectification_validity",
    "calibrate_stereo",
    "calibration_summary",
    "canonicalize_board_order",
    "detect_corners",
    "make_calibration_record",
    "points_from_four_corners",
    "rectify_image",
    "validate_calibration_record",
]
