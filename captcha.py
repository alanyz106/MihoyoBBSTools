import json
import os
import time
from datetime import datetime

import httpx

from loghelper import log

# CV 破解模块（可选，需要 geetest_cv 包 + 模型文件 + 依赖）
try:
    import geetest_cv
    _GEETEST_CV_IMPORTABLE = True
except ImportError:
    _GEETEST_CV_IMPORTABLE = False

# CV 破解失败计数（本次运行内累计，仅社区签到 bbs 场景使用），超过阈值后切打码服务避免无限重试
# 阈值 = CV 最多尝试次数；当前 4 → CV 至多试 4 次，第 5 次验证码触发才走 2captcha/capsolver
_cv_fail_count = 0
_CV_MAX_FAIL = 4

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


def _cv_enabled() -> bool:
    """CV 破解是否启用：环境变量 GEETEST_CV_ENABLE=1 且依赖和模型可用"""
    if os.getenv("GEETEST_CV_ENABLE", "").strip().lower() not in ("1", "true", "yes"):
        return False
    if not _GEETEST_CV_IMPORTABLE:
        return False
    return geetest_cv.is_available()


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


# 极验 get.php 返回的 data.s 字段对应的具体验证形式
GEETEST_S_MAP = {
    "slide": "滑块",
    "click": "文字点选",
    "ai": "智能模式",
    "voice": "语音",
    "beeline": "轨迹",
    "seccode": "无感通过",
}


def get_geetest_session_detail(gt: str, challenge: str) -> dict:
    """
    通过极验 get.php 接口查询本次 challenge 会话的详细数据

    ⚠️ 警告：此函数会推进 challenge 会话状态，**绝不能在打码前调用**！
    2026-08-06 曾在 record_captcha_type（打码前）调用本函数，导致 2captcha
    拿同一 challenge 打码时被极验拒绝（ERROR_CAPTCHA_UNSOLVABLE），米游社社区
    签到全部失败。现已从打码流程移除，保留函数仅作参考/调试用。
    实测 is_next=true 模式下返回的 data.s 是会话 hash 而非类型标识，拿不到
    slide/click 这种具体形式。具体验证形式只能浏览器抓包确认。

    :param gt: 极验 gt 参数
    :param challenge: 本次会话的 challenge
    :return: data dict（含 s / pic / tip 等），失败时返回 {}
    """
    try:
        client = httpx.Client(timeout=15)
        resp = client.get(
            "https://api.geetest.com/get.php",
            params={
                "is_next": "true",
                "gt": gt,
                "challenge": challenge,
                "lang": "zh-cn",
                "pt": "0",
                "client_type": "web",
                "w": "",
            },
            headers={"User-Agent": "Mozilla/5.0"},
        )
        text = resp.text.strip()
        # 接口返回 JSONP 格式: ({"status": "success", "data": {...}})
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        data = json.loads(text)
        if data.get("status") == "success":
            return data.get("data", {})
        log.warning(f"get.php 返回异常: {data}")
        return {}
    except Exception as e:
        log.warning(f"获取验证码会话详情失败: {e}")
        return {}
    finally:
        client.close()


def record_captcha_type(gt: str, scene: str, challenge: str = "") -> str:
    """
    查询并记录验证码类型，追加到 log/captcha_record.log，同时写入运行日志

    ⚠️ 不要在此函数（打码前）调用 get.php！get.php 会推进 challenge 会话状态，
    导致 2captcha 拿同一 challenge 打码时被极验拒绝（ERROR_CAPTCHA_UNSOLVABLE），
    2026-08-06 的回归 bug 就是这个原因。只能调只读的 gettype.php。
    具体验证形式（slide/click）需浏览器抓包确认，代码层面拿不到。

    :param gt: 极验 gt 参数
    :param scene: 场景标识，如 game/bbs
    :param challenge: 保留参数（兼容调用方签名），不再用于查询，避免污染会话
    :return: 类型名称（slide/click 等）
    """
    detail = get_geetest_detail(gt)
    captcha_type = detail.get("type", "unknown")
    type_name = CAPTCHA_TYPE_MAP.get(captcha_type, captcha_type)

    # 推送摘要：一行展示呈现模式（gettype.php 只读，不影响 challenge）
    log.info(f"验证码类型: {scene}场景 → {type_name}({captcha_type})")

    # 详细数据降级到 debug，避免推送消息过长（loghelper 默认 INFO 级别，debug 不进推送）
    script_names = {k: v for k, v in detail.items() if isinstance(v, str) and ("." in v)}
    if script_names:
        log.debug(f"验证码接口脚本: {json.dumps(script_names, ensure_ascii=False)}")

    # 完整数据写入本地 log/captcha_record.log（*.log 已被 gitignore，不进仓库）
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CAPTCHA_RECORD_FILE, "a", encoding="utf-8") as f:
            f.write(f"{now} 场景={scene} gt={gt} 类型={captcha_type} ({type_name})\n")
            f.write(f"    gettype 数据: {json.dumps(detail, ensure_ascii=False)}\n")
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


