"""
极验 v3 文字点选验证码 CV 破解模块

改造自 ravizhan/geetest-v3-click-crack (AGPL-3.0)
适配 fullpage 9.2.0-guwyxh 版本，成功率约 80%。

⚠️ 本模块会推进 challenge 会话状态，调用后该 challenge 不能再给 2captcha 用。
   集成时 CV 失败要返回 None，让外层重新触发拿新 challenge 给 2captcha。

依赖：onnxruntime, opencv-python, numpy, cryptography, httpx[http2]
模型：yolov8s.onnx (43MB) + siamese.onnx (17MB)，放 models/ 目录或用
      环境变量 GEETEST_CV_MODEL_DIR 指定。
"""
import json
import os
import time
from datetime import datetime

from loghelper import log

# 九宫格图片验证码识别（可选）
try:
    import geetest_ninepic
    _GEETEST_NINEPIC_IMPORTABLE = True
except ImportError:
    _GEETEST_NINEPIC_IMPORTABLE = False

_model_instance = None

_DEBUG_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "log", "cv_debug")
_SAVE_DEBUG = os.getenv("GEETEST_CV_DEBUG", "").strip().lower() in ("1", "true", "yes")


def _debug_path(prefix: str, retry: int, ext: str = "png") -> str:
    os.makedirs(_DEBUG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%m%d_%H%M%S_%f")[:-3]
    return os.path.join(_DEBUG_DIR, f"{prefix}_{ts}_r{retry}.{ext}")


def _save_bytes(path: str, data: bytes):
    try:
        with open(path, "wb") as f:
            f.write(data)
    except Exception as e:
        log.debug(f"保存调试图片失败: {path}, {e}")


def _get_model():
    """模型单例（onnx 加载较重，复用避免重复初始化）"""
    global _model_instance
    if _model_instance is None:
        from .model import Model
        _model_instance = Model()
    return _model_instance


def is_available() -> bool:
    """检查 CV 破解是否可用（依赖 + 模型文件）"""
    try:
        import onnxruntime  # noqa
        import cv2  # noqa
        import numpy  # noqa
        from cryptography.hazmat.primitives.ciphers import Cipher  # noqa
    except ImportError:
        return False
    model_dir = os.getenv("GEETEST_CV_MODEL_DIR",
                          os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "models"))
    return os.path.exists(os.path.join(model_dir, "yolov8s.onnx")) and \
           os.path.exists(os.path.join(model_dir, "siamese.onnx"))


def solve(gt: str, challenge: str, max_retries: int = 3):
    """
    CV 破解极验 v3 文字点选验证码

    走完整极验前端流程：gettype → get_c_s → ajax → get_pic → detect → siamese → verify

    :param gt: 极验 gt 参数
    :param challenge: 本次会话的 challenge
    :param max_retries: 识别失败最大重试次数（每次重试刷新验证码图片）
    :return: {"challenge": str, "validate": str} 或 None（失败/异常）
    """
    try:
        from .crack import Crack
    except ImportError as e:
        log.warning(f"CV 破解依赖未安装，跳过: {e}")
        return None

    log.info("CV 破解中...")
    try:
        crack = Crack(gt, challenge)
        crack.gettype()
        crack.get_c_s()
        time.sleep(0.5)
        crack.ajax()

        model = _get_model()

        for retry in range(max_retries):
            ttt = time.time()
            pic_content = crack.get_pic(retry)
            pic_got_time = time.time()
            if _SAVE_DEBUG and pic_content:
                _save_bytes(_debug_path("raw", retry, "jpg"), pic_content)

            small_img, big_img = model.detect(pic_content, retry)
            log.debug(f"CV 检测: 小图{len(small_img)}个 大图{len(big_img)}个 (第{retry+1}次)")

            if not small_img or not big_img:
                log.warning(f"CV 文字点选检测结果为空，可能是九宫格图片验证码 (第{retry+1}次)")
                if _GEETEST_NINEPIC_IMPORTABLE and geetest_ninepic.is_available():
                    try:
                        points = geetest_ninepic.predict_points(pic_content)
                        if points:
                            submission = [f"{col}_{row}" for col, row, _ in points]
                            log.info(f"九宫格模型提交: {submission}")
                            # 极验要求验证与获取图片间隔不小于 2 秒，九宫格有时更严格
                            wait_time = 2.3 - (time.time() - pic_got_time)
                            if wait_time > 0:
                                time.sleep(wait_time)
                            result = json.loads(crack.verify(submission))
                            if result.get("data", {}).get("result") == "success":
                                validate = result["data"].get("validate", "")
                                log.info("九宫格 CV 破解成功，未消耗打码额度")
                                return {
                                    "challenge": challenge,
                                    "validate": validate,
                                }
                            log.warning(f"九宫格 CV verify 未通过 (第{retry+1}次): {result.get('data', {})}")
                        else:
                            log.warning(f"九宫格模型未识别到匹配项 (第{retry+1}次)")
                    except Exception as e:
                        log.warning(f"九宫格 CV 识别异常 (第{retry+1}次): {e}")
                else:
                    log.debug("九宫格模型不可用，跳过")
                continue

            result_list, scores = model.siamese(small_img, big_img, retry)
            log.debug(f"CV 匹配结果: {result_list}, 分数: {scores} (第{retry+1}次)")
            if not result_list:
                log.warning(f"CV 匹配失败，刷新重试 (第{retry+1}次)")
                continue

            point_list = []
            for i in result_list:
                left = str(round((i[0] + 30) / 333 * 10000))
                top = str(round((i[1] + 30) / 333 * 10000))
                point_list.append(f"{left}_{top}")

            # 极验要求验证与获取图片间隔不小于 2 秒，否则报 duration short
            wait_time = 2.0 - (time.time() - ttt)
            if wait_time > 0:
                time.sleep(wait_time)

            result = json.loads(crack.verify(point_list))
            if result.get("data", {}).get("result") == "success":
                validate = result["data"].get("validate", "")
                log.info("CV 破解成功，未消耗打码额度")
                return {
                    "challenge": challenge,
                    "validate": validate,
                }
            log.warning(f"CV verify 未通过 (第{retry+1}次): {result.get('data', {})}")

        log.warning(f"CV 破解失败（{max_retries}次重试均未成功），将回退打码服务")
        return None
    except Exception as e:
        log.warning(f"CV 破解异常: {e}")
        return None
