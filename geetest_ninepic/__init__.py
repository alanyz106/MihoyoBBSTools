"""
极验 v3 九宫格图片验证码识别模块

使用 Yiffyi 发布的 ResNet18 ONNX 模型和 hint 图片库，对九宫格验证码进行
90 类图像分类，找出与问题小图同类的格子。

模型来源：https://huggingface.co/Yiffyi/resnet-geetest-v3-nine-pic
数据集来源：https://huggingface.co/datasets/Yiffyi/dataset-geetest-v3-nine-pic
基于项目：https://github.com/Yiffyi/geetest-v3-nine-pic-crack（AGPL-3.0）

本模块仅做模型推理和坐标计算，协议交互仍复用 geetest_cv.crack.Crack。
"""
import json
import os
from typing import List, Tuple

import cv2
import numpy as np
import onnxruntime

from loghelper import log

# 九宫格 9 张候选图在原始 344x384 图片中的坐标（左上、右下）
_OPTION_COORDS = [
    [[0, 0], [112, 112]],
    [[116, 0], [228, 112]],
    [[232, 0], [344, 112]],
    [[0, 116], [112, 228]],
    [[116, 116], [228, 228]],
    [[232, 116], [344, 228]],
    [[0, 232], [112, 344]],
    [[116, 232], [228, 344]],
    [[232, 232], [344, 344]],
]

# 问题小图坐标
_HINT_COORDS = [[2, 344], [42, 384]]

_MODEL_PATH = None
_HINT_DIR = None
_model = None
_hint_images = []


def _model_dir() -> str:
    return os.getenv(
        "GEETEST_CV_MODEL_DIR",
        os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "models"),
    )


def is_available() -> bool:
    """检查九宫格模型是否可用"""
    global _MODEL_PATH, _HINT_DIR
    model_dir = _model_dir()
    _MODEL_PATH = os.path.join(model_dir, "geetest_ninepic", "model.onnx")
    _HINT_DIR = os.path.join(model_dir, "geetest_ninepic", "hints")
    if not os.path.exists(_MODEL_PATH):
        return False
    if not os.path.isdir(_HINT_DIR):
        return False
    hint_files = [f for f in os.listdir(_HINT_DIR) if f.endswith(".jpg")]
    return len(hint_files) >= 90


def _load_model():
    global _model
    if _model is None:
        _model = onnxruntime.InferenceSession(_MODEL_PATH)
    return _model


def _load_hints():
    global _hint_images
    if not _hint_images:
        for i in range(90):
            path = os.path.join(_HINT_DIR, f"hint_{i}.jpg")
            img = cv2.imread(path)
            if img is None:
                raise RuntimeError(f"无法加载 hint 图片: {path}")
            _hint_images.append(img)
    return _hint_images


def _preprocess(image: np.ndarray) -> np.ndarray:
    """模型输入预处理：resize -> RGB -> normalize -> CHW"""
    image = cv2.resize(image, (224, 224))
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    arr = image.astype(np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    arr = np.transpose(arr, (2, 0, 1))
    return arr


def _crop_options_and_hint(raw_bytes: bytes):
    """把原始验证码图切成 9 张候选图和 1 张问题小图"""
    img = cv2.imdecode(np.frombuffer(raw_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("无法解码验证码图片")
    options = []
    for coords in _OPTION_COORDS:
        x1, y1 = coords[0]
        x2, y2 = coords[1]
        options.append(img[y1:y2, x1:x2])
    x1, y1 = _HINT_COORDS[0]
    x2, y2 = _HINT_COORDS[1]
    hint = img[y1:y2, x1:x2]
    return options, hint


def _guess_category_id(hint_img: np.ndarray) -> Tuple[int, float]:
    """用 cv2 标准差匹配问题小图属于 90 个类别中的哪一个"""
    hints = _load_hints()
    diffs = [cv2.absdiff(h, hint_img) for h in hints]
    stds = np.std(diffs, axis=(1, 2, 3))
    idx = int(np.argmin(stds))
    return idx, float(stds[idx])


def _predict(options: List[np.ndarray]) -> List[int]:
    """对 9 张候选图做 90 类分类，返回每张的类别编号"""
    model = _load_model()
    inputs = np.stack([_preprocess(opt) for opt in options])
    outputs = model.run(None, {model.get_inputs()[0].name: inputs})[0]
    return np.argmax(outputs, axis=1).tolist()


def predict_points(raw_bytes: bytes) -> List[Tuple[int, int, int]]:
    """
    对原始验证码图进行九宫格识别

    :return: [(col, row, category_id), ...]，col/row 从 1 开始，九宫格坐标
    """
    options, hint = _crop_options_and_hint(raw_bytes)
    cat_idx, cat_std = _guess_category_id(hint)
    log.debug(f"九宫格 hint 匹配类别: {cat_idx}, std: {cat_std:.4f}")
    predictions = _predict(options)
    log.debug(f"九宫格候选图预测: {predictions}")
    points = []
    for idx, pred in enumerate(predictions):
        if pred == cat_idx:
            col = idx % 3 + 1
            row = idx // 3 + 1
            points.append((col, row, cat_idx))
    return points


def solve_with_crack(crack, raw_bytes: bytes) -> bool:
    """
    用已经初始化好的 Crack 实例提交九宫格答案

    :param crack: geetest_cv.crack.Crack 实例，已完成 get_c_s/ajax
    :param raw_bytes: 原始验证码图片 bytes
    :return: verify 是否返回 success
    """
    points = predict_points(raw_bytes)
    if not points:
        log.warning("九宫格模型未识别到匹配项")
        return False
    # 极验提交格式：col_row，例如 ["1_1", "2_1", "3_1"]
    submission = [f"{col}_{row}" for col, row, _ in points]
    log.info(f"九宫格模型提交: {submission}")
    result = crack.verify(submission)
    data = json.loads(result).get("data", {})
    return data.get("result") == "success"
