# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""Web 访问与搜索工具。"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, List
from urllib.parse import urlencode

import httpx


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 lumin-chat/1.0"
)


class _HTMLTextExtractor(HTMLParser):
    """从 HTML 中提取可读文本。"""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in {"p", "div", "section", "article", "br", "li", "h1", "h2", "h3"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in {"p", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def get_text(self) -> str:
        """返回压缩后的文本。"""

        text = html.unescape("".join(self._chunks))
        text = re.sub(r"\r", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()


@dataclass
class WebToolClient:
    """封装网页抓取与搜索逻辑。"""

    timeout_seconds: int = 60

    def fetch_page(self, url: str, max_chars: int = 120000) -> Dict[str, object]:
        """抓取网页并提取标题与正文摘要。"""

        with self._client() as client:
            response = client.get(url, follow_redirects=True)
            response.raise_for_status()
            text = response.text
        title_match = re.search(r"<title[^>]*>(?P<title>.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        title = html.unescape(title_match.group("title").strip()) if title_match else ""
        extractor = _HTMLTextExtractor()
        extractor.feed(text)
        extracted = extractor.get_text()
        excerpt = extracted[:max_chars]
        return {
            "url": url,
            "final_url": str(response.url),
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "encoding": response.encoding,
            "title": title,
            "text": excerpt,
            "truncated": len(extracted) > max_chars,
        }

    def search(self, query: str, limit: int = 5) -> Dict[str, object]:
        """使用 DuckDuckGo HTML 结果页进行公开网页搜索。"""

        search_url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
        with self._client() as client:
            response = client.get(search_url, follow_redirects=True)
            response.raise_for_status()
            body = response.text
        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        snippet_pattern = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.IGNORECASE | re.DOTALL)
        results: List[Dict[str, str]] = []
        snippets = [self._clean_html(match.group("snippet")) for match in snippet_pattern.finditer(body)]
        for index, match in enumerate(pattern.finditer(body)):
            if len(results) >= limit:
                break
            results.append(
                {
                    "title": self._clean_html(match.group("title")),
                    "url": html.unescape(match.group("href")),
                    "snippet": snippets[index] if index < len(snippets) else "",
                }
            )
        return {
            "query": query,
            "engine": "duckduckgo-html",
            "count": len(results),
            "results": results,
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout_seconds,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    @staticmethod
    def _clean_html(raw_html: str) -> str:
        """移除 HTML 标签并解码实体。"""

        text = re.sub(r"<[^>]+>", " ", raw_html)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()


def format_payload(payload: Dict[str, object]) -> str:
    """将 Web 工具结果编码为统一 JSON 字符串。"""

    return json.dumps(payload, ensure_ascii=False, indent=2)
