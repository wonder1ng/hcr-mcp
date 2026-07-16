"""키워드 기반 최근 이슈 뉴스 수집 — 회사/산업/직무 트렌드 모두 같은 검색+그룹핑 엔진을 쓴다
(검색어와 LLM 관련성 판단 문구만 바뀜). company_report가 회사 이슈·산업 동향을, fit이 직무
트렌드를 각각 collect_recent_issues/collect_industry_trend/collect_job_trend로 가져다 쓴다.

v1 첫 구현은 HcR/scrapy/news_links_scrapy.py(finance.naver.com 증권뉴스 검색)를 copy-adapt했으나,
실제 테스트에서 두 가지 구조적 문제가 드러나 general 뉴스검색(search.naver.com)으로 교체했다:
1. finance.naver.com은 증권/금융 채널만 색인해 일반 뉴스(대표 교체, 전시회 후원 등)를 놓친다.
2. 두 엔드포인트 모두 `q=%22...%22`(따옴표) 고급검색 문법을 실제로는 무시한다(정확히 일치 검색이
   URL 파라미터로는 불가능함을 실증 확인) — 그래서 동명이인/무관 회사 필터링은 LLM 그룹핑 단계에서
   같이 처리한다(_group_into_issues).

기간 지정도 마찬가지로 ds/de/nso 파라미터 조합으로 과거 특정 구간을 지정하는 게 재현되지 않아
포기했다 — 대신 sort=1(최신순) + 페이지 깊이로 구현한다(page를 깊이 넘길수록 점점 과거로 감을
실증 확인: start=1→2026.06, start=101→2025.03, start=301→2023.06). "기간을 늘린다"는 곧
"페이지를 더 깊이 넘긴다"와 같다.

본문 수집: 검색 결과 링크가 언론사마다 제각각 다른 도메인이라(mtn.co.kr, etnews.com, ...) 범용
스크래핑 품질이 들쭉날쭉했다(일부는 깨끗하지만 일부는 헤드라인 목록/로그인 안내만 잡힘 — 후자는
실제 페이월이 아니라 봇 감지로 보이지만 신뢰도를 확신할 수 없다). 그래서 검색결과에 네이버뉴스
미러(n.news.naver.com) 링크가 있으면 그걸 우선 사용하고(항상 무료, 안정적인 article#dic_area
셀렉터), 없으면 검색결과 자체의 스니펫(.sds-comps-text-type-body1, 항상 확보되고 추가 요청도
불필요)을 그대로 쓴다 — 임의 도메인 풀스크래핑은 하지 않는다.

이슈 클러스터링은 제목 문자열 유사도(SequenceMatcher)로 먼저 시도했으나 언론사마다 표현이 달라
("전관" vs "전관업체") 정확도가 너무 낮았다. 제목+날짜 목록을 LLM에 한 번 보내 그룹핑한다(본문은
그룹핑 후 대표 기사만 필요하므로 저렴함). 검색/크롤링 실패는 다른 콜렉터들과 동일하게 조용한 부분
실패(예외를 던지지 않음). 그룹핑 응답이 깨지면(JSON 파싱 실패 등) 필터링 없이 기사마다 별도
이슈로 안전하게 축소한다 — LLM 호출 자체의 실패(인증·rate limit 등)는 llm_client가 이미
HcrMcpError로 변환해 그대로 위로 전파된다.

ponytail: 검색 결과 페이지의 sds-comps-* 클래스는 해시라 Naver 프론트엔드 개편 시 깨질 수 있다
(data-heatmap-target 속성이 상대적으로 안정적이라 그걸 우선 사용). 셀렉터가 깨지면 이 파일부터
다시 점검할 것.
"""

import asyncio
import json
import random
import re
from calendar import monthrange
from datetime import date, timedelta
from typing import Callable

import httpx
from bs4 import BeautifulSoup
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from hcr_mcp import llm_client, net
from hcr_mcp.company_report.news import event_taxonomy

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

_SEARCH_URL = "https://search.naver.com/search.naver"
_REQUEST_TIMEOUT = 10
_PAGE_SIZE = 10
_ARTICLES_PER_ISSUE = 2
_MIN_ISSUES = 15           # 이 개수 미만이면 탐색 기간을 늘려 재시도 — 도달하면 그 라운드에서
                           # 찾은 이슈를 전부(개수 상한 없이) 반환한다. "15개면 끝"이 아니라
                           # "이 정도는 있어야 그만 찾는다"는 하한선이다. 면접에서 어떤 이슈가
                           # 쓸모 있을지 미리 알 수 없으므로 결과를 임의로 자르지 않는다. (5로는
                           # 6개월 라운드만 보고 거의 항상 조기 종료돼 결과가 얕았음 — 실측)
_SIX_MONTH_BACK_MONTHS = 6    # 첫 라운드(최근 6개월)는 달력 월 기준 정확히 6개월 전 — 롤링이라
                              # "햇수" 규칙(아래) 대상이 아니다.
_EARLY_YEAR_CUTOFF_MONTH = 4  # 4월 1일 컷오프: 그 해 데이터가 아직 적을 시점이라 한 해 더 본다.
_CALENDAR_YEARS_BACK = 2          # 기본: 작년+재작년 전체(올해 몫과 합쳐 "3개년" 도달)
_CALENDAR_YEARS_BACK_EARLY_EXTRA = 1  # 4/1 이전이면 그전해까지 추가("4개년" 도달)
_NOISE_RATIO_THRESHOLD = 0.5  # LLM이 무관 판정해 제외한 기사 비율이 이보다 높으면 "노이즈 많음"
_NOISE_MIN_ARTICLES = 10      # 표본이 이보다 적으면 노이즈 판단을 건너뜀(우연에 좌우되기 쉬움)

_DATE_ABS_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})\.?")
_DATE_REL_RE = re.compile(r"(\d+)\s*(분|시간|일)\s*전")

_RELEVANCE_LINE = {
    "company": (
        '1. "{keyword}" 자체에 대한 기사뿐 아니라 그 계열사·자회사·브랜드·제품 라인에 대한 기사도\n'
        '   관련 기사로 포함하세요. 목록 안에 "{keyword} 브랜드명"처럼 두 이름이 함께 나오는\n'
        "   제목이 있으면, 그 브랜드명만 단독으로 나온 다른 제목들도 같은 회사 기사로 간주하세요.\n"
        '   이름은 비슷하지만 실제로는 무관한 기사(동명이인, 다른 회사, 또는 그 단어가 일반적인\n'
        "   의미로 쓰인 경우 등)만 결과에서 제외하고, 관련 여부가 애매하면 제외하지 말고 포함하세요.\n"
    ),
    "industry": '1. "{keyword}" 산업/업종과 무관한 기사(단어만 우연히 겹치는 경우 등)는 결과에서 제외하세요.\n',
    "job": '1. "{keyword}" 직무와 무관한 기사(단어만 우연히 겹치는 경우 등)는 결과에서 제외하세요.\n',
}

_SUBJECT_LABEL = {"company": "기업명", "industry": "산업/업종 키워드", "job": "직무명"}

def _group_system_prompt(keyword: str, subject_kind: str) -> str:
    relevance_line = _RELEVANCE_LINE[subject_kind].format(keyword=keyword)
    return (
        f'"{keyword}"는 {_SUBJECT_LABEL[subject_kind]}입니다(일반 명사와 우연히 같은 표기여도 이 의미로 판단하세요).\n'
        f'당신은 "{keyword}" 관련 뉴스를 수집하는 스크래퍼입니다.\n'
        "번호가 매겨진 기사 제목·날짜 목록이 주어지면 두 가지를 하세요:\n"
        f"{relevance_line}"
        "2. 남은 기사 중 실질적으로 같은 사건이나 같은 트렌드/이슈를 다루는 기사끼리 그룹으로\n"
        "   묶으세요. 언론사마다 제목 표현이 달라도 같은 사건·트렌드면 같은 그룹입니다. 다른\n"
        "   기사와 묶이지 않는 관련 기사는 그룹 크기 1로 두세요.\n"
        '다른 설명 없이 순수 JSON으로만 응답하세요: {"groups": [[0,2,5], [1], [3,4]]}\n'
        "groups는 인덱스 배열의 배열입니다. 제외한 기사의 인덱스는 어떤 그룹에도 넣지 마세요."
    )


