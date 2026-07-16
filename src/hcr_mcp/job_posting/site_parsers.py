"""채용 사이트 기업정보 페이지 파서 (HcR/scrapy/jobkoreaScrapy.py의
gamejob_company_info/jobkorea_company_info/super_company_info copy-adapt).

이 모듈이 다루는 채용 사이트 기업정보는 공고 URL이 있는 한 항상 확보 가능한 base data다
(DART/홈페이지/뉴스는 회사에 따라 없을 수 있다) — 다만 여기 포함된 CSS 셀렉터는 특정
사이트(잡코리아/게임잡) 마크업에 맞춰져 있어 사이트 구조가 바뀌거나 다른 사이트면 파싱이
실패할 수 있다. 그 경우를 위해 site_profile_collector.py가 URL 직접입력·스크린샷
비전추출 폴백을 둔다.

원본 차이: 파싱 실패(AttributeError, 사이트 구조 변경 등) 시 디버그용 HTML 파일을
디스크에 남기던 부분을 제거하고 조용히 부분/빈 결과를 반환.
"""

import re

from bs4 import BeautifulSoup


def _clean_text(text: str) -> str:
    return " ".join(text.split())


def gamejob_company_info(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {}

    header = {}
    corp_name = soup.select_one(".corpName")
    if corp_name:
        header["회사명"] = _clean_text(corp_name.get_text())
    status = soup.select_one(".corpHeader .now")
    if status:
        header["채용상태"] = _clean_text(status.get_text())
    result["기업정보"] = header

    company_info = {}
    for dl in soup.select(".corpInfo dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            company_info[_clean_text(dt.get_text())] = _clean_text(dd.get_text(" ", strip=True))
    result["기업상세정보"] = company_info

    intro_sections = {}
    for article in soup.select("#infoTab article.corpDesc"):
        title_tag = article.select_one("h4.corpTit")
        content_tag = article.select_one(".contArea")
        if not title_tag or not content_tag:
            continue
        title = _clean_text(title_tag.get_text()).replace("•", "").replace("ㆍ", "")
        intro_sections[title] = _clean_text(content_tag.get_text("\n"))
    result["소개"] = intro_sections

    news_list = []
    for li in soup.select("#newsTab .newslist li"):
        title, desc, date = li.select_one(".tit"), li.select_one(".desc"), li.select_one(".date")
        news_list.append(
            {
                "제목": _clean_text(title.get_text()) if title else None,
                "내용": _clean_text(desc.get_text()) if desc else None,
                "날짜": _clean_text(date.get_text()) if date else None,
            }
        )
    result["기업뉴스"] = news_list

    return result


def jobkorea_company_info(html: str) -> dict:
    result: dict = {
        "basic_info": {}, "financial": {}, "history": [], "employment": {}, "benefits": {}, "location": {},
        "raw_text": "",
    }

    soup = BeautifulSoup(html, "html.parser")
    soup = soup.select_one("div.company-body-infomation")
    if soup is None:
        return result

    # 회사마다 마크업이 조금씩 다르고(실측: 같은 "연혁" 섹션도 회사에 따라 아래 구조화 셀렉터가
    # 맞는 곳도, #devJKhistory처럼 <br>로만 구분된 텍스트 블록인 곳도 있음) CSS 셀렉터가 계속
    # site 개편을 따라가지 못할 수 있어, 구조화 파싱과 별개로 이 컨테이너 전체 텍스트도 원문
    # 그대로 함께 남긴다 — 구조화 필드가 놓친 내용이 있어도(예: 셀렉터가 안 맞는 섹션) LLM이
    # 이 raw_text로 보완할 수 있게. mutate 전(위 <br> 치환 등 영향 없게) 여기서 먼저 캡처.
    result["raw_text"] = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())

    basic_table = soup.select_one("table.table-basic-infomation-primary")
    if basic_table:
        for tr in basic_table.select("tr.field"):
            cells = tr.find_all(["th", "td"])
            i = 0
            while i < len(cells):
                if cells[i].name == "th":
                    key = cells[i].get_text(" ", strip=True)
                    if i + 1 < len(cells):
                        value_td = cells[i + 1]
                        values = [x.get_text(" ", strip=True) for x in value_td.select(".value, .salary-average-item, .reference")]
                        result["basic_info"][key] = " | ".join(values) if values else value_td.get_text(" ", strip=True)
                    i += 2
                else:
                    i += 1

    for card in soup.select(".financial-analysis-card"):
        title_tag = card.select_one(".headers .header")
        if not title_tag:
            continue
        title = title_tag.get_text(strip=True)
        value_tag = card.select_one(".revenue .value")
        result["financial"][title] = {"current_value": value_tag.get_text(strip=True) if value_tag else None}
        yearly = {}
        for bar in card.select(".chart .bar"):
            year_tag, val_tag = bar.select_one(".label"), bar.select_one(".value")
            if year_tag and val_tag:
                yearly[year_tag.get_text(strip=True)] = val_tag.get_text(strip=True)
        if yearly:
            result["financial"][title]["history"] = yearly

    current_year = None
    for item in soup.select(".corporate-history-list-item"):
        year_tag = item.select_one(".year")
        if year_tag:
            current_year = year_tag.get_text(strip=True)
        month_tag = item.select_one(".month")
        if month_tag:
            result["history"].append(
                {
                    "year": current_year,
                    "month": month_tag.get_text(strip=True),
                    "events": [x.get_text(strip=True) for x in item.select(".month-description")],
                }
            )

    # 위 .year/.month/.month-description 구조가 없는 회사는(실측: #devJKhistory) 연혁이
    # "2022년<br>- 항목<br>- 항목<br><br>2021년<br>..." 형태의 텍스트 블록 하나로만 들어있다.
    # 두 구조 다 위 루프처럼 각자 셀렉터가 없으면 그냥 빈 채로 넘어가므로 병행해도 중복되지 않음.
    history_block = soup.select_one("#devJKhistory")
    if history_block:
        for br in history_block.find_all("br"):
            br.replace_with("\n")
        current_year, events = None, []
        for line in (l.strip() for l in history_block.get_text().split("\n")):
            if not line:
                continue
            year_match = re.match(r"^(\d{4})년$", line)
            if year_match:
                if current_year and events:
                    result["history"].append({"year": current_year, "events": events})
                current_year, events = year_match.group(1), []
            else:
                events.append(line.lstrip("- ").strip())
        if current_year and events:
            result["history"].append({"year": current_year, "events": events})

    recruitments = []
    for row in soup.select(".table-in-progress-announcement tbody tr"):
        tds = row.find_all("td")
        if len(tds) >= 3:
            title_tag = row.select_one(".title")
            recruitments.append(
                {"period": tds[0].get_text(strip=True), "title": title_tag.get_text(strip=True) if title_tag else "", "details": tds[2].get_text(" | ", strip=True)}
            )
    result["employment"]["recruitments"] = recruitments

    for item in soup.select(".benefit-item-group .item"):
        category = item.select_one(".benefit-header")
        if not category:
            continue
        result["benefits"][category.get_text(strip=True)] = [p.get_text(strip=True) for p in item.select(".benefit-body p")]

    address_tag = soup.select_one(".working-environment-map .address")
    if address_tag:
        result["location"]["address"] = address_tag.get_text(strip=True)
    map_link = soup.select_one(".btnMapApiL")
    if map_link:
        result["location"]["map_url"] = map_link.get("href")

    return result


def super_company_info(html: str) -> dict:
    result: dict = {}
    soup = BeautifulSoup(html, "html.parser")
    soup = soup.select_one("#wrap")
    if soup is None:
        return result

    company_name = soup.select_one("h2.giHd")
    if company_name:
        result["company_name"] = company_name.get_text(strip=True)

    corp_info = soup.select(".corpInfo dl")
    if len(corp_info) >= 4:
        for key, dl in zip(("founded", "employee_count", "company_type", "revenue"), corp_info[:4]):
            strong = dl.select_one("strong")
            if strong:
                result[key] = strong.get_text(strip=True)

    result["reasons_to_join"] = [li.get_text(strip=True) for li in soup.select(".corpInfo2 ul li")]

    history = []
    for item in soup.select(".history_cont > li"):
        year_tag = item.find("p")
        history.append(
            {"year": year_tag.get_text(strip=True) if year_tag else "", "events": [x.get_text(" ", strip=True) for x in item.select("ul li")]}
        )
    result["history"] = history

    benefits = []
    for item in soup.select(".culture_area > div"):
        title, desc = item.select_one(".culture_txt p"), item.select_one(".culture_txt span")
        benefits.append({"title": title.get_text(strip=True) if title else "", "description": desc.get_text(" ", strip=True) if desc else ""})
    result["benefits"] = benefits

    talents = []
    for item in soup.select(".talent li"):
        title, desc = item.select_one("strong"), item.select_one("p")
        talents.append({"type": title.get_text(strip=True) if title else "", "description": desc.get_text(" ", strip=True) if desc else ""})
    result["talents"] = talents

    address_tag = soup.select_one(".corMap_info dl dd")
    if address_tag:
        result["address"] = address_tag.get_text(" ", strip=True)

    website_tag = soup.select_one(".corp_tel a")
    if website_tag:
        result["website"] = website_tag.get_text(strip=True)

    return result
