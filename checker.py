# checker.py
# 미자모 서평 검토기 - 링크 자동 검사 로직
# 2026-08-15 v3.0
#   - 용어 정리: PASS -> OKAY, 부족 -> CHECK, 확인 필요 -> 접속 불가
#   - 네이버 카페는 거의 항상 자동 접속이 차단되는 것으로 확인되어, 아예 시도하지 않고
#     바로 "접속 불가"로 표시 (억지로 읽으려다 잘못된 결과를 주지 않기 위함)
#   - YES24 사록(sarak.yes24.com)은 자바스크립트 렌더링 사이트라 계속 자동검토 제외
#   - 알라딘/교보 등 전용 선택자가 없는 사이트는, 본문으로 보이는 영역을 못 찾으면
#     페이지 전체로 대체하지 않고 "접속 불가"로 처리 (엉뚱한 영역까지 세어 숫자가
#     터무니없이 커지는 문제 방지)
#   - 글자수가 비정상적으로 적거나(30자 미만) 이미지가 비정상적으로 많으면(30개 초과)
#     제대로 못 읽은 것으로 보고 "접속 불가"로 재분류
#   - 서점링크2 뒤에 오는 선택 항목 "기타" 컬럼 지원 (있으면 같은 기준으로 판정에 포함,
#     없거나 빈칸이면 판정에서 제외)

import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin

MIN_CHARS = 500
MIN_IMAGES = 2
SUSPICIOUSLY_LOW_CHARS = 30      # 이보다 적으면 제대로 못 읽은 것으로 간주
SUSPICIOUSLY_HIGH_IMAGES = 30    # 이보다 많으면 페이지 전체를 잘못 센 것으로 간주

