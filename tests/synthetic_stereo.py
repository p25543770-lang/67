"""Синтетическая стереопара для тестов: доска, две камеры, известный базис.

Тесты не должны требовать настоящих Kinect и Logitech. Модуль рисует
шахматную доску в двух ракурсах с известными внутренними параметрами и
положением камер, чтобы калибровку и глубину можно было проверить численно.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

try:  # cv2 нужен только стереотестам; остальные тесты его не импортируют
    import cv2
except Exception:  # pragma: no cover - отсутствие OpenCV проверяется отдельно
    cv2 = None  # type: ignore[assignment]


WORKING_SIZE = (640, 480)


def require_cv2() -> None:
    if cv2 is None:  # pragma: no cover - зависит от окружения
        raise RuntimeError("OpenCV недоступен: стереотесты требуют opencv-python.")


@dataclass
class SyntheticCamera:
    """Одна камера синтетической пары."""

    matrix: np.ndarray
    distortion: np.ndarray
    center: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float64))
    name: str = "camera"


def default_pair(baseline_m: float = 0.10) -> tuple[SyntheticCamera, SyntheticCamera]:
    left = SyntheticCamera(
        matrix=np.array([[618.0, 0.0, 318.0], [0.0, 615.0, 238.0], [0.0, 0.0, 1.0]]),
        distortion=np.array([0.03, -0.04, 0.0002, -0.0001, 0.0]),
        center=np.zeros(3, dtype=np.float64),
        name="a",
    )
    right = SyntheticCamera(
        matrix=np.array([[608.0, 0.0, 330.0], [0.0, 606.0, 232.0], [0.0, 0.0, 1.0]]),
        distortion=np.array([-0.02, 0.02, 0.0003, 0.0002, 0.0]),
        center=np.array([baseline_m, 0.0, 0.0]),
        name="b",
    )
    return left, right


def board_texture(board, pixels_per_square: int = 200) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Шахматное поле (cols+1)×(rows+1) и его внешние углы в метрах."""
    require_cv2()
    square = board.square_m
    rows, cols = board.rows + 1, board.cols + 1
    texture = np.full((rows * pixels_per_square, cols * pixels_per_square), 255, dtype=np.uint8)
    for row in range(rows):
        for col in range(cols):
            if (row + col) % 2 == 0:
                texture[row * pixels_per_square:(row + 1) * pixels_per_square,
                        col * pixels_per_square:(col + 1) * pixels_per_square] = 0
    outer = np.array(
        [[-square, -square, 0.0],
         [board.cols * square, -square, 0.0],
         [board.cols * square, board.rows * square, 0.0],
         [-square, board.rows * square, 0.0]],
        dtype=np.float32,
    )
    texture_corners = np.array(
        [[0.0, 0.0],
         [(board.cols + 1) * pixels_per_square, 0.0],
         [(board.cols + 1) * pixels_per_square, (board.rows + 1) * pixels_per_square],
         [0.0, (board.rows + 1) * pixels_per_square]],
        dtype=np.float32,
    )
    return texture, outer, texture_corners


def render_board(board, rvec, tvec, camera: SyntheticCamera, *, size=WORKING_SIZE,
                 seed: int = 0, background: int = 60) -> np.ndarray:
    """Нарисовать доску так, как её увидит камера ``camera``."""
    require_cv2()
    width, height = size
    rng = np.random.default_rng(seed)
    texture, outer, texture_corners = board_texture(board)
    image = np.full((height, width), background, dtype=np.float32)
    image = 0.6 * image + 0.4 * rng.integers(40, 90, (height, width)).astype(np.float32)
    rvec = np.asarray(rvec, dtype=np.float64)
    tvec = np.asarray(tvec, dtype=np.float64) - camera.center
    projected, _ = cv2.projectPoints(outer, rvec, tvec, camera.matrix, camera.distortion)
    projected = projected.reshape(-1, 2).astype(np.float32)
    homography = cv2.getPerspectiveTransform(texture_corners, projected)
    warped = cv2.warpPerspective(texture, homography, (width, height), flags=cv2.INTER_LINEAR,
                                 borderValue=(background, background, background))
    mask = cv2.warpPerspective(np.full_like(texture, 255), homography, (width, height),
                               flags=cv2.INTER_NEAREST, borderValue=0)
    image = np.where(mask > 0, warped, image).astype(np.float32)
    image = cv2.GaussianBlur(image, (3, 3), 0.6)
    image = np.clip(image + rng.normal(0, 2.0, (height, width)), 0, 255).astype(np.uint8)
    return image


def board_pose(index: int) -> tuple[np.ndarray, np.ndarray]:
    """Разные наклоны и удаления доски: нужны и для внутренних параметров, и для базиса."""
    rx = math.radians([-24, -14, 0, 14, 26, -20, 10, 22, -6, 18][index % 10])
    ry = math.radians([16, 0, -20, -8, 14, 26, -16, 4, 22, -12][index % 10])
    rz = math.radians([-8, 5, 10, -6, 0, 12, -10, 4, 7, -3][index % 10])
    tvec = np.array(
        [0.02 * [-0.06, 0.04, 0.0, 0.05, -0.03, 0.02, -0.04, 0.03, 0.0, 0.05][index % 10],
         0.02 * [0.02, -0.03, 0.04, 0.0, -0.02, 0.03, -0.05, 0.01, 0.02, -0.03][index % 10],
         0.80 + 0.04 * (index % 4)],
        dtype=np.float64,
    )
    return np.array([rx, ry, rz], dtype=np.float64), tvec


def render_pair(board, index: int, cameras=None, *, seed: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    require_cv2()
    left, right = cameras or default_pair()
    rvec, tvec = board_pose(index)
    seed = index * 2 if seed is None else seed
    return (render_board(board, rvec, tvec, left, seed=seed),
            render_board(board, rvec, tvec, right, seed=seed + 1))


def calibration_views(board, *, count: int = 10, cameras=None, skew_ms: float = 1.5) -> list[dict]:
    """Готовые пары углов доски для ``calibrate_stereo``."""
    require_cv2()
    from scan_pipeline.stereo_calibration import detect_corners

    views = []
    for index in range(count):
        image_a, image_b = render_pair(board, index, cameras)
        found_a = detect_corners(image_a, board)
        found_b = detect_corners(image_b, board)
        if not (found_a["found"] and found_b["found"]):
            continue
        views.append({"a": found_a["points"], "b": found_b["points"], "skewMs": skew_ms})
    return views
