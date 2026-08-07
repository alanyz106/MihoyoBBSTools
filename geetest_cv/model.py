"""
极验 v3 文字点选验证码破解 - CV 识别层

改造自 ravizhan/geetest-v3-click-crack (AGPL-3.0)
用 YOLOv8 检测文字位置 + 孪生网络匹配点击顺序。
"""
import os
from datetime import datetime

import cv2
import numpy as np
import onnxruntime

from loghelper import log

# 模型文件目录：优先环境变量，否则用项目根目录下的 models/
_DEFAULT_MODEL_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "models")
_MODEL_DIR = os.getenv("GEETEST_CV_MODEL_DIR", _DEFAULT_MODEL_DIR)

_SAVE_DEBUG = os.getenv("GEETEST_CV_DEBUG", "").strip().lower() in ("1", "true", "yes")
_DEBUG_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "log", "cv_debug")


def _debug_path(prefix: str, retry: int, ext: str = "png") -> str:
    os.makedirs(_DEBUG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%m%d_%H%M%S_%f")[:-3]
    return os.path.join(_DEBUG_DIR, f"{prefix}_{ts}_r{retry}.{ext}")


class Model:
    def __init__(self):
        self.img = None
        yolo_path = os.path.join(_MODEL_DIR, "yolov8s.onnx")
        siamese_path = os.path.join(_MODEL_DIR, "siamese.onnx")
        if not os.path.exists(yolo_path) or not os.path.exists(siamese_path):
            raise FileNotFoundError(
                f"模型文件不存在，请在 {_MODEL_DIR} 放置 yolov8s.onnx 和 siamese.onnx，"
                f"或设置环境变量 GEETEST_CV_MODEL_DIR")
        self.yolo = onnxruntime.InferenceSession(yolo_path)
        self.Siamese = onnxruntime.InferenceSession(siamese_path)
        self.classes = ["big", "small"]
        log.debug("CV 模型加载完成")

    def detect(self, img: bytes, retry: int = 0):
        confidence_thres = 0.8
        iou_thres = 0.8
        model_inputs = self.yolo.get_inputs()
        input_shape = model_inputs[0].shape
        input_width = input_shape[2]
        input_height = input_shape[3]
        self.img = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_ANYCOLOR)
        img_height, img_width = self.img.shape[:2]
        img = cv2.cvtColor(self.img, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, (input_height, input_width))
        image_data = np.array(img) / 255.0
        image_data = np.transpose(image_data, (2, 0, 1))
        image_data = np.expand_dims(image_data, axis=0).astype(np.float32)
        input = {model_inputs[0].name: image_data}
        output = self.yolo.run(None, input)
        outputs = np.transpose(np.squeeze(output[0]))
        rows = outputs.shape[0]
        boxes, scores, class_ids = [], [], []
        x_factor = img_width / input_width
        y_factor = img_height / input_height
        for i in range(rows):
            classes_scores = outputs[i][4:]
            max_score = np.amax(classes_scores)
            if max_score >= confidence_thres:
                class_id = np.argmax(classes_scores)
                x, y, w, h = outputs[i][0], outputs[i][1], outputs[i][2], outputs[i][3]
                left = int((x - w / 2) * x_factor)
                top = int((y - h / 2) * y_factor)
                width = int(w * x_factor)
                height = int(h * y_factor)
                class_ids.append(class_id)
                scores.append(max_score)
                boxes.append([left, top, width, height])
        indices = cv2.dnn.NMSBoxes(boxes, scores, confidence_thres, iou_thres)
        new_boxes = [boxes[i] for i in indices]
        small_imgs, big_img_boxes = {}, []
        for i in new_boxes:
            cropped = self.img[i[1]: i[1] + i[3], i[0]: i[0] + i[2]]
            if cropped.shape[0] < 35 and cropped.shape[1] < 35:
                small_imgs[i[0]] = cropped
            else:
                big_img_boxes.append(i)

        # 保存调试图：在原图上画出所有检测框，绿色=big，红色=small
        if _SAVE_DEBUG:
            debug_img = self.img.copy()
            for box in big_img_boxes:
                cv2.rectangle(debug_img, (box[0], box[1]), (box[0] + box[2], box[1] + box[3]), (0, 255, 0), 2)
            for x, sm in small_imgs.items():
                # small_imgs 只保留了裁剪图，没有原始 box，这里用 key(x) 和裁剪高度反推
                h, w = sm.shape[:2]
                cv2.rectangle(debug_img, (x, 0), (x + w, h), (0, 0, 255), 2)
            cv2.imwrite(_debug_path("detect", retry, "jpg"), debug_img)

        return small_imgs, big_img_boxes

    @staticmethod
    def preprocess_image(img, size=(105, 105)):
        img_resized = cv2.resize(img, size)
        img_normalized = np.array(img_resized) / 255.0
        img_transposed = np.transpose(img_normalized, (2, 0, 1))
        img_expanded = np.expand_dims(img_transposed, axis=0).astype(np.float32)
        return img_expanded

    def siamese(self, small_imgs, big_img_boxes, retry: int = 0):
        preprocessed_small_imgs = {i: self.preprocess_image(small_imgs[i]) for i in sorted(small_imgs)}
        result_list = []
        result_scores = []
        for i in sorted(preprocessed_small_imgs):
            image_data_1 = preprocessed_small_imgs[i]
            best_box = None
            best_score = -1
            for box in big_img_boxes:
                if [box[0], box[1]] in result_list:
                    continue
                cropped = self.img[box[1]: box[1] + box[3], box[0]: box[0] + box[2]]
                image_data_2 = self.preprocess_image(cropped)
                inputs = {'input': image_data_1, "input.53": image_data_2}
                output = self.Siamese.run(None, inputs)
                output_sigmoid = 1 / (1 + np.exp(-output[0]))
                res = output_sigmoid[0][0]
                if res > best_score:
                    best_score = res
                    best_box = [box[0], box[1]]
                if res >= 0.1:
                    result_list.append([box[0], box[1]])
                    result_scores.append(round(float(res), 4))
                    break
            # 如果当前 small 没有触发 0.1 阈值，记录最佳匹配分数用于调试
            if best_box and best_box not in result_list:
                log.debug(f"CV small@{i} 最佳匹配分数: {best_score:.4f}，未达阈值")
        return result_list, result_scores
