# checker.py
# 미자모 서평 검토기 - 링크 자동 검사 로직
# 2026-08-15 v2.0
#   - Playwright/로그인 세션 방식 폐기 -> requests 기반 정적 크롤링으로 전면 교체
#   - 로그인이 필요 없는 페이지만 자동 확인 가능 (네이버 카페처럼 로그인 필요한 곳은 "확인 필요"로 표시)
#   - 인스타그램은 여전히 자동 검토 대상에서 제외 -> 항상 "확인 필요"
#   - 글자수는 공백 제외 500자, 이미지는 2장 이상 기준
#   - 자바스크립트로 지연 로딩되는 이미지(lazy load)는 못 셀 수 있음 -> 대략적인 필터링 목적으로 사용

import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin

MIN_CHARS = 500
MIN_IMAGES = 2
LINK_COLUMNS = ["카페링크", "블로그or인스타링크", "서점링크1", "서점링크2"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

CONTENT_SELECTORS = [".se-main-container", "#postViewArea", ".article_container", ".post_ct"]


def parse_table(raw_text: str):
    """엑셀/구글시트에서 복사한 탭 구분 표 텍스트를 파싱한다."""
    lines = [line for line in raw_text.strip().splitlines() if line.strip()]
    if not lines:
        return []

    first_cells = lines[0].split("\t")
    if first_cells and "닉네임" in first_cells[0]:
        lines = lines[1:]

    participants = []
    for line in lines:
        cells = line.split("\t")
        cells = cells + [""] * (5 - len(cells))
        participants.append({
            "닉네임": cells[0].strip(),
            "카페링크": cells[1].strip(),
            "블로그or인스타링크": cells[2].strip(),
            "서점링크1": cells[3].strip(),
            "서점링크2": cells[4].strip(),
        })
    return participants


def classify_value(value: str):
    value = (value or "").strip()
    if not value:
        return "blank", value
    if value.startswith("http://") or value.startswith("https://"):
        return "url", value
    return "text", value


def is_instagram(url: str) -> bool:
    try:
        return "instagram.com" in urlparse(url).netloc.lower()
    except Exception:
        return False


def _count_chars_no_space(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _find_content_element(soup):
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el:
            return el
    return None


def _count_images(el):
    count = 0
    for img in el.find_all("img"):
        src = img.get("src") or img.get("data-lazy-src") or img.get("data-src") or ""
        if src and "blank.gif" not in src and "spacer" not in src:
            count += 1
    return count


def _fetch(url, session):
    resp = session.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
    resp.raise_for_status()
    return resp


def _check_naver_blog_or_cafe(url, session):
    """네이버 블로그/카페는 로그인이 필요할 수 있고, 블로그(PC버전)는 본문이
    iframe(mainFrame) 안에 있는 구조라 별도 처리가 필요하다.
    본문 요소를 못 찾으면 None을 반환해 needs_check로 처리하게 한다."""
    resp = _fetch(url, session)
    if "nid.naver.com" in resp.url:
        return None  # 로그인 페이지로 리다이렉트됨

    soup = BeautifulSoup(resp.text, "html.parser")
    el = _find_content_element(soup)

    if el is None:
        iframe = soup.select_one("#mainFrame")
        if iframe and iframe.get("src"):
            iframe_url = urljoin(resp.url, iframe["src"])
            resp2 = _fetch(iframe_url, session)
            if "nid.naver.com" in resp2.url:
                return None
            soup2 = BeautifulSoup(resp2.text, "html.parser")
            el = _find_content_element(soup2)

    if el is None:
        return None

    return el.get_text(), _count_images(el)


def _check_generic(url, session):
    """네이버 외 사이트(YES24/알라딘/교보 등)는 전용 선택자가 없어
    페이지 전체 텍스트/이미지로 대략 추정한다."""
    resp = _fetch(url, session)
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text()

    image_count = 0
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src:
            image_count += 1
    return text, image_count


def _check_url(url: str, session):
    if is_instagram(url):
        return {
            "status": "needs_check",
            "message": "인스타그램은 자동 검토 대상에서 제외했습니다. 직접 확인해주세요.",
        }

    try:
        host = urlparse(url).netloc.lower()
        is_naver_blog = "blog.naver.com" in host
        is_naver_cafe = "cafe.naver.com" in host

        if is_naver_blog or is_naver_cafe:
            result = _check_naver_blog_or_cafe(url, session)
            if result is None:
                site = "카페" if is_naver_cafe else "블로그"
                return {
                    "status": "needs_check",
                    "message": f"로그인이 필요하거나 본문을 찾지 못했습니다. 네이버 {site} 글은 직접 확인해주세요.",
                }
            text, image_count = result
        else:
            text, image_count = _check_generic(url, session)

    except Exception:
        return {
            "status": "needs_check",
            "message": "페이지를 자동으로 읽지 못했습니다. (접근 차단/오류 등)",
        }

    char_count = _count_chars_no_space(text)
    char_ok = char_count >= MIN_CHARS
    image_ok = image_count >= MIN_IMAGES

    if char_ok and image_ok:
        return {
            "status": "pass",
            "char_count": char_count,
            "image_count": image_count,
            "message": f"{char_count}자 / 이미지 {image_count}개",
        }

    reasons = []
    if not char_ok:
        reasons.append(f"{char_count}자 ({MIN_CHARS - char_count}자 부족)")
    if not image_ok:
        reasons.append(f"이미지 {image_count}개 ({MIN_IMAGES - image_count}장 부족)")
    return {
        "status": "insufficient",
        "char_count": char_count,
        "image_count": image_count,
        "message": " / ".join(reasons),
    }


def _check_cell(value: str, session):
    kind, val = classify_value(value)
    if kind == "blank":
        return {"status": "insufficient", "message": "링크가 입력되지 않았습니다."}
    if kind == "text":
        return {"status": "pass", "message": f"텍스트 기록으로 확인됨: {val}"}
    return _check_url(val, session)


def _final_status(cell_results):
    statuses = [r["status"] for r in cell_results.values()]
    if "insufficient" in statuses:
        return "부족"
    if "needs_check" in statuses:
        return "확인 필요"
    return "PASS"


def run_review(raw_text: str):
    participants = parse_table(raw_text)
    session = requests.Session()

    all_results = []
    for p in participants:
        cell_results = {}
        for col in LINK_COLUMNS:
            cell_results[col] = _check_cell(p[col], session)
        all_results.append({
            "닉네임": p["닉네임"],
            "final": _final_status(cell_results),
            "details": cell_results,
        })
    return all_results
