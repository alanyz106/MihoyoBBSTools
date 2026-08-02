import json
import os
import time
from datetime import datetime

import httpx

from loghelper import log

CAPSOLVER_API_URL = "https://api.capsolver.com"
TWOCAPTCHA_API_URL = "https://api.2captcha.com"
POLL_INTERVAL = 2
MAX_POLL_TIME = 120

CAPTCHA_TYPE_MAP = {
    "slide": "滑块验证",
    "click": "点选验证",
    "fullpage": "弹窗验证",
    "voice": "语音验证",
    "beeline": "轨迹验证",
    "unknown": "未知",
}

LOG_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "log")
CAPTCHA_RECORD_FILE = os.path.join(LOG_DIR, "captcha_record.log")


def _get_provider() -> str:
    """获取验证码提供商配置，环境变量 CAPTCHA_PROVIDER: capsolver / 2captcha / 其他(跳过)"""
    return os.getenv("CAPTCHA_PROVIDER", "").strip().lower()


def get_geetest_detail(gt: str) -> dict:
    """
    通过极验 gettype.php 接口查询验证码的完整原始数据

    :param gt: 极验的 gt 参数
    :return: data dict（含 type / slide / click / fullpage JS 路径等），失败时返回 {}
    """
    try:
        client = httpx.Client(timeout=15)
        resp = client.get(f"https://api.geetest.com/gettype.php?gt={gt}",
                          headers={"User-Agent": "Mozilla/5.0"})
        text = resp.text.strip()
        # 接口返回 JSONP 格式: ({"status": "success", "data": {...}})
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        data = json.loads(text)
        if data.get("status") == "success":
            return data.get("data", {})
        log.warning(f"gettype 返回异常: {data}")
        return {}
    except Exception as e:
        log.warning(f"获取验证码类型失败: {e}")
        return {}
    finally:
        client.close()


def get_geetest_type(gt: str) -> str:
    """
    通过极验 gettype.php 接口查询验证码类型

    :param gt: 极验的 gt 参数
    :return: slide / click / fullpage 等类型，失败时返回 unknown
    """
    return (get_geetest_detail(gt) or {}).get("type", "unknown")


def record_captcha_type(gt: str, scene: str) -> str:
    """
    查询并记录验证码类型，追加到 log/captcha_record.log，同时写入运行日志

    :param gt: 极验 gt 参数
    :param scene: 场景标识，如 game/bbs
    :return: 类型名称（slide/click 等）
    """
    detail = get_geetest_detail(gt)
    captcha_type = detail.get("type", "unknown")
    type_name = CAPTCHA_TYPE_MAP.get(captcha_type, captcha_type)
    script_names = {k: v for k, v in detail.items() if isinstance(v, str) and ("." in v)}
    log.info(f"验证码类型记录: 场景={scene} 类型={type_name} ({captcha_type})")
    if script_names:
        log.info(f"验证码接口脚本: {json.dumps(script_names, ensure_ascii=False)}")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CAPTCHA_RECORD_FILE, "a", encoding="utf-8") as f:
            f.write(f"{now} 场景={scene} gt={gt} 类型={captcha_type} ({type_name})\n")
            f.write(f"    详细数据: {json.dumps(detail, ensure_ascii=False)}\n")
    except Exception as e:
        log.warning(f"写入验证码类型记录失败: {e}")
    return captcha_type