def _parse_date_text(text: str) -> str:
    m = _DATE_ABS_RE.search(text)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m = _DATE_REL_RE.search(text)
    if m:
        n, unit = m.groups()
        delta = timedelta(days=int(n)) if unit == "일" else timedelta(0)
        return (date.today() - delta).isoformat()
    return ""


_TRANSPORT_RETRY_DELAY_RANGE = (1.0, 3.9)  # 초 — HcR/scrapy/news_links_scrapy.py의
                                            # random.uniform(1,2,3.9) 디도스 방지 랜덤 딜레이 패턴


def _date_range_params(start_date: date, end_date: date) -> dict:
    """실증 확인된 뉴스검색 커스텀 기간 필터 조합. pd=3 + nso=so:dd,p:from...to... + qdt=1
    + sort=1(최신순) 조합이 해당 구간으로 정확히 좁혀준다(사용자 확인 레퍼런스 URL 기준)."""
    ds, de = start_date.strftime("%Y.%m.%d"), end_date.strftime("%Y.%m.%d")
    nso = f"so:dd,p:from{start_date.strftime('%Y%m%d')}to{end_date.strftime('%Y%m%d')}"
    return {
        "ssc": "tab.news.all", "sm": "tab_opt", "sort": "1", "photo": "0", "field": "0",
        "pd": "3", "ds": ds, "de": de, "docid": "", "qdt": "1", "related": "0", "mynews": "0",
        "office_type": "0", "office_section_code": "0", "news_office_checked": "",
        "nso": nso, "is_sug_officeid": "0", "office_category": "0", "service_area": "0",
    }


async def _fetch_search_page(
    client: httpx.AsyncClient, keyword: str, start_date: date, end_date: date, start_idx: int
) -> str:
    """HTTP 레벨 실패(전송 실패)는 원본 news_links_scrapy.py의 collect_page_data와 동일하게
    횟수 제한 없이 랜덤 대기 후 계속 재시도한다 — "결과가 나올 때까지" 포기하지 않는다.
    콘텐츠 레벨 신호(파싱 결과 없음)는 이 함수가 아니라 호출자가 원본과 동일하게 재시도 없이
    "진짜 끝"으로 판단한다(전송 실패는 일시적이라 계속 재시도할 가치가 있지만, 정상 응답인데
    결과가 없는 건 그 이상 페이지가 없다는 신뢰할 수 있는 신호이기 때문)."""
    params = {**_date_range_params(start_date, end_date), "query": keyword, "start": str(start_idx)}
    while True:
        try:
            resp = await client.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            net.raise_if_ssl_trust_error(e)
            await asyncio.sleep(random.uniform(*_TRANSPORT_RETRY_DELAY_RANGE))
            continue
        return resp.text


def _label_text(el) -> str:
    """제목/언론사명 라벨 텍스트만 추출한다. Naver가 접근성용 숨김 텍스트("새 창 열림" 등)를
    형제 span(해시 클래스라 이름으로 걸러낼 수 없음)으로 끼워넣어서, 컨테이너 전체의 get_text
    대신 실제 라벨 span(class에 sds-comps-text-type-* 포함) 하나만 골라 읽는다."""
    if el is None:
        return ""
    label = el.select_one('[class*="sds-comps-text-type-"]')
    return (label or el).get_text(" ", strip=True)


def _parse_search_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    wrap = soup.select_one("div.fds-news-item-list-tab")
    if not wrap:
        return []

    items: list[dict] = []
    for node in wrap.select(":scope > div"):
        title_a = node.select_one('a[data-heatmap-target=".tit"]')
        if not title_a or not title_a.get("href"):
            continue

        press_el = node.select_one(".sds-comps-profile-info-title-text")
        snippet_el = node.select_one(".sds-comps-text-type-body1")
        naver_a = node.select_one('a[href*="n.news.naver.com"]')

        article_date = ""
        for subtext in node.select(".sds-comps-profile-info-subtext"):
            article_date = _parse_date_text(subtext.get_text(strip=True))
            if article_date:
                break

        items.append(
            {
                "title": _label_text(title_a),
                "url": title_a["href"],
                "naver_url": naver_a["href"] if naver_a else None,
                "press": _label_text(press_el),
                "date": article_date,
                "snippet": _label_text(snippet_el),
            }
        )
    return items


def _is_no_results_page(html: str) -> bool:
    """실증: 좁은 날짜 구간에 기사가 0건이면 Naver가 검색 결과 목록(div.fds-news-item-list-tab)
    대신 안내 컨테이너(<div id="notfound" class="api_noresult_wrap">)를 담아 200 OK로 준다 —
    이걸 스로틀링(빈 응답)과 구분 못 하면 무한 재시도에 빠진다(collect_recent_issues("윕스")
    테스트에서 실측). 텍스트 문구 매칭이 아니라 이 특정 요소(id+class)의 존재 여부로 판단한다
    — 페이지 어딘가에 같은 문구가 우연히 섞여 있을 가능성을 피하기 위함."""
    soup = BeautifulSoup(html, "html.parser")
    return soup.select_one("#notfound.api_noresult_wrap") is not None


_DUPLICATE_PAGE_THRESHOLD = 0.8  # 이 비율 이상 URL이 이미 본 것과 겹치면 "더 이상 새 페이지 없음"


_MAX_PAGES_PER_WINDOW = 30  # 한 기간 구간 안에서 넘길 페이지 상한(안전핀) — 정상적으로는
                            # 중복 페이지 신호로 그 전에 멈춘다.


async def _search_window(
    client: httpx.AsyncClient, keyword: str, start_date: date, end_date: date, seen_urls: set[str]
) -> list[dict]:
    """[start_date, end_date] 구간을 1페이지부터 훑어 새 기사를 모은다(실증 확인된 pd=3+nso
    커스텀 기간 필터 사용 — _date_range_params 참고). 페이지 깊이로 기간을 추정하던 이전 방식
    대신, 목표 구간을 정확히 지정해서 검색하므로 "기간을 다 못 채웠나"를 추측할 필요가 없다.

    Naver는 구간 안에서도 실제 마지막 페이지를 넘어서면 빈 응답 대신 마지막 유효 페이지를 그대로
    다시 돌려주는 것으로 보인다(실증) — "빈 응답"과 "새 결과 없음(중복 페이지)"을 다른 신호로
    다룬다. 빈 응답은 일시적 스로틀링으로 보고 결과가 나올 때까지 무한 재시도한다(HTTP 레벨
    실패는 _fetch_search_page가 담당, 여기선 응답은 왔는데 파싱 결과가 0건인 경우를 담당) —
    스로틀링은 영구 차단이 아니라 간격을 두고 다시 보내면 통과한다(실증). 단, 파싱 결과가 0건인
    원인이 스로틀링이 아니라 이 구간에 애초에 기사가 없는 것(Naver가 안내 컨테이너를 담아 200
    OK로 응답 — `_is_no_results_page` 참고)일 수도 있다. 이 경우는 아무리 재시도해도 똑같은
    응답만 반복되므로(실증: collect_recent_issues("윕스")에서 좁은 1일짜리 구간이 무한 재시도에
    빠짐을 확인) 재시도 없이 즉시 이 구간을 빈 결과로 확정한다. 반면 반환된 URL 대부분
    (_DUPLICATE_PAGE_THRESHOLD 이상)이 이미 수집한 URL과 겹치면, 그건 실제로 이 구간의 마지막
    페이지에 도달했다는 신뢰할 수 있는 신호로 보고 재시도 없이 멈춘다(seen_urls는 검색 전체에
    걸쳐 호출자가 누적 관리 — 구간이 바뀌어도 유지되어야 중복 판단이 의미 있다).
    """
    articles: list[dict] = []
    for page_no in range(1, _MAX_PAGES_PER_WINDOW + 1):
        start_idx = (page_no - 1) * _PAGE_SIZE + 1

        while True:
            html = await _fetch_search_page(client, keyword, start_date, end_date, start_idx)  # HTTP 레벨 실패는 내부에서 무한 재시도
            page_items = _parse_search_page(html)
            if page_items or _is_no_results_page(html):
                break  # 기사 있음, 또는 이 구간엔 애초에 결과가 없다는 확정 신호 — 재시도 종료
            await asyncio.sleep(random.uniform(*_TRANSPORT_RETRY_DELAY_RANGE))  # 빈 응답 — 스로틀링으로 보고 재시도

        if not page_items:
            break  # 결과없음 확정(위에서 온 케이스) — 이 구간은 더 볼 페이지가 없음

        new_items = [a for a in page_items if a["url"] not in seen_urls]
        overlap_ratio = 1 - (len(new_items) / len(page_items))
        if overlap_ratio >= _DUPLICATE_PAGE_THRESHOLD:
            break  # 대부분 이미 본 URL — 이 구간의 끝에 도달했다는 신호(진짜 끝)

        seen_urls.update(a["url"] for a in page_items)
        articles.extend(new_items)
    return articles


