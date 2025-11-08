"""Утилиты для обхода защиты Yellowpages и парсинга результатов поиска.

В модуле реализован клиент, который сначала разогревает сайт в браузере
Playwright, собирает актуальные cookies и заголовки, а затем выполняет
HTTP-запросы через requests. При получении ответа 403 происходит автоматический
фолбэк – страница загружается повторно в браузере, данные обновляются, и запрос
повторяется без падений скрипта.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlencode, quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from requests import Request, RequestException, Response

# ANSI-коды для наглядного логирования.
RESET = "\033[0m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"

# Небольшой список реальных user-agent'ов.
BASE_URL = "https://www.yellowpages.com/"


USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]


def random_sleep(min_seconds: float, max_seconds: float) -> None:
    """Случайная пауза между действиями."""
    time.sleep(random.uniform(min_seconds, max_seconds))


def build_headers(user_agent: str, referer: str) -> Dict[str, str]:
    """Базовый набор заголовков, близкий к настоящему браузеру."""
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Referer": referer,
    }


@dataclass
class WarmUpResult:
    cookies: List[Dict[str, str]]
    headers: Dict[str, str]
    html: Optional[str]


def warm_up_yellowpages(target_url: Optional[str] = None, *, fetch_content: bool = False) -> WarmUpResult:
    """Открывает сайт в Playwright, собирает cookies и заголовки.

    Параметры
    ---------
    target_url: URL страницы, для которой нужно подготовить cookies.
    fetch_content: Если True – вернуть также HTML содержимое `target_url`.
    """

    print(f"{YELLOW}[WARM-UP] Запускаем браузер для сбора cookies…{RESET}")

    user_agent = random.choice(USER_AGENTS)
    html: Optional[str] = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-gpu-sandbox",
                "--no-zygote",
                "--single-process",
                "--disable-web-security",
                "--disable-features=VizDisplayCompositor",
                "--disable-software-rasterizer",
            ],
        )

        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": random.randint(1280, 1920), "height": random.randint(720, 1080)},
            locale="en-US",
        )

        page = context.new_page()

        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=35_000)
            random_sleep(2, 4)
            page.keyboard.press("End")
            random_sleep(1, 2)
            page.keyboard.press("Home")
            random_sleep(1, 2)
            page.click("header", position={"x": 10, "y": 10}, timeout=3_000)
            random_sleep(1, 2)

            if target_url:
                page.goto(target_url, wait_until="domcontentloaded", timeout=35_000)
                random_sleep(1.5, 3.0)
                page.mouse.wheel(0, random.randint(400, 900))
                random_sleep(1, 2)
                if fetch_content:
                    html = page.content()
        except PlaywrightTimeoutError as exc:
            print(f"{RED}[WARM-UP] Не удалось загрузить страницу: {exc}{RESET}")
        finally:
            cookies = context.cookies()
            # navigator.userAgent надёжнее – гарантирует актуальное значение.
            user_agent = page.evaluate("navigator.userAgent")
            context.close()
            browser.close()

    referer = BASE_URL
    if target_url:
        parsed = urlparse(target_url)
        if parsed.scheme and parsed.netloc:
            referer = f"{parsed.scheme}://{parsed.netloc}/"

    headers = build_headers(user_agent, referer=referer)
    cookie_str = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)
    headers["Cookie"] = cookie_str

    print(f"{YELLOW}[WARM-UP] Получено {len(cookies)} cookies{RESET}")

    return WarmUpResult(cookies=cookies, headers=headers, html=html)


class YellowPagesClient:
    """HTTP-клиент с автоматическим обновлением cookies через Playwright."""

    def __init__(self, *, timeout: int = 30) -> None:
        self.session = requests.Session()
        self.timeout = timeout
        self._cookies: Dict[str, str] = {}
        self._headers: Dict[str, str] = {}
        self.total_requests = 0
        self.total_fallbacks = 0

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------
    def warm_up(self, target_url: Optional[str] = None, *, fetch_content: bool = False) -> WarmUpResult:
        result = warm_up_yellowpages(target_url, fetch_content=fetch_content)
        self._apply_session_state(result.cookies, result.headers)
        return result

    def get(self, url: str, *, params: Optional[Dict[str, str]] = None, allow_fallback: bool = True) -> str:
        """Выполняет GET-запрос, при 403 повторяет его через Playwright."""

        request = Request("GET", url, params=params)
        prepared = self.session.prepare_request(request)

        try:
            response = self.session.send(prepared, timeout=self.timeout)
            self.total_requests += 1
        except RequestException as exc:
            print(f"{RED}[HTTP] Ошибка запроса: {exc}{RESET}")
            if allow_fallback:
                return self._fallback_fetch(prepared.url)
            raise

        if response.status_code == 403 and allow_fallback:
            print(f"{RED}[HTTP] Получен 403. Пытаемся обновить сессию через браузер…{RESET}")
            return self._fallback_fetch(prepared.url)

        response.raise_for_status()
        self._update_cookies_from_response(response)
        return response.text

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------
    def _apply_session_state(self, cookies: Iterable[Dict[str, str]], headers: Dict[str, str]) -> None:
        self.session.cookies.clear()
        self.session.headers.clear()
        self.session.headers.update(headers)

        self._cookies = {}
        for cookie in cookies:
            name, value = cookie["name"], cookie["value"]
            self._cookies[name] = value
            self.session.cookies.set(name, value)

        self._headers = dict(headers)

    def _update_cookies_from_response(self, response: Response) -> None:
        for cookie in response.cookies:
            self._cookies[cookie.name] = cookie.value

    def _fallback_fetch(self, url: str) -> str:
        self.total_fallbacks += 1
        result = self.warm_up(url, fetch_content=True)
        if result.html:
            print(f"{CYAN}[FALLBACK] Используем HTML, полученный из браузера.{RESET}")
            return result.html

        # Если браузер не вернул HTML (например, из-за таймаута), повторим HTTP запрос.
        request = Request("GET", url)
        prepared = self.session.prepare_request(request)
        response = self.session.send(prepared, timeout=self.timeout)
        response.raise_for_status()
        self._update_cookies_from_response(response)
        return response.text


# ----------------------------------------------------------------------
# Парсинг результатов поиска
# ----------------------------------------------------------------------

def build_search_url(search_terms: str, location: str) -> str:
    params = {
        "search_terms": search_terms,
        "geo_location_terms": location,
    }
    return f"{BASE_URL}search?{urlencode(params, quote_via=quote_plus)}"


def parse_search_results(html: str) -> List[Dict[str, Optional[str]]]:
    """Извлекает основные данные по компаниям со страницы поиска."""
    soup = BeautifulSoup(html, "html.parser")
    cards: List[Dict[str, Optional[str]]] = []

    for result in soup.select(".result" ):
        name_tag = result.select_one("a.business-name span")
        phone_tag = result.select_one(".phones")
        address_tag = result.select_one(".street-address")
        locality_tag = result.select_one(".locality")

        cards.append(
            {
                "name": name_tag.get_text(strip=True) if name_tag else None,
                "phone": phone_tag.get_text(strip=True) if phone_tag else None,
                "address": address_tag.get_text(strip=True) if address_tag else None,
                "locality": locality_tag.get_text(strip=True) if locality_tag else None,
            }
        )

    return cards


def extract_total_pages(html: str) -> int:
    """Определяет количество страниц в пагинации."""
    soup = BeautifulSoup(html, "html.parser")
    last_page = 1
    for link in soup.select(".pagination li a"):
        try:
            number = int(link.get_text(strip=True))
            last_page = max(last_page, number)
        except ValueError:
            continue
    return last_page


def demo(search_terms: str, location: str, *, max_pages: int = 1) -> List[Dict[str, Optional[str]]]:
    """Пример использования клиента: возвращает первые `max_pages` страниц."""
    client = YellowPagesClient()
    collected: List[Dict[str, Optional[str]]] = []

    url = build_search_url(search_terms, location)
    client.warm_up(url)

    page = 1
    while page <= max_pages:
        paged_url = f"{url}&page={page}" if page > 1 else url
        html = client.get(paged_url)
        if page == 1:
            total_pages = extract_total_pages(html)
            print(f"{GREEN}[INFO] Доступно страниц: {total_pages}{RESET}")
        cards = parse_search_results(html)
        print(f"{GREEN}[INFO] Страница {page}: найдено {len(cards)} карточек{RESET}")
        collected.extend(cards)
        page += 1

    return collected


if __name__ == "__main__":  # pragma: no cover - пример запуска
    # Пример: python yellowpages_scraper.py "chicken" "Los Angeles, CA" 2
    import argparse

    parser = argparse.ArgumentParser(description="YellowPages scraper с защитой от 403")
    parser.add_argument("search_terms", help="Что ищем")
    parser.add_argument("location", help="Город/штат")
    parser.add_argument("--pages", type=int, default=1, help="Сколько страниц собрать")
    args = parser.parse_args()

    demo(args.search_terms, args.location, max_pages=args.pages)