REQUIRED_COLUMNS = ["카페링크", "블로그or인스타링크", "서점링크1", "서점링크2"]
OPTIONAL_COLUMNS = ["기타"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

CONTENT_SELECTORS = [
    ".se-main-container", "#postViewArea", ".article_container", ".post_ct",
    ".view_content", "#Contents .contents_style", ".contents_style",
]

# 자바스크립트로 본문을 나중에 불러오는 방식(SPA)이라 정적 크롤링으로는
# 실제 리뷰 내용을 못 읽는 사이트 목록.
JS_RENDERED_DOMAINS = ["sarak.yes24.com"]


def parse_table(raw_text: str):
    """엑셀/구글시트에서 복사한 탭 구분 표 텍스트를 파싱한다.
    닉네임/카페링크/블로그or인스타링크/서점링크1/서점링크2 뒤에
    "기타" 컬럼이 있을 수도, 없을 수도 있다."""
    lines = [line for line in raw_text.strip().splitlines() if line.strip()]
    if not lines:
        return []

    first_cells = lines[0].split("\t")
    if first_cells and "닉네임" in first_cells[0]:
        lines = lines[1:]

    participants = []
    for line in lines:
        cells = line.split("\t")
        cells = cells + [""] * (5 - len(cells)) if len(cells) < 5 else cells
        etc_value = cells[5].strip() if len(cells) >= 6 else None
        participants.append({
            "닉네임": cells[0].strip(),
            "카페링크": cells[1].strip(),
            "블로그or인스타링크": cells[2].strip(),
            "서점링크1": cells[3].strip(),
            "서점링크2": cells[4].strip(),
            "기타": etc_value,  # None이면 컬럼 자체가 없던 것, ""면 있지만 빈칸
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


def is_naver_cafe(url: str) -> bool:
    try:
        return "cafe.naver.com" in urlparse(url).netloc.lower()
    except Exception:
        return False


def is_js_rendered_site(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(d in host for d in JS_RENDERED_DOMAINS)


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


def _check_naver_blog(url, session):
    """네이버 블로그는 본문이 iframe 안에 있는 구조가 많다.
    본문 요소를 못 찾으면 None을 반환해 접속 불가로 처리하게 한다."""
    resp = _fetch(url, session)
    if "nid.naver.com" in resp.url:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    el = _find_content_element(soup)

    if el is None:
        iframe = None
        for iframe_id in ("mainFrame", "cafe_main"):
            candidate = soup.select_one(f"#{iframe_id}")
            if candidate and candidate.get("src"):
                iframe = candidate
                break
        if iframe is None:
            candidate = soup.find("iframe")
            if candidate and candidate.get("src"):
                iframe = candidate

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
    """전용 선택자가 없는 사이트(알라딘/교보 등)는, 본문으로 보이는 영역을 찾았을 때만
    그 영역을 세고, 못 찾으면 페이지 전체로 대체하지 않고 None을 반환한다.
    (전체 페이지를 세면 메뉴/광고/관련글까지 다 잡혀 숫자가 터무니없이 커지는 문제 방지)"""
    resp = _fetch(url, session)
    soup = BeautifulSoup(resp.text, "html.parser")
    el = _find_content_element(soup)
    if el is None:
        return None
    return el.get_text(), _count_images(el)


def _check_url(url: str, session):
    if is_instagram(url):
        return {
            "status": "unreachable",
            "message": "인스타그램은 자동 검토 대상에서 제외했습니다. 직접 확인해주세요.",
        }

    if is_naver_cafe(url):
        return {
            "status": "unreachable",
            "message": "네이버 카페는 자동 접속이 거의 항상 차단되어 검토 대상에서 제외했습니다. 직접 확인해주세요.",
        }

    if is_js_rendered_site(url):
        return {
            "status": "unreachable",
            "message": "이 사이트는 자바스크립트로 본문을 불러오는 방식이라 자동 검토가 어렵습니다. 직접 확인해주세요.",
        }

    try:
        host = urlparse(url).netloc.lower()
        is_naver_blog = "blog.naver.com" in host

        if is_naver_blog:
            result = _check_naver_blog(url, session)
        else:
            result = _check_generic(url, session)

        if result is None:
            return {
                "status": "unreachable",
                "message": "페이지를 자동으로 읽지 못했습니다. 직접 확인해주세요.",
            }
        text, image_count = result

    except Exception:
        return {
            "status": "unreachable",
            "message": "접속이 되지 않았습니다. (접근 차단/오류 등)",
        }

    char_count = _count_chars_no_space(text)

    # 비정상적으로 적거나 많은 숫자는 제대로 못 읽은 것으로 보고 접속 불가로 재분류
    if char_count < SUSPICIOUSLY_LOW_CHARS or image_count > SUSPICIOUSLY_HIGH_IMAGES:
        return {
            "status": "unreachable",
            "message": f"읽은 결과가 비정상적입니다({char_count}자 / 이미지 {image_count}개). 직접 확인해주세요.",
        }

    char_ok = char_count >= MIN_CHARS
    image_ok = image_count >= MIN_IMAGES

    if char_ok and image_ok:
        return {
            "status": "okay",
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
        "status": "check",
        "char_count": char_count,
        "image_count": image_count,
        "message": " / ".join(reasons),
    }


def _check_cell(value: str, session):
    kind, val = classify_value(value)
    if kind == "blank":
        return {"status": "check", "message": "링크가 입력되지 않았습니다."}
    if kind == "text":
        return {"status": "okay", "message": f"텍스트 기록으로 확인됨: {val}"}
    return _check_url(val, session)


def _final_status(cell_results):
    statuses = [r["status"] for r in cell_results.values()]
    if "check" in statuses:
        return "CHECK"
    if "unreachable" in statuses:
        return "접속 불가"
    return "OKAY"


def run_review(raw_text: str):
    participants = parse_table(raw_text)
    session = requests.Session()

    all_results = []
    for p in participants:
        cell_results = {}
        for col in REQUIRED_COLUMNS:
            cell_results[col] = _check_cell(p[col], session)

        # 기타는 컬럼 자체가 없거나(None) 빈칸이면 판정에서 제외, 내용이 있으면 포함
        etc_value = p.get("기타")
        if etc_value:
            cell_results["기타"] = _check_cell(etc_value, session)

        all_results.append({
            "닉네임": p["닉네임"],
            "final": _final_status(cell_results),
            "details": cell_results,
        })
    return all_results