_DEDUP_SIMILARITY_THRESHOLD = 0.86  # 제목 임베딩 코사인 유사도 임계값 — 뉴스 중복탐지 업계
                                     # 관행(대략 0.8대)을 참고하되, 오탐 병합(실제로는 다른
                                     # 사건인데 합쳐짐) 쪽을 더 경계해 다소 보수적으로 잡음.
_DEDUP_DATE_WINDOW_DAYS = 1  # 표현만 우연히 비슷한 별개 시점 기사가 잘못 합쳐지는 걸 막기 위해
                             # 최대한 좁게(당일) 잡는다.


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _days_apart(date_a: str, date_b: str) -> int | None:
    if not date_a or not date_b:
        return None
    return abs((date.fromisoformat(date_a) - date.fromisoformat(date_b)).days)


async def _dedup_cluster_by_embedding(articles: list[dict]) -> list[list[dict]]:
    """제목+본문 임베딩 유사도로 근접 중복(같은 사건, 다른 언론사 보도)을 LLM 그룹핑 앞단에서
    미리 합친다. 문자열 유사도(SequenceMatcher)는 언론사마다 표현이 달라 정확도가 낮았고(모듈
    상단 docstring 참고), LLM 그룹핑 한 번으로도 표현이 비슷한 기사를 놓치는 걸 확인했다. 
    제목/스니펫만 쓰면 텍스트가 짧아 정확도가 떨어져서, 이제 수집 단계에서 미리 확보해둔 본문까지 함께 임베딩한다.

    클러스터 대표값은 첫 기사로 고정하지 않고 멤버들의 평균으로 계속 갱신한다(leader
    clustering 기법). 첫 기사만 계속 기준으로 삼으면 어떤 기사가 먼저 처리되느냐에 따라 결과가
    달라지는 문제가 있고, 반대로 "아무 두 기사든 임계값만 넘으면 무조건 합친다"는 방식은 A-B가
    비슷하고 B-C가 비슷하면 실제로 안 닮은 A-C까지 사슬처럼 엮여버리는 문제(체이닝)가 있다 —
    평균으로 갱신하면 두 문제를 모두 완화할 수 있다.

    LLM 그룹핑을 대체하는 게 아니라 그 앞단에서 명백한 근접 중복만 먼저 줄여주는 전처리다 —
    관련성 판단(동명이인 등)과 표현이 크게 다른 같은 이슈 병합은 여전히 LLM 그룹핑이 담당한다.
    """
    if not articles:
        return []

    texts = [f"{a['title']} {a.get('body') or a.get('snippet') or ''}".strip() for a in articles]
    embeddings = await llm_client.embed_batch(texts)

    clusters: list[list[int]] = []
    centroids: list[list[float]] = []  # 클러스터 멤버 임베딩의 실행 평균
    for i, (article, emb) in enumerate(zip(articles, embeddings)):
        joined = None
        for c_idx, members in enumerate(clusters):
            anchor_date = articles[members[0]]["date"]  # 날짜창 판단은 최초 멤버 기준으로 충분
            days = _days_apart(article["date"], anchor_date)
            if days is not None and days > _DEDUP_DATE_WINDOW_DAYS:
                continue
            if _cosine_similarity(emb, centroids[c_idx]) >= _DEDUP_SIMILARITY_THRESHOLD:
                joined = c_idx
                break
        if joined is not None:
            clusters[joined].append(i)
            n = len(clusters[joined])
            centroids[joined] = [(c * (n - 1) + e) / n for c, e in zip(centroids[joined], emb)]
        else:
            clusters.append([i])
            centroids.append(list(emb))

    return [[articles[i] for i in members] for members in clusters]


def _relevance_only_system_prompt(keyword: str, subject_kind: str) -> str:
    relevance_line = _RELEVANCE_LINE[subject_kind].format(keyword=keyword)
    return (
        f'"{keyword}"는 {_SUBJECT_LABEL[subject_kind]}입니다(일반 명사와 우연히 같은 표기여도 이 의미로 판단하세요).\n'
        f'당신은 "{keyword}" 관련 뉴스를 수집하는 스크래퍼입니다.\n'
        "번호가 매겨진 기사 제목·날짜·요약 목록이 주어지면 다음 기준으로 판단하세요:\n"
        f"{relevance_line}"
        "relevant 필드에 관련 있다고 판단한 기사 번호(0부터, 입력 목록 인덱스 그대로)를 담으세요."
    )


class _RelevantIndices(BaseModel):
    relevant: list[int] = Field(description="관련 있다고 판단한 기사 번호(0부터, 입력 목록 인덱스 그대로)")


async def _filter_relevant_batch(keyword: str, subject_kind: str, articles: list[dict]) -> list[dict]:
    """제목+날짜+스니펫만으로 무관 기사를 걸러낸다 — 스니펫은 검색 결과 파싱 단계에서 이미
    확보돼 추가 요청 없이 쓸 수 있고(_group_into_issues의 관련성 판단과 동일한 근거), 본문은
    이 판단에 필요하지 않다. 본문 수집(_attach_body, 네트워크 요청)·임베딩(_dedup_cluster_by_embedding)
    보다 먼저 실행해서 무관 기사가 그 이후 단계까지 도달하지 않게 한다 — 무관 기사의 본문을
    아예 안 가져오므로 네트워크 비용도, 임베딩에 실리는 토큰량도 함께 줄어든다(대량 기사가
    임베딩 요청 한도를 넘는 문제의 근본 원인 완화 — llm_client.embed_batch의 배치 분할은
    그래도 필요한 안전망이지만, 애초에 실리는 양 자체를 줄이는 게 먼저).
    구조화 출력(with_structured_output) 사용 — _summarize_and_classify_batch/_reconstruct_batch와
    동일 패턴, 범위 밖 인덱스는 걸러낸다. 이 필터를 통과 못한
    (관련 없다고 판단된) 기사가 있어도, 이후 _group_into_issues(관련성 재판단)와
    _filter_unrelated_issues(결정론적 키워드 포함 검사)가 다시 한 번 노이즈를 걸러내는 안전망으로
    남아 있다 — 여기서 뭔가 놓쳐도 최종 결과 전까지 노이즈 제거 기회가 두 번 더 있다."""
    lines = "\n".join(f"{i}. [{a['date']}] {a['title']} — {a.get('snippet') or ''}" for i, a in enumerate(articles))
    chain = ChatPromptTemplate.from_messages(
        [("system", _relevance_only_system_prompt(keyword, subject_kind)), ("human", "기사 목록:\n{articles_text}")]
    ) | llm_client.get_chat_model().with_structured_output(_RelevantIndices)
    try:
        result: _RelevantIndices = await chain.ainvoke({"articles_text": lines})
    except Exception:  # noqa: BLE001 — 예상 못한 오류도 여기서 삼켜야 이 배치가 전체 수집을 중단시키지 않는다
        return articles  # 판정 실패 시 걸러내지 않고 전부 통과 — 이후 두 안전망이 여전히 노이즈를 걸러낸다
    valid = [i for i in result.relevant if 0 <= i < len(articles)]
    return [articles[i] for i in valid]