def _solve_via_capsolver(gt: str, challenge: str, page_url: str):
    """
    通过 CapSolver API 解决 GeeTest 验证码

    :return: {"challenge": str, "validate": str} 或 None
    """
    api_key = os.getenv("CAPTCHA_API_KEY", "").strip()
    if not api_key:
        log.warning("未设置 CAPTCHA_API_KEY 环境变量，跳过验证码处理")
        return None

    client = httpx.Client(timeout=30)

    try:
        log.info("正在向 CapSolver 提交验证码...")
        task_resp = client.post(f"{CAPSOLVER_API_URL}/createTask", json={
            "clientKey": api_key,
            "task": {
                "type": "GeetestTaskProxyless",
                "websiteURL": page_url,
                "gt": gt,
                "challenge": challenge
            }
        })
        task_data = task_resp.json()
        if task_data.get("errorId") != 0:
            log.error(f"CapSolver 创建任务失败: {task_data.get('errorDescription', '未知错误')}")
            return None

        task_id = task_data["taskId"]
        log.info(f"CapSolver 任务已提交，ID: {task_id}，正在等待结果...")

        elapsed = 0
        while elapsed < MAX_POLL_TIME:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            poll_resp = client.post(f"{CAPSOLVER_API_URL}/getTaskResult", json={
                "clientKey": api_key,
                "taskId": task_id
            })
            poll_data = poll_resp.json()

            if poll_data.get("status") == "ready":
                solution = poll_data["solution"]
                keys = sorted(solution.keys())
                has_v3 = "validate" in solution or "seccode" in solution
                api_version = "v3" if has_v3 else ("v4" if any(
                    k in solution for k in ("lot_number", "pass_token", "captcha_output")
                ) else "未知")
                log.info(f"验证码已解决: 极验接口={api_version} solution字段={keys}")
                validate = solution.get("validate", "") or solution.get("pass_token", "") or ""
                return {
                    "challenge": solution.get("challenge", challenge),
                    "validate": validate
                }
            elif poll_data.get("status") in ("processing", "idle"):
                continue
            else:
                log.error(f"CapSolver 任务异常: {poll_data}")
                return None

        log.warning("CapSolver 验证码解决超时")
        return None

    except Exception as e:
        log.error(f"CapSolver 请求异常: {e}")
        return None
    finally:
        client.close()


def _solve_via_2captcha(gt: str, challenge: str, page_url: str):
    """
    通过 2captcha API 解决 GeeTest 验证码

    :return: {"challenge": str, "validate": str} 或 None
    """
    api_key = os.getenv("CAPTCHA_API_KEY", "").strip()
    if not api_key:
        log.warning("未设置 CAPTCHA_API_KEY 环境变量，跳过验证码处理")
        return None

    client = httpx.Client(timeout=30)

    try:
        log.info("正在向 2captcha 提交验证码...")
        task_resp = client.post(f"{TWOCAPTCHA_API_URL}/createTask", json={
            "clientKey": api_key,
            "task": {
                "type": "GeeTestTaskProxyless",
                "websiteURL": page_url,
                "gt": gt,
                "challenge": challenge
            }
        })
        task_data = task_resp.json()
        if task_data.get("errorId") != 0:
            log.error(f"2captcha 创建任务失败: {task_data.get('errorDescription', '未知错误')}")
            return None

        task_id = task_data["taskId"]
        log.info(f"2captcha 任务已提交，ID: {task_id}，正在等待结果...")

        elapsed = 0
        while elapsed < MAX_POLL_TIME:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            poll_resp = client.post(f"{TWOCAPTCHA_API_URL}/getTaskResult", json={
                "clientKey": api_key,
                "taskId": task_id
            })
            poll_data = poll_resp.json()

            if poll_data.get("status") == "ready":
                solution = poll_data["solution"]
                keys = sorted(solution.keys())
                has_v3 = "validate" in solution or "seccode" in solution
                api_version = "v3" if has_v3 else ("v4" if any(
                    k in solution for k in ("lot_number", "pass_token", "captcha_output")
                ) else "未知")
                log.info(f"验证码已解决: 极验接口={api_version} solution字段={keys}")
                validate = solution.get("validate", "") or solution.get("pass_token", "") or ""
                return {
                    "challenge": solution.get("challenge", challenge),
                    "validate": validate
                }
            elif poll_data.get("status") == "processing":
                continue
            else:
                log.error(f"2captcha 任务异常: {poll_data}")
                return None

        log.warning("2captcha 验证码解决超时")
        return None

    except Exception as e:
        log.error(f"2captcha 请求异常: {e}")
        return None
    finally:
        client.close()


def game_captcha(gt: str, challenge: str) -> dict:
    """解决游戏签到的 GeeTest 验证码"""
    record_captcha_type(gt, "game")
    provider = _get_provider()
    if provider == "capsolver":
        return _solve_via_capsolver(gt, challenge, "https://act.mihoyo.com/")
    elif provider == "2captcha":
        return _solve_via_2captcha(gt, challenge, "https://act.mihoyo.com/")
    return None


def bbs_captcha(gt: str, challenge: str) -> dict:
    """解决米游社社区操作的 GeeTest 验证码"""
    record_captcha_type(gt, "bbs")
    provider = _get_provider()
    if provider == "capsolver":
        return _solve_via_capsolver(gt, challenge, "https://www.miyoushe.com/")
    elif provider == "2captcha":
        return _solve_via_2captcha(gt, challenge, "https://www.miyoushe.com/")
    return None