def _remote_api_url() -> str:
    """
    自建验证码推理服务器地址（GEETEST_API_URL），设置后优先于本地 CV。

    支持两种写法，会自动规范成破解接口的 base：
      - 规范前缀：https://host/geetest/v3/nine_pic   （推荐，2026-08-31 起）
      - 旧前缀  ：https://host/captcha              （已废弃，nginx 侧 308 跳转到规范前缀）
    末尾斜杠可省略；误填成 .../captcha/ 也能自动收敛到 /geetest/v3/nine_pic。
    """
    raw = os.getenv("GEETEST_API_URL", "").strip().rstrip("/")
    if not raw:
        return ""
    # 旧前缀自动升级到规范前缀，避免 nginx 308 重定向（httpx 默认不跟随 308）
    for old in ("/captcha", "/geetest/v3/nine_pic"):
        if raw == old or raw.endswith(old):
            return raw[: -len(old)] + "/geetest/v3/nine_pic"
    return raw


def _remote_api_key() -> str:
    """自建验证码推理服务器 API Key（GEETEST_API_KEY），作为 X-API-Key 请求头"""
    return os.getenv("GEETEST_API_KEY", "").strip()


def _solve_via_remote_api(gt: str, challenge: str):
    """
    通过自建推理服务器解决极验验证码（geetest-v3-nine-pic-crack 部署的服务端）

    服务器端自行完成 gettype → get_c_s → ajax → get_pic → 模型识别 → verify 全流程，
    本机无需安装 CV 依赖、无需下载模型。成功返回极验完整响应 JSON。

    ⚠️ 与本地 CV 一样会推进 challenge 会话状态，失败后该 challenge 不能再给
    2captcha 用，必须返回 None 让外层重新触发拿新 challenge。

    :param gt: 极验 gt 参数
    :param challenge: 本次会话的 challenge
    :return: {"challenge": str, "validate": str} 或 None
    """
    base = _remote_api_url()
    headers = {}
    api_key = _remote_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    # follow_redirects=True：服务器侧前缀迁移（如 /captcha → /geetest/v3/nine_pic）会返回
    # 301/308，httpx 默认不跟随，会把重定向页当响应，导致"服务挂了"的假象。
    client = httpx.Client(timeout=120, headers=headers, follow_redirects=True)
    try:
        log.info(f"正在调用自建验证码 API... ({base}/crack_it)")
        resp = client.get(f"{base}/crack_it", params={"gt": gt, "challenge": challenge})
        if resp.status_code != 200:
            # 3xx：重定向链没走通，多半是路径前缀不对，提示实际打到的地址
            if 300 <= resp.status_code < 400:
                location = resp.headers.get("location", "(无 Location 头)")
                log.warning(
                    f"自建验证码 API 返回重定向 HTTP {resp.status_code}，未被自动处理；"
                    f"目标={location}。请检查 GEETEST_API_URL 是否指向规范前缀 "
                    f"/geetest/v3/nine_pic（当前 base={base}）"
                )
            # 401/403：API Key 有问题，明确提示，避免被误判为服务不可用
            elif resp.status_code in (401, 403):
                log.warning(
                    f"自建验证码 API 鉴权失败 HTTP {resp.status_code}："
                    f"请检查 secret GEETEST_API_KEY 是否有效（响应: {resp.text[:120]}）"
                )
            else:
                log.warning(f"自建验证码 API 返回异常: HTTP {resp.status_code}, {resp.text[:200]}")
            return None
        data = resp.json()
        result_data = data.get("data", {})
        if result_data.get("result") == "success":
            validate = result_data.get("validate", "")
            log.info("自建验证码 API 破解成功，未消耗打码额度")
            return {"challenge": challenge, "validate": validate}
        log.warning(f"自建验证码 API 破解失败: {result_data}")
        return None
    except Exception as e:
        log.warning(f"自建验证码 API 请求异常: {e}")
        return None
    finally:
        client.close()


def bbs_captcha(gt: str, challenge: str) -> dict:
    """解决米游社社区操作的 GeeTest 验证码（自建API/本地CV 优先，打码服务兜底）"""
    global _cv_fail_count
    # 自建推理服务器优先：模型跑在服务器上，本机无需模型和 CV 依赖
    # （同样走独立极验流程，失败会污染 challenge，返回 None 让外层重新触发）
    if _remote_api_url():
        if _cv_fail_count >= _CV_MAX_FAIL:
            log.warning("自建验证码 API 连续失败，改用打码服务")
        else:
            result = _solve_via_remote_api(gt, challenge)
            if result:
                return result
            _cv_fail_count += 1
            log.warning(f"自建验证码 API 破解失败（第{_cv_fail_count}次），等待重新触发验证码")
            return None
    # 本地 CV 破解优先（独立流程，会推进 challenge，不走 record_captcha_type）
    elif _cv_enabled() and _cv_fail_count < _CV_MAX_FAIL:
        result = geetest_cv.solve(gt, challenge)
        if result:
            return result
        _cv_fail_count += 1
        log.warning(f"CV 破解失败（第{_cv_fail_count}次），等待重新触发验证码")
        return None  # challenge 已污染，外层重新触发拿新 challenge
    # 打码服务兜底（新 challenge，走 record_captcha_type 记录类型）
    record_captcha_type(gt, "bbs", challenge)
    provider = _get_provider()
    if provider == "capsolver":
        return _solve_via_capsolver(gt, challenge, "https://www.miyoushe.com/")
    elif provider == "2captcha":
        return _solve_via_2captcha(gt, challenge, "https://www.miyoushe.com/")
    return None