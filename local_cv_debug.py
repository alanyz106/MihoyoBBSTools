"""
本地触发一次米游社验证码，测试 CV 模块并保存调试图片。

用法：
    set GEETEST_CV_ENABLE=1
    set GEETEST_CV_DEBUG=1
    .venv\\Scripts\\python local_cv_debug.py

注意：
- 不会完成签到，只调用 createVerification 拿 challenge 并走 CV 流程。
- 需要本地 config/config.yaml 已配置好 cookie/stoken。
- CV 失败后不会继续走 2captcha，避免消耗额度。
"""
import os
import sys

# 确保本地模型被找到
os.environ.setdefault("GEETEST_CV_MODEL_DIR", os.path.join(os.path.dirname(os.path.realpath(__file__)), "models"))
os.environ.setdefault("GEETEST_CV_ENABLE", "1")
os.environ.setdefault("GEETEST_CV_DEBUG", "1")

import config
import mihoyobbs
from loghelper import log


def main():
    config.load_config()
    if not config.config.get("enable"):
        log.error("Config 未启用")
        return 1

    log.info("构造 Mihoyobbs 实例并获取 challenge...")
    bbs = mihoyobbs.Mihoyobbs()
    log.info("调用 get_pass_challenge 触发 CV 流程...")
    result = bbs.get_pass_challenge()
    log.info(f"get_pass_challenge 返回: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
