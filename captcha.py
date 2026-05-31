import os
import time

import httpx

from loghelper import log

CAPSOLVER_API_URL = "https://api.capsolver.com"
TWOCAPTCHA_API_URL = "https://api.2captcha.com"
POLL_INTERVAL = 2
MAX_POLL_TIME = 120


def _get_provider() -> str:
    """获取验证码提供商配置，环境变量 CAPTCHA_PROVIDER: capsolver / 2captcha / 其他(跳过)"""
    return os.getenv("CAPTCHA_PROVIDER", "").strip().lower()


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
                log.info("验证码已解决")
                return {
                    "challenge": solution.get("challenge", challenge),
                    "validate": solution["validate"]
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
                log.info("验证码已解决")
                return {
                    "challenge": solution.get("challenge", challenge),
                    "validate": solution["validate"]
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
    provider = _get_provider()
    if provider == "capsolver":
        return _solve_via_capsolver(gt, challenge, "https://act.mihoyo.com/")
    elif provider == "2captcha":
        return _solve_via_2captcha(gt, challenge, "https://act.mihoyo.com/")
    return None


def bbs_captcha(gt: str, challenge: str) -> dict:
    """解决米游社社区操作的 GeeTest 验证码"""
    provider = _get_provider()
    if provider == "capsolver":
        return _solve_via_capsolver(gt, challenge, "https://www.miyoushe.com/")
    elif provider == "2captcha":
        return _solve_via_2captcha(gt, challenge, "https://www.miyoushe.com/")
    return None