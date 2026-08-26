import os
import sys


def _get_proxy_url():
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    try:
        import config as cfg
        proxy_url = cfg.config.get("proxy") if isinstance(cfg.config, dict) else None
    except Exception:
        return None
    else:
        if proxy_url:
            os.environ.setdefault("HTTPS_PROXY", proxy_url)
            os.environ.setdefault("HTTP_PROXY", proxy_url)
            return proxy_url
    return None


def get_new_session(**kwargs):
    try:
        # 优先使用httpx，在httpx无法使用的环境下使用requests
        import httpx

        proxy_url = kwargs.pop("proxy", None) or _get_proxy_url()
        transport_kwargs = {"retries": 10}
        if proxy_url:
            transport_kwargs["proxy"] = proxy_url
        http_client = httpx.Client(timeout=30, transport=httpx.HTTPTransport(**transport_kwargs),
                                   follow_redirects=True,
                                   **kwargs)
        # 当openssl版本小于1.0.2的时候直接进行一个空请求让httpx报错
        import tools

        if tools.get_openssl_version() < 102:
            httpx.get()
    except (TypeError, ModuleNotFoundError) as e:
        import requests
        from requests.adapters import HTTPAdapter

        class _TimeoutSession(requests.Session):
            def request(self, method, url, **kwargs):
                kwargs.setdefault('timeout', 30)
                return super().request(method, url, **kwargs)

        http_client = _TimeoutSession()
        http_client.mount('http://', HTTPAdapter(max_retries=10))
        http_client.mount('https://', HTTPAdapter(max_retries=10))
    return http_client


def is_module_imported(module_name):
    return module_name in sys.modules


def get_new_session_use_proxy(http_proxy: str):
    if is_module_imported("httpx"):
        # httpx >= 0.26 使用 proxy（单数）参数
        return get_new_session(proxy=f'http://{http_proxy}')
    else:
        session = get_new_session()
        session.proxies = {
            "http": f'http://{http_proxy}',
            "https": f'http://{http_proxy}'
        }
        return session


class _LazyClient:
    def __init__(self, factory):
        self._factory = factory
        self._client = None

    def _get(self):
        if self._client is None:
            self._client = self._factory()
        return self._client

    def __getattr__(self, name):
        return getattr(self._get(), name)


http = _LazyClient(get_new_session)