async def _filter_relevant(keyword: str, subject_kind: str, articles: list[dict]) -> list[dict]:
    """_BATCH_SIZE 단위로 나눠 관련성 필터링(제목+스니펫만 사용, 본문 불필요)."""
    return await _run_batched(articles, lambda batch: _filter_relevant_batch(keyword, subject_kind, batch))


async def _group_into_issues(keyword: str, subject_kind: str, articles: list[dict]) -> list[list[dict]]:
    """제목+날짜+스니펫을 LLM에 보내 무관한 기사(동명이인 등)를 걸러내고 같은 이슈/트렌드끼리
    그룹핑. 스니펫은 검색 단계에서 이미 확보해둔 정보라 추가 요청 없이 공짜로 쓸 수 있는데,
    제목만으로는 관련성 판단 근거가 부족한 경우가 있다(실증: "정삼용 시큐아이 대표..." 기사가
    회사명과 무관한 동음이의어(무선 침입 방지 시스템의 업계 약어 WIPS)를 담고 있었는데 제목만
    봐서는 이를 알 수 없었음 — collect_recent_issues("윕스") 테스트에서 실측)."""
    lines = "\n".join(f"{i}. [{a['date']}] {a['title']} — {a.get('snippet') or ''}" for i, a in enumerate(articles))
    raw = await llm_client.chat(
        [
            {"role": "system", "content": _group_system_prompt(keyword, subject_kind)},
            {"role": "user", "content": f"기사 목록:\n{lines}"},
        ],
        temperature=0,
    )
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    try:
        groups: list[list[int]] = json.loads(raw)["groups"]
        seen = [i for group in groups for i in group]
        if len(seen) != len(set(seen)) or any(i < 0 or i >= len(articles) for i in seen):
            raise ValueError("인덱스 중복/범위 초과")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return [[a] for a in articles]  # 그룹핑 응답이 깨지면 필터링 없이 기사마다 별도 이슈로 축소

    return [[articles[i] for i in group] for group in groups]


async def _group_into_issues_batched(keyword: str, subject_kind: str, articles: list[dict]) -> list[list[dict]]:
    """대량 기사도 정확히 그룹핑하기 위해 _BATCH_SIZE 단위로 나눠 각자 그룹핑(관련성 필터링 +
    배치 내 그룹핑)한 뒤 이어붙인다. 실측: 기사 180건을 _group_into_issues에 한 번에 넣으니
    응답 JSON이 깨져 매번 "그룹핑 실패 → 기사마다 별도 이슈" 폴백이 발동해, 같은 사건 기사
    10여 건이 중복 이슈로 그대로 나열되는 문제를 확인(과거 _summarize_issues/_classify_issues
    시절과 동일한 근본 원인 — 지금은 _summarize_and_classify로 통합·강건화됨) — 그래서 배치가
    필수.

    배치 경계를 넘는 근접 중복은 여기서 다시 합치지 않는다 — 대신 이 함수를 호출하는
    _group_and_dedup이 호출 전에 전체 기사에 임베딩 기반 사전 병합(_dedup_cluster_by_embedding)을
    이미 수행해서, 이 함수가 받는 articles는 이미 근접 중복이 대부분 제거된 대표 기사 목록이다.
    과거엔 배치 그룹의 대표 기사만 모아 재귀적으로 한 번 더 LLM에 "같은 트렌드면 합쳐도 된다"는
    느슨한 기준으로 병합시켰는데, 이 2차 패스가 서로 무관한 이슈의 대표끼리도 과도하게 합쳐버리는
    문제를 확인했다(실증: collect_recent_issues("윕스")에서 기사 76건이 최종 이슈 5개로
    뭉개지고, 이슈당 근거 기사 상한(2건)에 가려 대부분 기사가 안 보이게 됨). LLM에게 이미 만든
    그룹/레이블을 다시 병합시키는 패턴은 학계에서도 같은 실패 유형이 보고된다 — 세분화된
    클러스터를 과도하게 뭉치거나 반대로 분산시킴(arXiv:2410.00927). 그래서 병합 판단 자체를
    LLM에 다시 맡기지 않고 결정론적 임베딩 사전 병합으로 앞단에서 끝낸다."""
    batch_groups: list[list[dict]] = []
    for i in range(0, len(articles), _BATCH_SIZE):
        batch_groups.extend(await _group_into_issues(keyword, subject_kind, articles[i : i + _BATCH_SIZE]))
    return batch_groups


async def _select_top_issues(issue_groups: list[list[dict]]) -> list[list[dict]]:
    """기사 수(관심도 proxy) 많은 순으로 랭킹(개수 상한 없음), 이슈당 대표 기사(최대 2건) 선정.

    대표 기사는 언론사 등장 순서가 아니라 본문 임베딩 기준 medoid(그룹 내 다른 기사들과 평균
    유사도가 가장 높은 기사)로 뽑는다 — 예전엔 그룹 순서상 먼저 나온 기사를 그냥 썼는데, 그
    순서가 실제로 "이 이슈를 가장 잘 대표하는 기사"를 보장하지 않아서 무관한 기사가 대표로 뽑히는 문제가 있었다. medoid는 통계학에서 이상치에
    강건한 대표값 선정 기법으로 널리 쓰인다. 2번째 기사는 medoid와 언론사가 다르면서 medoid와
    가장 유사한 기사(언론사 다양성은 2순위로 유지)."""
    ranked = sorted(issue_groups, key=len, reverse=True)

    selected: list[list[dict]] = []
    for group in ranked:
        if len(group) == 1:
            selected.append(group)
            continue

        texts = [f"{a['title']} {a.get('body') or a.get('snippet') or ''}".strip() for a in group]
        embeddings = await llm_client.embed_batch(texts)

        avg_similarity = [
            sum(_cosine_similarity(embeddings[i], embeddings[j]) for j in range(len(group)) if j != i) / (len(group) - 1)
            for i in range(len(group))
        ]
        medoid_idx = max(range(len(group)), key=lambda i: avg_similarity[i])

        picked = [group[medoid_idx]]
        seen_press = {group[medoid_idx]["press"]}
        others = sorted(
            (i for i in range(len(group)) if i != medoid_idx),
            key=lambda i: _cosine_similarity(embeddings[i], embeddings[medoid_idx]),
            reverse=True,
        )
        for i in others:  # 1순위: medoid와 언론사 다르면서 가장 유사한 기사
            if group[i]["press"] not in seen_press:
                picked.append(group[i])
                break
        if len(picked) < _ARTICLES_PER_ISSUE and others:  # 다른 언론사가 없으면 차순위 유사 기사로
            picked.append(group[others[0]])

        selected.append(picked[:_ARTICLES_PER_ISSUE])
    return selected


async def _fetch_naver_mirror_body(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        net.raise_if_ssl_trust_error(e)
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    body_tag = soup.select_one("article#dic_area") or soup.select_one("#articleBodyContents")
    return body_tag.get_text(" ", strip=True) if body_tag else ""


async def _attach_body(client: httpx.AsyncClient, article: dict) -> dict:
    if article["naver_url"]:
        body = await _fetch_naver_mirror_body(client, article["naver_url"])
        if body:
            return {**article, "body": body}
    return {**article, "body": article["snippet"]}


def _oldest_date(articles: list[dict]) -> str:
    dates = [a["date"] for a in articles if a["date"]]
    return min(dates) if dates else ""


def _earliest_article(articles: list[dict]) -> dict | None:
    """issue_title과 occurred_month를 항상 같은 기사에서 뽑기 위한 대표 기사 선정 — 예전엔
    title은 group[0], occurred_month는 그룹 전체의 최소 날짜(_oldest_date)로 서로 다른 기사에서
    뽑아서, 실제로는 A 기사의 제목인데 날짜는 무관한 B 기사(같은 그룹에 잘못 묶인 기사) 것이
    나오는 불일치가 있었다(실증: "단독 대표 체제 전환" 기사의 제목에 전혀 다른 MOU 기사의 날짜가
    붙어 나옴). 날짜순으로 가장 이른 기사를 대표로 삼아 title/occurred_month를 항상 함께 뽑는다."""
    dated = [a for a in articles if a["date"]]
    if dated:
        return min(dated, key=lambda a: a["date"])
    return articles[0] if articles else None


_BATCH_SIZE = 25  # 한 번의 LLM 호출에 넣는 이슈 개수 상한 — 이보다 많으면 이 단위로 나눠 호출한다.
                  # 실측: 이슈 180개를 한 번에 분류 요청하니 응답 JSON이 깨져 전부 "기타/일반"으로
                  # 안전 폴백되는 걸 확인(길이 불일치) — 이슈가 많을수록 오히려 100% 폴백되는
                  # 역효과라 배치가 필수. 제목+스니펫만 보내는 가벼운 그룹핑 단계용 크기다.

_SUMMARY_BATCH_SIZE = 6  # 요약+분류 단계 전용 배치 크기(_BATCH_SIZE보다 작음) — 이 단계는
                         # 기사 본문 전체를 프롬프트에 싣는다. 한 번에 25개씩(본문 포함) 보내면
                         # LLM이 뒤쪽 항목 내용을 놓치고 앞선 답변을 그대로 반복하는 현상을
                         # 실측(collect_recent_issues("윕스") 대량 테스트에서 tech_rnd 이슈 14개가
                         # 서로 무관한 제목인데도 gist가 전부 똑같이 나옴) — "긴 컨텍스트에서
                         # 중간 내용을 놓친다"는 잘 알려진 LLM 현상(컨텍스트 로트)이고, 4~8개
                         # 청크가 20개+보다 안정적이라는 연구 결과를 참고해 보수적으로 잡음.


async def _run_batched(items: list[dict], fn, batch_size: int = _BATCH_SIZE) -> list:
    """items를 batch_size 단위로 나눠 fn을 호출하고 결과를 이어붙인다."""
    results: list = []
    for i in range(0, len(items), batch_size):
        results.extend(await fn(items[i : i + batch_size]))
    return results


class _IssueSummaryItem(BaseModel):
    index: int = Field(description="입력 이슈 목록의 번호(0부터) — 원본 순서 그대로 에코")
    actor: str = Field(
        description="이 이슈의 핵심 행위를 실제로 한 주체(회사/사람). 실제 행위자 혹은 기사에서 설명하고자 하는 주체."
    )
    gist: str = Field(description="actor를 주어로 핵심 내용을 1~2문장 요약(제목 반복 금지, 본문에만 있는 구체적 내용)")
    event_id: str = Field(description="분류체계에서 가장 적합한 event_id 하나. 애매하면 general_other.general_pr")


class _IssueSummaryBatch(BaseModel):
    items: list[_IssueSummaryItem]


_SUMMARIZE_CLASSIFY_SYSTEM = f"""당신은 뉴스 기사 본문을 읽고 핵심을 요약하고, 아래 이벤트 유형
분류체계에 따라 분류하는 전문가입니다.

[분류체계]
{event_taxonomy.TAXONOMY_PROMPT_TEXT}

번호가 매겨진 이슈 목록이 주어지며, 각 이슈에는 그 사건을 다루는 기사 본문(1~2건)이 포함되어
있습니다. 이슈마다 반드시 아래 순서로 판단하세요:
1. 먼저 actor(핵심 행위를 실제로 한 주체)를 판단하세요. 행위 주체 혹은 기사에서 설명하고자 하는 주체. 단순 언급 및 인용과 오인하지 말 것
2. 그다음 actor를 주어로 핵심 내용을 1~2문장으로 요약하세요(제목을 반복하지 말고 본문에만
   있는 구체적인 내용을 담으세요).
3. 위 분류체계에서 가장 적합한 event_id 하나로 분류하세요. 애매하면 general_other.general_pr을 쓰세요.

각 항목의 index 필드에 원본 목록의 번호를 반드시 그대로 담아 응답하세요."""


async def _summarize_and_classify_batch(issues: list[dict]) -> list[dict]:
    """이슈 배치 → [{"gist":..., "event_id":...}, ...] (원본 순서 그대로). 응답 항목마다 자기
    index를 직접 들고 있어(포지션이 아니라 인덱스로 매칭) 일부 항목이 누락돼도 그 항목만 개별
    폴백되고 나머지는 살아남는다 — structured_output(strict json schema)도 필드 타입·필수 여부
    같은 구조적 정합성만 보장할 뿐 배열 "개수"가 입력과 일치하는 것까지는 보장하지 않으므로
    (OpenAI 공식 문서 확인) 여전히 필요한 방어. fit/service.py의 with_structured_output 패턴 재사용
    — 이전엔 요약/분류를 각각 별도 호출(총 2회)로 나눠 raw 텍스트+정규식 코드펜스 제거+수동
    json.loads+길이검증을 했고, 길이가 하나라도 안 맞으면 배치 전체를 버렸다(실측:
    collect_recent_issues("윕스") 테스트에서 17개 중 2개가 안 맞아 17개 전부 빈 gist로 폴백됨)."""
    lines = []
    for i, issue in enumerate(issues):
        bodies = "\n".join(f"  - {a['body']}" for a in issue["articles"] if a.get("body"))
        lines.append(f"{i}. [{issue['occurred_month']}] {issue['issue_title']}\n{bodies}")

    chain = ChatPromptTemplate.from_messages(
        [("system", _SUMMARIZE_CLASSIFY_SYSTEM), ("human", "이슈 목록:\n{issues_text}")]
    ) | llm_client.get_chat_model().with_structured_output(_IssueSummaryBatch)
    result: _IssueSummaryBatch = await chain.ainvoke({"issues_text": "\n\n".join(lines)})

    by_index = {item.index: item for item in result.items}
    results: list[dict] = []
    for i in range(len(issues)):
        item = by_index.get(i)
        if item:
            results.append({"gist": item.gist, "event_id": item.event_id})
        else:
            results.append({"gist": "", "event_id": event_taxonomy._FALLBACK_EVENT_ID})  # 이 항목만 개별 폴백
    return results


async def _summarize_and_classify(issues: list[dict]) -> list[dict]:
    """이슈별 gist 요약 + event_id 분류를 한 번의 구조화 LLM 호출로 함께 처리한다(원본 기사
    본문을 보면서 요약과 분류를 동시에 하므로, 분류만 따로 하면서 이미 손실된 요약문만 보고
    판단하던 것보다 근거가 더 풍부하다). 이슈가 많으면 _SUMMARY_BATCH_SIZE 단위로 나눠 호출한다
    (그룹핑의 _BATCH_SIZE보다 작음 — 이 단계는 본문 전체를 실어 프롬프트가 훨씬 무겁다)."""
    return await _run_batched(issues, _summarize_and_classify_batch, batch_size=_SUMMARY_BATCH_SIZE)


class _FinalIssueItem(BaseModel):
    index: int = Field(description="입력 이슈 목록의 번호(0부터) — 그대로 에코")
    reasoning: str = Field(
        description="기준 중요도에서 왜 올리거나 내렸는지(또는 유지했는지) 한 문장 근거. "
        "예: '단순 인용출처라 기준보다 낮춤', '회사가 직접 당사자인 확정 발표라 기준 유지'."
    )
    company_importance: int = Field(
        description=(
            "이 기업 관점에서 실제 중요도(1~10). 프롬프트에 준 '기준 중요도'(event_id의 일반적인 "
            "중요도)에서 시작해, 이 기사가 실제로 확정된 사실인지 추측·홍보성인지, 회사가 직접 "
            "당사자인지 단순 언급·인용출처에 불과한지, 전사적 영향인지 지엽적인지에 따라 위/아래로 "
            "조정한다. 맨땅에서 새로 매기지 말고 기준값 대비 조정으로 판단할 것."
            "실제 목표로 하는 기업에 대한 기사인지도 고려한다. 예: '단순 언급·인용출처라 기준보다 낮춤', '회사가 직접 당사자인 확정 발표라 기준 유지'"
        )
    )
    supersedes: list[int] = Field(
        default_factory=list,
        description="이 이슈가 시간상 잇는/갱신하는 이전 이슈의 인덱스들(같은 사안의 후속 보도·전개). 없으면 빈 배열.",
    )


class _FinalIssueBatch(BaseModel):
    items: list[_FinalIssueItem]


_RECONSTRUCT_SYSTEM = """당신은 기업 뉴스 이슈 목록을 검토해 (1) 이 기업 입장에서 실제 중요도를
재평가하고 (2) 같은 사안이 시간에 따라 이어지는 이슈끼리 연결하는 전문가입니다.

번호가 매겨진 이슈 목록(제목+날짜+요약+기준 중요도)이 주어집니다. 기준 중요도는 사건 유형만
보고 매긴 일반적인 값이고, 실제 기사 내용을 보면 더 높거나 낮을 수 있습니다. 각 이슈마다:
1. reasoning + company_importance(1~10): 기준 중요도에서 시작해 조정하세요. 확정된 사실이면
   유지, 추측·홍보성 기사면 낮추고, 회사가 직접 당사자면 유지, 단순 언급·인용출처에 불과하면
   크게 낮추고, 전사적 영향이면 유지, 지엽적 영향이면 낮추세요. 왜 그렇게 조정했는지(또는
   유지했는지) reasoning에 한 문장으로 남기세요.
2. supersedes: 이 이슈가 더 이전의 다른 이슈의 후속·갱신·전개라면 그 인덱스를 적으세요(예:
   "공동대표 체제 전환" 이슈가 나중의 "단독대표 체제 전환" 이슈로 이어진다면, 단독대표 이슈의
   supersedes에 공동대표 이슈의 인덱스를 담습니다). 단순히 날짜가 가깝거나 주제가 겹친다고
   연결하지 마세요 — 실제로 같은 사안의 전개일 때만 연결하세요.

각 항목의 index 필드에 원본 목록의 번호를 반드시 그대로 담아 응답하세요."""


async def _reconstruct_batch(issues: list[dict]) -> list[dict]:
    lines = []
    for i, issue in enumerate(issues):
        info = event_taxonomy.EVENT_LOOKUP.get(issue["event_id"]) or event_taxonomy.EVENT_LOOKUP[event_taxonomy._FALLBACK_EVENT_ID]
        lines.append(
            f"{i}. [{issue['occurred_month']}] {issue['issue_title']} (기준 중요도: {info['base_importance']})\n"
            f"   {issue['gist']}"
        )
    chain = ChatPromptTemplate.from_messages(
        [("system", _RECONSTRUCT_SYSTEM), ("human", "이슈 목록:\n{issues_text}")]
    ) | llm_client.get_chat_model().with_structured_output(_FinalIssueBatch)
    result: _FinalIssueBatch = await chain.ainvoke({"issues_text": "\n\n".join(lines)})

    by_index = {item.index: item for item in result.items}
    results: list[dict] = []
    for i in range(len(issues)):
        item = by_index.get(i)
        if item:
            supersedes = [s for s in item.supersedes if 0 <= s < len(issues) and s != i]
            results.append({
                "company_importance": max(1, min(10, item.company_importance)),
                "reasoning": item.reasoning,
                "supersedes": supersedes,
            })
        else:
            info = event_taxonomy.EVENT_LOOKUP.get(issues[i]["event_id"]) or event_taxonomy.EVENT_LOOKUP[event_taxonomy._FALLBACK_EVENT_ID]
            results.append({"company_importance": info["base_importance"], "reasoning": "", "supersedes": []})  # 이 항목만 개별 폴백(기준값 유지)
    return results


async def _reconstruct(issues: list[dict]) -> list[dict]:
    """이슈별(제목+날짜+gist만, 본문 없음) 실제 기업 연관도/중요도 재평가와 시간대 연결
    (supersedes)을 한 번의 구조화 LLM 호출로 처리한다. 본문을 안 실어 가벼우므로 그룹핑과 같은
    _BATCH_SIZE를 쓴다."""
    return await _run_batched(issues, _reconstruct_batch, batch_size=_BATCH_SIZE)


def _merge_timelines(finalized: list[dict], reconstructions: list[dict]) -> list[dict]:
    """supersedes로 이어진 이슈들을 체인으로 묶어 하나로 병합한다. 병합된 이슈의 gist는 LLM이
    새로 쓰는 게 아니라, 이미 나온 gist들을 시간순으로 화살표로 이어붙인 것뿐이다(파이썬 문자열
    조립) — 여러 사건을 하나의 새 문장으로 재서술시키면 그 단계에서 환각이 생기기 쉽다는 연구
    결과가 있어(타임라인 요약에서 "날짜별 사건 통합" 단계가 주요 환각 원인 중 하나), 새 문장을
    쓰게 하지 않고 기존 문장을 그대로 잇는 방식을 택했다. 기사(출처)도 전부 보존한다."""
    n = len(finalized)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(older: int, newer: int) -> None:
        root_older, root_newer = find(older), find(newer)
        if root_older != root_newer:
            parent[root_older] = root_newer  # 최신 쪽을 대표로 유지

    for i, r in enumerate(reconstructions):
        for prev in r["supersedes"]:
            union(prev, i)

    chains: dict[int, list[int]] = {}
    for i in range(n):
        chains.setdefault(find(i), []).append(i)

    merged: list[dict] = []
    for members in chains.values():
        members.sort(key=lambda i: finalized[i]["occurred_month"])
        latest = finalized[members[-1]]
        importances = [reconstructions[i]["company_importance"] for i in members if reconstructions[i]["company_importance"] is not None]
        importance = max(importances) if importances else 1

        if len(members) > 1:
            gist = " → ".join(f"[{finalized[i]['occurred_month']}] {finalized[i]['gist']}" for i in members)
        else:
            gist = latest["gist"]

        merged.append(
            {
                "issue_title": latest["issue_title"],
                "occurred_month": latest["occurred_month"],
                "gist": gist,
                "event_id": latest["event_id"],
                "articles": [a for i in members for a in finalized[i]["articles"]],
                "importance": importance,
            }
        )
    return merged


def _group_into_topics(issues: list[dict]) -> list[dict]:
    """event_taxonomy의 대분류(category_id)를 토픽 버킷으로 이슈를 묶는다. 토픽 개수는 taxonomy가
    가진 만큼(최대 11개) 그대로 쓰고 임의로 자르지 않는다 — 토픽 내부/토픽 간 정렬만 중요도로 한다.
    중요도는 taxonomy의 고정값이 아니라 _reconstruct가 이슈 내용을 보고 직접 매긴 값을 그대로
    쓴다(event_id는 카테고리 라벨을 붙이는 용도로만 사용)."""
    buckets: dict[str, dict] = {}
    for issue in issues:
        event_id = issue["event_id"]
        info = event_taxonomy.EVENT_LOOKUP.get(event_id) or event_taxonomy.EVENT_LOOKUP[event_taxonomy._FALLBACK_EVENT_ID]
        issue["event_id"] = event_id if event_id in event_taxonomy.EVENT_LOOKUP else event_taxonomy._FALLBACK_EVENT_ID
        bucket = buckets.setdefault(
            info["category_id"], {"category_id": info["category_id"], "label_ko": info["category_label"], "issues": []}
        )
        bucket["issues"].append(issue)

    topics = list(buckets.values())
    for topic in topics:
        topic["issues"].sort(key=lambda i: i["importance"], reverse=True)
    topics.sort(key=lambda t: max(i["importance"] for i in t["issues"]), reverse=True)
    return topics


async def _finalize_issues(issues: list[dict]) -> tuple[list[dict], list[dict]]:
    """gist·분류·중요도 재평가·시간대 연결을 붙여 토픽별로 묶은 최종본과, 오류 대비용 원문(본문
    포함) 보존본을 함께 반환한다. 최종본의 이슈는 title/url/press/date(출처)+gist+event_id+
    importance만 남기고, 원문 보존본은 저장 전용(프롬프트에는 안 넣음) — 요약이 잘못됐거나
    재처리가 필요할 때 재스크래핑 없이 다시 쓸 수 있게 한다."""
    if not issues:
        return issues, issues

    raw_issues = [
        {"issue_title": i["issue_title"], "occurred_month": i["occurred_month"], "articles": i["articles"]}
        for i in issues
    ]

    summaries = await _summarize_and_classify(issues)
    finalized = [
        {
            "issue_title": issue["issue_title"],
            "occurred_month": issue["occurred_month"],
            "gist": s["gist"],
            "event_id": s["event_id"],
            "articles": [{k: v for k, v in a.items() if k != "body"} for a in issue["articles"]],
        }
        for issue, s in zip(issues, summaries)
    ]

    reconstructions = await _reconstruct(finalized)
    merged = _merge_timelines(finalized, reconstructions)
    topics = _group_into_topics(merged)
    return topics, raw_issues


def _months_ago(d: date, months: int) -> date:
    month_index = d.month - 1 - months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def _search_rounds(today: date) -> list[tuple[date, date]]:
    """검색 라운드 경계를 오늘 기준으로 생성한다 — [최근 6개월(롤링)] → [올해 나머지, 있을 때만]
    → [작년] → [재작년] → (4/1 이전이면) [그전해]. 서로 겹치지 않게 과거로 갈수록 더 이전 구간만
    순증분으로 만든다(하루도 중복 검색 안 함) — `cursor`가 "이미 검색된 가장 이른 날짜"를 추적해,
    각 캘린더 연도 라운드를 그 전날까지로 잘라낸다(6개월 라운드가 작년까지 걸치는 1~6월 실행
    시에도 겹침 없이 자동으로 처리됨).

    "6개월"만 롤링(달력 월 기준 정확히 6개월 전)이고, 나머지 라운드는 365일 롤링이 아니라
    캘린더 연도(1/1~12/31) 경계로 자른다 — 그래야 매 라운드가 항상 "그 해 전체"(또는 6개월
    라운드가 못 채운 나머지)가 되어 하루 이틀짜리 자투리 구간이 생기지 않는다(365일 롤링이었을
    때의 문제 — collect_recent_issues("윕스") 테스트에서 좁은 1일 구간이 무한 재시도에 빠지는 걸
    실측). 4월 1일 이전(그 해 데이터가 아직 적을 시점)에는 캘린더 연도 라운드를 하나 더 늘려
    총 4개년까지 본다.
    """
    six_month_start = _months_ago(today, _SIX_MONTH_BACK_MONTHS)
    rounds = [(six_month_start, today)]
    cursor = six_month_start  # 이 날짜부터는 이미 검색됨 — 다음 라운드는 이 전날까지만

    years_back = _CALENDAR_YEARS_BACK
    if today < date(today.year, _EARLY_YEAR_CUTOFF_MONTH, 1):
        years_back += _CALENDAR_YEARS_BACK_EARLY_EXTRA

    for i in range(years_back + 1):  # i=0: 올해 나머지, i=1..: 작년/재작년/(그전해)
        year = today.year - i
        year_start = date(year, 1, 1)
        year_end = min(date(year, 12, 31), cursor - timedelta(days=1))
        if year_end >= year_start:
            rounds.append((year_start, year_end))
            cursor = year_start

    return rounds


def _filter_unrelated_issues(keyword: str, subject_kind: str, issue_groups: list[list[dict]]) -> list[list[dict]]:
    """회사 이슈(subject_kind=="company")에 한해, 그룹 내 어떤 기사의 title/본문에도 회사명이
    전혀 등장하지 않으면 그 그룹을 통째로 제거하는 결정론적 안전망. LLM 관련성 판단이 놓친 완전
    무관 기사를 마지막에 한 번 더 걸러낸다. snippet이 아니라 body로 확인하는 이유는,
    snippet은 네이버 검색결과 자체가 만든 발췌문이라 느슨한 매칭에도 검색어가 강조돼 남아있을
    수 있어 실제 본문 기준이 더 정확하다. 산업/직무 트렌드 검색은 특정 회사를 짚는 게 아니라 이 필터가 의미가 없어 적용하지
    않는다."""
    if subject_kind != "company":
        return issue_groups
    return [
        group for group in issue_groups
        if any(keyword in a["title"] or keyword in (a.get("body") or a.get("snippet") or "") for a in group)
    ]


async def _group_and_dedup(keyword: str, subject_kind: str, articles: list[dict]) -> list[list[dict]]:
    """근접 중복 사전 병합(_dedup_cluster_by_embedding) → LLM 그룹핑(대표 기사만 대상) → 원래
    클러스터로 확장 → 회사 이슈는 결정론적 무관 기사 필터까지 적용한 최종 이슈 그룹."""
    clusters = await _dedup_cluster_by_embedding(articles)
    representatives = [c[0] for c in clusters]
    rep_to_cluster = {id(rep): c for rep, c in zip(representatives, clusters)}

    issue_groups = await _group_into_issues_batched(keyword, subject_kind, representatives)
    expanded = [[a for rep in group for a in rep_to_cluster[id(rep)]] for group in issue_groups]

    return _filter_unrelated_issues(keyword, subject_kind, expanded)


async def _collect_issues(
    search_query: str,
    subject_kind: str,
    on_raw_ready: Callable[[list[dict]], None],
    display_keyword: str | None = None,
) -> tuple[list[dict], bool]:
    """검색어 → (이슈 목록, 노이즈 많음 여부). _search_rounds가 정한 라운드(6개월→올해 나머지→
    작년→재작년→(4/1 이전이면)그전해)를 순서대로 검색하고, 이슈가 _MIN_ISSUES(5) 미만이면 다음
    라운드로 넘어간다(_date_range_params 참고 — pd=3+nso 커스텀 기간 필터로 구간을 정확히
    지정, 페이지 깊이로 기간을 추측하지 않음). 한 라운드가 목표 구간을 다 못 채우고 멈췄으면
    (중간에 중복 페이지 신호 등으로) 그 빈 구간만 1페이지부터 다시 검색해서 채운다. 그 라운드에서
    찾은 이슈는 개수 제한 없이 전부(중요도순 랭킹만 적용) 반환한다. 이슈별로 대표 기사(최대 2건,
    본문 유사도 기준 medoid 선정 — _select_top_issues 참고)와 발생 시점(occurred_month)을 포함한다.

    display_keyword: LLM 관련성 판단 프롬프트에 보여줄 이름(검색어에 "기업"/대표명 등 부가어가
    섞여도 프롬프트에는 원래 이름만 노출하기 위함). 생략하면 search_query를 그대로 쓴다.

    on_raw_ready: 매 라운드 스크래핑(본문 확보까지) 직후, 그룹핑/임베딩/요약을 시작하기 전에
    호출된다(라운드마다 누적된 전체 articles로 매번 다시 호출 — 최신 스냅샷으로 덮어쓰기 용도).
    스크래핑은 재수집 비용이 가장 크고, 뒤이은 LLM/임베딩 처리는 실패 가능성이 있는 단계라
    (예: 임베딩 배치 크기 초과) 그 실패가 이미 확보한 스크래핑 결과까지 날려버리지 않도록,
    저장을 선별 단계보다 먼저 끝내둔다. 필수 인자 — 저장을 건너뛰는 호출은 허용하지 않는다
    (의무 저장: 재수집 비용이 가장 큰 데이터를 저장 누락으로 잃는 일을 원천 차단).

    검색 결과가 없거나 전부 실패해도 예외를 던지지 않고 빈 리스트를 반환한다 — 임의의 키워드에
    최근 언급 기사가 없는 것은 정상적인 결과다.
    """
    display_keyword = display_keyword or search_query
    async with httpx.AsyncClient(follow_redirects=True) as client:
        articles: list[dict] = []      # 관련성 필터 + 본문 수집까지 끝난 기사(선별 단계 입력)
        seen_urls: set[str] = set()
        selected: list[list[dict]] = []
        found_count = 0     # 관련성 필터 이전, 검색으로 실제 찾은 기사 수(누적) — "검색 결과 자체가 없음" 판단용
        relevant_count = 0  # 관련성 필터 통과 기사 수(누적) — 노이즈 비율 계산용(임베딩/그룹핑 완료를 기다릴 필요 없음)

        for window_start, window_end in _search_rounds(date.today()):
            round_new: list[dict] = await _search_window(client, search_query, window_start, window_end, seen_urls)

            oldest = _oldest_date(round_new)
            if oldest and oldest > window_start.isoformat():
                # 이번 라운드가 목표 구간(window_start까지)을 다 못 채우고 멈췄다 — 남은 구간만
                # 1페이지부터 새로 검색해서 채운다(한 번만, 재귀적으로 다시 재시도하진 않음).
                gap_end = date.fromisoformat(oldest) - timedelta(days=1)
                if gap_end >= window_start:
                    round_new.extend(await _search_window(client, search_query, window_start, gap_end, seen_urls))

            found_count += len(round_new)

            if round_new:
                # 본문 수집(네트워크 요청)·임베딩보다 먼저, 제목+스니펫만으로 무관 기사를 걸러낸다
                # (_filter_relevant 참고) — 둘 다 검색 결과 파싱 단계에서 이미 확보된 값이라 추가
                # 요청이 필요 없다. 무관 기사는 본문을 아예 안 가져오므로 네트워크 비용도, 뒤이은
                # 임베딩에 실리는 토큰량도 함께 줄어든다.
                round_new = await _filter_relevant(display_keyword, subject_kind, round_new)
            relevant_count += len(round_new)

            if round_new:
                # 본문을 그룹핑/대표기사 선정보다 먼저 확보해둔다 — 제목만 보고 고른 뒤 나중에
                # 본문을 붙이면, 중복 판단·대표 선정 전부 제목만으로 하게 돼 정확도가 떨어진다
                # (실증: 대표 기사도 언론사 다양성만으로 뽑혀 실제 사건과 다른 기사가 섞여 들어감).
                # 수집 단계에서 한 번만 가져오면 이후 모든 단계가 같은 본문을 재사용한다.
                fetched = await asyncio.gather(*[_attach_body(client, a) for a in round_new])
                round_new = list(fetched)
            articles.extend(round_new)

            on_raw_ready(articles)  # 임베딩 이전 — 이후 단계가 실패해도 이미 저장됨

            if not found_count:
                break  # 검색 결과 자체가 없음 — 기간을 늘려도 소용없음

            if articles:
                issue_groups = await _group_and_dedup(display_keyword, subject_kind, articles)
                selected = await _select_top_issues(issue_groups)

            if len(selected) >= _MIN_ISSUES:
                break

        if not selected:
            return [], False

        noisy = found_count >= _NOISE_MIN_ARTICLES and (1 - relevant_count / found_count) > _NOISE_RATIO_THRESHOLD

    issues: list[dict] = []
    for group in selected:
        primary = _earliest_article(group)
        issues.append(
            {
                "issue_title": primary["title"] if primary else "",
                "occurred_month": (primary["date"][:7] if primary and primary["date"] else ""),
                "articles": [
                    {k: v for k, v in a.items() if k not in ("naver_url", "snippet")} for a in group
                ],
            }
        )
    return issues, noisy


def _company_query_variants(company_name: str, ceo_name: str | None) -> list[str]:
    """채용공고를 올린 회사(=검색 노이즈가 실제로 문제되는 대상) 검색 시, 결과가 노이즈로
    판정되면 순서대로 시도할 더 구체적인 검색어. Naver는 큰따옴표를 정확일치 문법으로 존중하지
    않지만(실증 확인됨), "기업"/대표명을 추가 검색어로 넣는 것 자체는 실제로 결과를 좁혀준다."""
    variants = [f'"{company_name}"', f'기업 "{company_name}"']
    if ceo_name:
        variants.append(f'"{company_name}" "{ceo_name}"')
        variants.append(f'기업 "{company_name}" "{ceo_name}"')
    return variants


async def collect_recent_issues(
    company_name: str,
    on_raw_ready: Callable[[list[dict]], None],
    ceo_name: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """채용공고를 올린 회사의 최근 이슈 목록 (company_report가 사용).
    반환: (토픽별로 묶인 이슈 목록(event_taxonomy.py 기준, gist+출처+중요도), 원문 보존본(본문 포함, 저장 전용)).

    검색 결과 노이즈(동명이인 등으로 LLM이 무관 판정한 비율)가 높으면 더 구체적인 검색어로
    재시도한다: "{회사명}" → 기업 "{회사명}" → (대표명 있으면) "{회사명}" "{대표명}" →
    기업 "{회사명}" "{대표명}". ceo_name은 DART 기업개황 등에서 확보되면 넘겨주면 되고, 없으면
    처음 두 변형만 시도한다. 이 재시도는 채용 대상 회사 검색에만 쓴다 — 산업/직무 트렌드
    검색(collect_industry_trend/collect_job_trend)은 특정 회사를 짚는 게 아니라 노이즈 성격이
    달라 적용하지 않는다.

    on_raw_ready: _collect_issues 참고 — 필수 인자(의무 저장), 생략 불가.
    """
    issues: list[dict] = []
    for query in _company_query_variants(company_name, ceo_name):
        issues, noisy = await _collect_issues(query, "company", on_raw_ready, display_keyword=company_name)
        if not noisy:
            break
    return await _finalize_issues(issues)


async def collect_industry_trend(
    industry_keyword: str, on_raw_ready: Callable[[list[dict]], None]
) -> tuple[list[dict], list[dict]]:
    """산업/업종 관련 최근 트렌드·이슈 목록 (company_report가 사용).
    반환: (토픽별로 묶인 이슈 목록(event_taxonomy.py 기준, gist+출처+중요도), 원문 보존본(본문 포함, 저장 전용)).
    on_raw_ready: _collect_issues 참고 — 필수 인자(의무 저장), 생략 불가."""
    issues, _ = await _collect_issues(industry_keyword, "industry", on_raw_ready)
    return await _finalize_issues(issues)


async def collect_job_trend(
    job_title: str, on_raw_ready: Callable[[list[dict]], None]
) -> tuple[list[dict], list[dict]]:
    """직무 관련 최근 트렌드·이슈 목록 (fit이 사용 — company_report가 job_title을 받았을 때
    미리 수집해 job_trend로 캐시해두면 fit이 재수집 없이 가져다 쓴다).
    반환: (토픽별로 묶인 이슈 목록(event_taxonomy.py 기준, gist+출처+중요도), 원문 보존본(본문 포함, 저장 전용)).
    on_raw_ready: _collect_issues 참고 — 필수 인자(의무 저장), 생략 불가."""
    issues, _ = await _collect_issues(job_title, "job", on_raw_ready)
    return await _finalize_issues(issues)
