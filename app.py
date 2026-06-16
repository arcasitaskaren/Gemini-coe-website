# -*- coding: utf-8 -*-
import os
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, ".env"))

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, make_response
import json
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from config import Config
from models import db, ContentSection, Card, NavigationLink, FooterSection, Admin, SuggestedProfessional, Page, PageBlock, CrawledPage, NewsArticle, TrainingProgram
from werkzeug.utils import secure_filename
from datetime import datetime, timezone   # ? SINGLE import with both datetime AND timezone
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import hashlib
import re
import time
import secrets
import string
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse as _urlparse

# GA4 Analytics - graceful fallback
try:
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric, OrderBy
    )
    from google.oauth2 import service_account as ga_service_account
    GA4_AVAILABLE = True
except ImportError:
    GA4_AVAILABLE = False
    print("[GA4] google-analytics-data not installed.")

app = Flask(__name__)
app.jinja_env.filters['fromjson'] = json.loads
app.config.from_object(Config)

IS_PRODUCTION = os.environ.get('FLASK_ENV', 'production') == 'production'

GOV_IMAGE_BASE = (
    os.environ.get('GOV_IMAGE_BASE')
    or os.environ.get('UPLOAD_URL')
    or os.environ.get('STATIC_BASE_URL')
    or 'http://coe-psp.dap.edu.ph/static/images'
)

GA4_PROPERTY_ID      = os.environ.get('GA4_PROPERTY_ID', '')
GA4_CREDENTIALS_FILE = os.environ.get('GA4_CREDENTIALS_FILE', 'ga4-credentials.json')

print(f"\n[DEBUG] ANTHROPIC_API_KEY: {'LOADED' if os.getenv('ANTHROPIC_API_KEY') else 'NOT FOUND'}")
print(f"[DEBUG] IS_PRODUCTION: {IS_PRODUCTION}")
print(f"[DEBUG] GOV_IMAGE_BASE: {GOV_IMAGE_BASE}")
print(f"[DEBUG] GA4_AVAILABLE: {GA4_AVAILABLE}")
print(f"[DEBUG] GA4_PROPERTY_ID: {'SET' if GA4_PROPERTY_ID else 'NOT SET'}")
print()

db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'admin_login'


@app.context_processor
def inject_globals():
    return dict(GOV_IMAGE_BASE=GOV_IMAGE_BASE)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Admin, int(user_id))


@app.template_filter('fromjson')
def fromjson_filter(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return []


@app.template_filter('images_json')
def images_json_filter(value):
    if not value:
        return '[]'
    raw_list = value if isinstance(value, list) else (
        json.loads(value) if isinstance(value, str) else []
    )
    clean = []
    for item in raw_list:
        if isinstance(item, dict):
            src     = (item.get('src') or item.get('filename') or '').strip()
            caption = (item.get('caption') or '').strip()
            if src:
                clean.append({'src': src, 'caption': caption})
        elif isinstance(item, str) and item.strip():
            clean.append({'src': item.strip(), 'caption': ''})
    return json.dumps(clean)


os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def escape_html(text):
    if not text:
        return text
    return (str(text)
            .replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;')
            .replace("'", '&#39;'))


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


def get_content(key, default=''):
    c = db.session.query(ContentSection).filter_by(content_key=key).first()
    return c.content_value if c else default


def get_all_content():
    return db.session.query(ContentSection).order_by(ContentSection.section_order).all()


def get_all_cards():
    return db.session.query(Card).order_by(Card.card_order).all()


def get_all_nav_links():
    return db.session.query(NavigationLink).order_by(NavigationLink.link_order).all()


def get_all_professionals():
    return db.session.query(SuggestedProfessional).order_by(SuggestedProfessional.professional_order).all()


def clear_ai_cache():
    cache_dir = os.path.join(app.root_path, 'cache')
    if os.path.exists(cache_dir):
        for f in os.listdir(cache_dir):
            if f.startswith('ai_') and f.endswith('.json'):
                try:
                    os.remove(os.path.join(cache_dir, f))
                except OSError:
                    pass


def no_cache_json(data, status=200):
    resp = make_response(jsonify(data), status)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma']        = 'no-cache'
    resp.headers['Expires']       = '0'
    return resp


def is_greeting(query):
    greetings = [
        'hello', 'hi', 'hey', 'greetings', 'good morning', 'good afternoon',
        'good evening', 'howdy', 'hola', 'bonjour', 'kamusta', 'kumusta',
        'what are you', 'who are you', 'introduce yourself',
    ]
    q = query.lower().strip()
    return any(q == g or q.startswith(g + ' ') or q.startswith(g + '!') for g in greetings)


# ---------------------------------------------------------------------------
#  URL fetcher + deep crawler
# ---------------------------------------------------------------------------

_url_cache     = {}
_URL_CACHE_TTL = 21600  # 6 hours

_SKIP_EXTENSIONS = {
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.zip', '.doc', '.docx', '.xls', '.xlsx', '.csv',
    '.mp4', '.mp3', '.avi', '.mov', '.svg', '.ico',
    '.css', '.js', '.json', '.xml', '.txt', '.map',
    '.woff', '.woff2', '.ttf', '.eot', '.otf',
    '.rss', '.atom', '.yaml', '.yml', '.toml',
}

_SKIP_URL_SEGMENTS = {
    '/wp-content/', '/wp-includes/', '/wp-json/',
    '/wp-admin/', '/feed/', '/xmlrpc',
    '/css/', '/js/', '/fonts/', '/font/',
    '/assets/css/', '/assets/js/',
    '/static/css/', '/static/js/',
}

_FETCH_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate',
    'Connection':      'keep-alive',
}

# Deep-crawl settings
MAX_CRAWL_DEPTH    = 3
MAX_PAGES_PER_SEED = 25
CRAWL_DELAY_SECS   = 0.4

# Periodic re-crawl interval
_RECRAWL_INTERVAL_HOURS = 6
_recrawl_timer = None


def _html_to_text(html: str) -> str:
    clean = re.sub(r'<style[^>]*>.*?</style>',   ' ', html,  flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<script[^>]*>.*?</script>',  ' ', clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<nav[^>]*>.*?</nav>',        ' ', clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<footer[^>]*>.*?</footer>',  ' ', clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<!--.*?-->',                 ' ', clean, flags=re.DOTALL)
    text  = re.sub(r'<[^>]+>', ' ', clean)
    return re.sub(r'\s+', ' ', text).strip()


def _extract_page_title(html: str) -> str:
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    return re.sub(r'\s+', ' ', m.group(1)).strip()[:200] if m else ''


def _extract_links_from_html(html: str, base_url: str) -> list:
    base_parsed  = _urlparse(base_url)
    same_origin  = []
    cross_origin = []

    for match in re.finditer(r'href=["\']([^"\'#][^"\']*)["\']', html, re.IGNORECASE):
        href = match.group(1).strip()
        if not href or href.startswith(('mailto:', 'javascript:', 'tel:', '#')):
            continue
        abs_url = urljoin(base_url, href)
        parsed  = _urlparse(abs_url)
        if parsed.scheme not in ('http', 'https'):
            continue
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in _SKIP_EXTENSIONS):
            continue
        # Also skip by path segment (catches ?ver= query string variants)
        abs_lower = abs_url.lower()
        if any(seg in abs_lower for seg in _SKIP_URL_SEGMENTS):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            clean += f"?{parsed.query}"
        if parsed.netloc == base_parsed.netloc:
            same_origin.append(clean)
        else:
            cross_origin.append(clean)

    seen, result = set(), []
    for u in same_origin + cross_origin:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def _fetch_single_url(url: str, timeout: int = 4) -> str:
    if not url or url in ('#', ''):
        return ''
    cached = _url_cache.get(url)
    if cached and (time.time() - cached.get('ts', 0) < _URL_CACHE_TTL):
        return cached.get('text', '')
    try:
        resp = requests.get(
            url, headers=_FETCH_HEADERS, timeout=timeout,
            allow_redirects=True, verify=False
        )
        if resp.status_code != 200:
            print(f"[fetch_url] HTTP {resp.status_code} for {url}")
            return ''
        html = resp.text
        text = _html_to_text(html)[:4000]
        _url_cache[url] = {'text': text, 'ts': time.time(), 'raw_html': html}
        return text
    except Exception as e:
        print(f"[fetch_url] Error fetching {url}: {e}")
        return ''


def fetch_url_content(url: str, deep: bool = True, max_subpages: int = 20) -> str:
    if not url or url in ('#', ''):
        return ''

    cache_key = f"__deep__{url}" if deep else url
    cached = _url_cache.get(cache_key)
    if cached and (time.time() - cached.get('ts', 0) < _URL_CACHE_TTL):
        return cached.get('text', '')

    root_text = _fetch_single_url(url)
    if not deep or not root_text:
        return root_text

    raw_html = _url_cache.get(url, {}).get('raw_html', '')
    if not raw_html:
        _url_cache[cache_key] = {'text': root_text, 'ts': time.time()}
        return root_text

    sub_links = _extract_links_from_html(raw_html, url)[:max_subpages]
    if not sub_links:
        _url_cache[cache_key] = {'text': root_text, 'ts': time.time()}
        return root_text

    sub_texts = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(_fetch_single_url, u, 6): u for u in sub_links}
        for future in as_completed(future_map, timeout=25):
            sub_url = future_map[future]
            try:
                t = future.result()
                if t:
                    sub_texts.append(f"\n[Sub-page: {sub_url}]\n{t[:2000]}")
            except Exception:
                pass

    combined = (root_text + ''.join(sub_texts))[:12000]
    _url_cache[cache_key] = {'text': combined, 'ts': time.time()}
    return combined


# ---------------------------------------------------------------------------
#  Persistent deep crawler (stores results in CrawledPage DB table)
# ---------------------------------------------------------------------------

def crawl_url_deep(seed_url: str, nav_link_id: int = None) -> int:
    if not seed_url or seed_url in ('#', ''):
        return 0

    parsed_seed = _urlparse(seed_url)
    root_domain = parsed_seed.netloc
    if not root_domain:
        return 0

    with app.app_context():
        existing = {
            r.page_url for r in
            db.session.query(CrawledPage.page_url)
            .filter_by(source_url=seed_url).all()
        }

    queue         = [(seed_url, 0)]
    visited       = set(existing)
    visited.add(seed_url.rstrip('/'))
    newly_crawled = 0

    while queue and newly_crawled < MAX_PAGES_PER_SEED:
        current_url, depth = queue.pop(0)
        if depth > MAX_CRAWL_DEPTH:
            continue
        try:
            print(f"[crawler] depth={depth} crawling: {current_url}")
            resp = requests.get(current_url, headers=_FETCH_HEADERS, timeout=10,
                                allow_redirects=True, verify=False)
            if resp.status_code != 200:
                continue

            html        = resp.text
            text        = _html_to_text(html)[:6000]
            title       = _extract_page_title(html)
            child_links = _extract_links_from_html(html, current_url)

            with app.app_context():
                existing_page = db.session.query(CrawledPage).filter_by(page_url=current_url).first()
                if existing_page:
                    existing_page.text_content = text
                    existing_page.page_title   = title
                    existing_page.crawled_at   = datetime.utcnow()
                else:
                    db.session.add(CrawledPage(
                        source_url   = seed_url,
                        page_url     = current_url,
                        page_title   = title,
                        text_content = text,
                        nav_link_id  = nav_link_id,
                        depth        = depth,
                    ))
                db.session.commit()
                newly_crawled += 1

            for link in child_links:
                norm = link.rstrip('/')
                if norm not in visited and newly_crawled + len(queue) < MAX_PAGES_PER_SEED * 2:
                    visited.add(norm)
                    queue.append((link, depth + 1))

            time.sleep(CRAWL_DELAY_SECS)

        except Exception as e:
            print(f"[crawler] Error on {current_url}: {e}")
            continue

    print(f"[crawler] Finished seed={seed_url}, crawled {newly_crawled} pages")
    return newly_crawled


def crawl_all_nav_links() -> int:
    """Crawl every NavigationLink URL and every Card button URL."""
    with app.app_context():
        nav_links = db.session.query(NavigationLink).filter(
            NavigationLink.link_url.isnot(None),
            NavigationLink.link_url != '#',
            NavigationLink.link_url != '',
        ).all()

        card_urls = []
        for card in db.session.query(Card).all():
            for url in _safe_json_dict(card.button_links).values():
                if url and url not in ('#', ''):
                    card_urls.append((url, None))

        total = 0
        for nav in nav_links:
            print(f"[crawler] Starting deep crawl for nav '{nav.link_text}' -> {nav.link_url}")
            total += crawl_url_deep(nav.link_url, nav_link_id=nav.id)

        seen = set()
        for url, _ in card_urls:
            if url not in seen:
                seen.add(url)
                print(f"[crawler] Card button url -> {url}")
                total += crawl_url_deep(url)

        print(f"[crawler] Total pages crawled: {total}")
        return total


def _schedule_recrawl():
    """Schedule a periodic background re-crawl every _RECRAWL_INTERVAL_HOURS hours."""
    global _recrawl_timer

    def _run():
        print(f"[crawler] Scheduled re-crawl starting...")
        with app.app_context():
            crawl_all_nav_links()
        _schedule_recrawl()

    _recrawl_timer = threading.Timer(_RECRAWL_INTERVAL_HOURS * 3600, _run)
    _recrawl_timer.daemon = True
    _recrawl_timer.start()
    print(f"[crawler] Next re-crawl scheduled in {_RECRAWL_INTERVAL_HOURS}h")


# ---------------------------------------------------------------------------
#  Search helpers
# ---------------------------------------------------------------------------

SYNONYMS = {
    'training':      ['capacity development', 'learning', 'course', 'seminar', 'workshop', 'program'],
    'course':        ['training', 'program', 'seminar', 'workshop'],
    'program':       ['training', 'course', 'initiative', 'project'],
    'new':           ['latest', 'recent', 'update', 'news', 'announcement'],
    'govlab':        ['governance lab', 'innovation lab', 'lab'],
    'challenge':     ['competition', 'contest', 'submit', 'entry'],
    'productivity':  ['efficiency', 'performance', 'output'],
    'public sector': ['government', 'agency', 'bureau', 'office'],
    'join':          ['register', 'enroll', 'membership', 'participate'],
    'about':         ['overview', 'mission', 'vision', 'background'],
    'community':     ['network', 'group', 'members', 'professionals'],
    'knowledge':     ['research', 'publication', 'study', 'resource', 'learning'],
    'digital':       ['technology', 'innovation', 'paperless', 'e-government'],
    'paperless':     ['digital', 'paper-less', 'e-document', 'technology'],
    'nextgen':       ['next generation', 'youth', 'leaders', 'future'],
    'enroll':        ['register', 'join', 'sign up', 'apply'],
    'conference':    ['summit', 'forum', 'congress', 'event', 'symposium'],
    'innovation':    ['digital', 'technology', 'improvement', 'reform'],
    'dap':           ['development academy', 'development academy of the philippines'],
    'coe':           ['center of excellence', 'excellence center'],
    'seminar':       ['training', 'workshop', 'webinar', 'session'],
    'toolkit':       ['tools', 'guide', 'resources', 'framework', 'manual', 'kit'],
    'framework':     ['structure', 'model', 'approach', 'methodology', 'system'],
    'assessment':    ['evaluation', 'review', 'audit', 'appraisal', 'analysis'],
    'moneywise':     ['financial literacy', 'money management', 'personal finance', 'budget', 'savings'],
    'financial':     ['money', 'finance', 'fiscal', 'budget', 'funding', 'moneywise'],
    'money':         ['financial', 'budget', 'savings', 'funds', 'fiscal', 'moneywise'],
    'budget':        ['financial planning', 'funds', 'allocation', 'money management'],
    'literacy':      ['education', 'awareness', 'learning', 'knowledge', 'skills'],
    'winner':        ['grand winner', 'champion', 'awardee', 'winner', 'first place', 'top'],
    'fastbreak':     ['fast break', 'productivity challenge', 'challenge', 'competition'],
    'award':         ['winner', 'awardee', 'recognition', 'prize', 'honor'],
    'result':        ['outcome', 'winner', 'result', 'announcement', 'awardee'],
}

STOPWORDS = {
    'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'need', 'dare', 'ought',
    'used', 'i', 'me', 'my', 'we', 'our', 'you', 'your', 'he', 'she',
    'it', 'its', 'they', 'their', 'what', 'which', 'who', 'whom', 'this',
    'that', 'these', 'those', 'for', 'of', 'in', 'on', 'at', 'to', 'by',
    'from', 'with', 'about', 'into', 'through', 'during', 'before', 'after',
    'how', 'when', 'where', 'why', 'any', 'all', 'both', 'each', 'more',
    'most', 'other', 'some', 'such', 'than', 'too', 'very', 'just', 'and',
    'but', 'or', 'nor', 'so', 'yet', 'not', 'no',
}


def _tokenise(text: str) -> list:
    words  = re.findall(r"[a-zA-Z0-9']+", text.lower())
    result = []
    for w in words:
        if w in STOPWORDS:
            continue
        if w.isdigit() or len(w) > 1:
            result.append(w)
    return result


def _expand_keywords(tokens: list) -> list:
    expanded = list(tokens)
    for t in tokens:
        for key, syns in SYNONYMS.items():
            if t == key or t in syns:
                expanded.extend([key] + syns)
    seen, result = set(), []
    for w in expanded:
        if w not in seen:
            seen.add(w)
            result.append(w)
    return result


def _score_text(tokens: list, expanded: list, haystack: str) -> float:
    if not haystack:
        return 0.0
    hay   = haystack.lower()
    score = 0.0
    for t in tokens:
        if re.search(rf'\b{re.escape(t)}\b', hay):
            score += 2.0
        elif t in hay:
            score += 0.5
    original_set = set(tokens)
    for syn in expanded:
        if syn not in original_set:
            if re.search(rf'\b{re.escape(syn)}\b', hay):
                score += 1.0
            elif syn in hay:
                score += 0.3
    max_possible = max(len(tokens) * 2.0, 1.0)
    return min(score / max_possible, 1.5)


def _extract_snippet(haystack: str, tokens: list, window: int = 200) -> str:
    if not haystack:
        return ''
    hay_lower = haystack.lower()
    best_pos  = -1
    for t in tokens:
        pos = hay_lower.find(t)
        if pos != -1:
            best_pos = pos
            break
    if best_pos == -1:
        return haystack[:window] + ('...' if len(haystack) > window else '')
    start   = max(0, best_pos - 60)
    end     = min(len(haystack), best_pos + window)
    snippet = haystack[start:end].strip()
    return ('...' if start > 0 else '') + snippet + ('...' if end < len(haystack) else '')


def _safe_json_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _safe_json_dict(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _image_url(filename: str) -> str:
    if not filename:
        return ''
    filename = filename.strip()
    if filename.startswith('http://') or filename.startswith('https://'):
        return filename
    bare = re.sub(r'^(static/images/|images/|static/)', '', filename)
    return f'{GOV_IMAGE_BASE}/{bare}'


def _strip_html(html_text: str) -> str:
    if not html_text:
        return ''
    text = re.sub(r'<[^>]+>', ' ', html_text)
    return re.sub(r'\s+', ' ', text).strip()


def _title_contains_any_token(tokens: list, title: str) -> bool:
    if not title or not tokens:
        return False
    title_lower = title.lower()
    return any(t in title_lower for t in tokens if len(t) >= 4)


def _check_exact_match(query: str, text: str) -> bool:
    if not query or not text:
        return False
    q   = query.strip().lower()
    hay = text.lower()
    if re.search(rf'\b{re.escape(q)}\b', hay):
        return True
    for tok in _tokenise(q):
        if len(tok) >= 4 and re.search(rf'\b{re.escape(tok)}\b', hay):
            return True
    return False


# ---------------------------------------------------------------------------
#  FULL DATABASE SEARCH
# ---------------------------------------------------------------------------

def search_website_content(query: str) -> dict:
    results = {
        'suggestions':   [],
        'images':        [],
        'related_links': [],
        'context_text':  '',
        'exact_match':   False,
        'exact_term':    '',
        'exact_context': '',
    }

    if not query or not query.strip():
        return results

    tokens   = _tokenise(query)
    if not tokens:
        return results
    expanded = _expand_keywords(tokens)
    matches  = []

    try:
        all_cards   = db.session.query(Card).all()
        all_navs    = db.session.query(NavigationLink).all()
        all_cs      = db.session.query(ContentSection).all()
        all_pages   = db.session.query(Page).filter_by(is_published=True).all()
        all_crawled = db.session.query(CrawledPage).all()

        # -- Index crawled pages by source URL for fast lookup ------------
        crawled_by_source = {}
        for cp in all_crawled:
            crawled_by_source.setdefault(cp.source_url, []).append(cp)

        # Track all crawled page URLs to avoid redundant live fetches
        crawled_urls = {cp.page_url for cp in all_crawled}

        # -- Collect URLs not yet in DB (still worth live-fetching) -------
        nav_urls = list({
            nav.link_url for nav in all_navs
            if nav.link_url and nav.link_url not in ('#', '', None)
            and nav.link_url not in crawled_urls
        })
        card_button_urls = list({
            url
            for card in all_cards
            for url in _safe_json_dict(card.button_links).values()
            if url and url not in ('#', '', None)
            and url not in crawled_urls
        })
        all_urls_to_fetch = list(set(nav_urls + card_button_urls))

        url_results = {}
        if all_urls_to_fetch:
            with ThreadPoolExecutor(max_workers=10) as executor:
                future_map = {
                    executor.submit(fetch_url_content, u, True, 20): u
                    for u in all_urls_to_fetch
                }
                for future in as_completed(future_map, timeout=4):
                    url = future_map[future]
                    try:
                        url_results[url] = future.result()
                    except Exception:
                        url_results[url] = ''
            fetched_ok = sum(1 for v in url_results.values() if v)
            print(f"[search] Live-fetched {fetched_ok}/{len(all_urls_to_fetch)} uncrawled URLs")

        # -- Helper: get merged crawled subpage text for a seed URL -------
        def _crawled_text_for(seed_url: str) -> str:
            pages = crawled_by_source.get(seed_url, [])
            if not pages:
                return ''
            pages = sorted(pages, key=lambda p: (p.depth, -p.crawled_at.timestamp()))
            parts = []
            for p in pages[:15]:
                if p.text_content:
                    parts.append(f"[{p.page_title or p.page_url}]\n{p.text_content[:1500]}")
            return '\n\n'.join(parts)[:8000]

        # -- Score Cards --------------------------------------------------
        for card in all_cards:
            btn_contents = _safe_json_dict(card.button_contents)
            btn_images   = _safe_json_dict(card.button_images)
            btn_links    = _safe_json_dict(card.button_links)
            buttons_list = _safe_json_list(card.buttons)
            content_blob = ' '.join(str(v) for v in btn_contents.values())

            title_score   = _score_text(tokens, expanded, card.title or '')
            content_score = _score_text(tokens, expanded, content_blob)
            overall       = max(title_score, content_score * 0.85)
            if _title_contains_any_token(tokens, card.title or ''):
                overall = max(overall, 0.8)

            card_subpage_texts = []
            card_links         = []

            for btn_idx, link_url in btn_links.items():
                if not link_url:
                    continue
                try:
                    label = buttons_list[int(btn_idx)]
                except (ValueError, IndexError):
                    label = 'View'
                card_links.append({'title': label, 'url': link_url, 'context': card.title})

                # Prefer DB-crawled data; fall back to live fetch
                crawled_sub = _crawled_text_for(link_url)
                if crawled_sub:
                    card_subpage_texts.append(f"[Button: {label} | {link_url}]\n{crawled_sub[:2500]}")
                else:
                    live_text = url_results.get(link_url, '')
                    if live_text:
                        card_subpage_texts.append(f"[Button: {label}]\n{live_text[:2000]}")

            subpage_blob = '\n'.join(card_subpage_texts)
            if subpage_blob:
                subpage_score = _score_text(tokens, expanded, subpage_blob)
                overall       = max(overall, subpage_score)

            if overall < 0.08:
                continue

            is_exact = _check_exact_match(
                query,
                (card.title or '') + ' ' + content_blob + ' ' + subpage_blob
            )
            snippet = _extract_snippet(
                subpage_blob if (subpage_blob and _score_text(tokens, expanded, subpage_blob) > content_score)
                else (content_blob or card.title or ''),
                tokens
            )

            card_images = []
            if card.image:
                card_images.append({'url': _image_url(card.image), 'alt_text': card.title, 'source': card.title})
            for btn_idx, btn_img_raw in btn_images.items():
                if isinstance(btn_img_raw, list):
                    for img_entry in btn_img_raw:
                        fname   = (img_entry.get('src', '') or img_entry.get('filename', '')) if isinstance(img_entry, dict) else str(img_entry)
                        caption = (img_entry.get('caption', '') if isinstance(img_entry, dict) else '') or card.title
                        if fname:
                            card_images.append({'url': _image_url(fname), 'alt_text': caption, 'source': card.title})
                elif isinstance(btn_img_raw, str) and btn_img_raw:
                    card_images.append({'url': _image_url(btn_img_raw), 'alt_text': card.title, 'source': card.title})

            full_text = f"{card.title}\n{content_blob}\n{subpage_blob}"
            matches.append({
                'score':        overall,
                'record_type':  'Card',
                'record_id':    card.id,
                'record_title': card.title,
                'snippet':      snippet,
                'full_text':    full_text[:6000],
                'images':       card_images,
                'links':        card_links,
                'is_exact':     is_exact,
            })

        # -- Score Navigation Links ---------------------------------------
        for nav in all_navs:
            title_score   = _score_text(tokens, expanded, nav.link_text or '')
            content_score = _score_text(tokens, expanded, nav.page_content or '')

            # Combine DB-crawled text + live fetch text
            live_text   = url_results.get(nav.link_url or '', '')
            crawled_sub = _crawled_text_for(nav.link_url or '') if nav.link_url else ''
            combined_ext = '\n\n'.join(filter(None, [live_text, crawled_sub]))[:8000]

            url_score = _score_text(tokens, expanded, combined_ext) if combined_ext else 0.0
            overall   = max(title_score, content_score * 0.85, url_score)

            if _title_contains_any_token(tokens, nav.link_text or ''):
                overall = max(overall, 0.8)
            if overall < 0.05:
                continue

            combined_text = (nav.page_content or '') + ('\n' + combined_ext if combined_ext else '')
            is_exact      = _check_exact_match(query, combined_text + ' ' + (nav.link_text or ''))

            best_ext  = crawled_sub or live_text
            best_text = best_ext if (best_ext and url_score >= content_score * 0.85) else (nav.page_content or best_ext or '')
            snippet   = _extract_snippet(best_text or nav.link_text or '', tokens)

            combined_context = (
                (nav.page_content or '')
                + ('\n\n[From linked pages & subpages]\n' + combined_ext if combined_ext else '')
            )[:8000]

            nav_images = []
            for img_entry in _safe_json_list(nav.images):
                if isinstance(img_entry, dict):
                    fname   = img_entry.get('src', '') or img_entry.get('filename', '')
                    caption = img_entry.get('caption', nav.link_text)
                elif isinstance(img_entry, str):
                    fname, caption = img_entry, nav.link_text
                else:
                    continue
                if fname:
                    nav_images.append({'url': _image_url(fname), 'alt_text': caption, 'source': nav.link_text})

            nav_links_out = []
            if nav.link_url and nav.link_url not in ('#', ''):
                nav_links_out.append({'title': nav.link_text, 'url': nav.link_url, 'context': nav.link_text})
                # Surface crawled subpages as individual links
                for cp in crawled_by_source.get(nav.link_url, [])[:5]:
                    if cp.page_url != nav.link_url:
                        nav_links_out.append({
                            'title':   cp.page_title or cp.page_url,
                            'url':     cp.page_url,
                            'context': f"Subpage of {nav.link_text}",
                        })
            else:
                nav_links_out.append({'title': nav.link_text, 'url': f'/nav-page/{nav.id}', 'context': nav.link_text})

            for url in re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', nav.page_content or ''):
                nav_links_out.append({
                    'title':   url.replace('https://', '').replace('http://', '').split('/')[0],
                    'url':     url,
                    'context': nav.link_text,
                })

            matches.append({
                'score':        overall,
                'record_type':  'NavigationLink',
                'record_id':    nav.id,
                'record_title': nav.link_text,
                'snippet':      snippet,
                'full_text':    combined_context[:8000],
                'images':       nav_images,
                'links':        nav_links_out,
                'context_text': combined_context[:5000],
                'is_exact':     is_exact,
            })

        # -- Score Content Sections ---------------------------------------
        for cs in all_cs:
            plain = _strip_html(cs.content_value or '')
            score = _score_text(tokens, expanded, plain)
            if score < 0.2:
                continue
            is_exact = _check_exact_match(query, plain + ' ' + (cs.content_key or ''))
            matches.append({
                'score':        score,
                'record_type':  'ContentSection',
                'record_id':    cs.id,
                'record_title': cs.content_key.replace('_', ' ').title(),
                'snippet':      _extract_snippet(plain, tokens),
                'full_text':    plain[:2000],
                'images':       [],
                'links':        [],
                'is_exact':     is_exact,
            })

        # -- Score Published Pages ----------------------------------------
        for page in all_pages:
            blocks      = page.get_blocks_ordered()
            page_texts  = [page.title or '', page.description or '']
            page_links  = []
            page_images = []

            for block in blocks:
                block_text = ' '.join(filter(None, [
                    _strip_html(block.content or ''),
                    block.heading or '',
                    block.subheading or '',
                    block.card_title or '',
                    _strip_html(block.card_description or ''),
                    block.image_caption or '',
                ]))
                if block_text.strip():
                    page_texts.append(block_text)
                if block.image_url:
                    page_images.append({'url': _image_url(block.image_url), 'alt_text': block.image_alt_text or page.title, 'source': page.title})
                if block.card_image:
                    page_images.append({'url': _image_url(block.card_image), 'alt_text': block.card_title or page.title, 'source': page.title})
                for btn in _safe_json_list(block.card_buttons):
                    if isinstance(btn, dict) and btn.get('url'):
                        page_links.append({'title': btn.get('text', 'View'), 'url': btn['url'], 'context': page.title})

            full_page_text = '\n'.join(page_texts)
            overall = max(
                _score_text(tokens, expanded, page.title or ''),
                _score_text(tokens, expanded, page.description or '') * 0.9,
                _score_text(tokens, expanded, full_page_text) * 0.95,
            )
            if overall < 0.1:
                continue

            is_exact = _check_exact_match(query, full_page_text)
            page_links.insert(0, {'title': page.title, 'url': f'/page/{page.slug}', 'context': page.title})

            matches.append({
                'score':        overall,
                'record_type':  'Page',
                'record_id':    page.id,
                'record_title': page.title,
                'snippet':      _extract_snippet(full_page_text, tokens),
                'full_text':    full_page_text[:3000],
                'images':       page_images,
                'links':        page_links,
                'is_exact':     is_exact,
            })

        # -- Score CrawledPage records (first-class, no de-boost) ---------
        for cp in all_crawled:
            text_score  = _score_text(tokens, expanded, cp.text_content or '')
            title_score = _score_text(tokens, expanded, cp.page_title or '')
            overall     = max(text_score, title_score * 1.2)
            if overall < 0.12:
                continue
            is_exact = _check_exact_match(query, (cp.text_content or '') + ' ' + (cp.page_title or ''))
            # Strong boost for exact match in a deep subpage
            if is_exact:
                overall = max(overall, 0.9)
            matches.append({
                'score':        overall,
                'record_type':  'CrawledPage',
                'record_id':    cp.id,
                'record_title': cp.page_title or cp.page_url,
                'snippet':      _extract_snippet(cp.text_content or cp.page_title or '', tokens),
                'full_text':    (cp.text_content or '')[:4000],
                'images':       [],
                'links':        [{
                    'title':   cp.page_title or cp.page_url,
                    'url':     cp.page_url,
                    'context': f"Subpage (depth {cp.depth}) of {cp.source_url}",
                }],
                'context_text': (cp.text_content or '')[:800],
                'is_exact':     is_exact,
            })

        # -- Sort and build final result ----------------------------------
        matches.sort(key=lambda x: (x.get('is_exact', False), x['score']), reverse=True)

        seen_images, seen_links = set(), set()
        context_parts           = []

        exact_matches = [m for m in matches if m.get('is_exact')]
        if exact_matches:
            results['exact_match']   = True
            results['exact_term']    = query.strip()
            results['exact_context'] = exact_matches[0].get('full_text', '')[:6000]

       # Extensions that should never appear as related links
        _BAD_LINK_EXTS = {
            '.css', '.js', '.json', '.xml', '.txt', '.map',
            '.woff', '.woff2', '.ttf', '.eot', '.otf',
            '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp',
            '.zip', '.doc', '.docx', '.xls', '.xlsx', '.csv',
            '.mp4', '.mp3', '.avi', '.mov', '.svg', '.ico',
            '.rss', '.atom', '.yaml', '.yml',
        }

        def _is_good_link(url: str) -> bool:
            if not url or url in ('#', ''):
                return False
            parsed = _urlparse(url)
            if parsed.scheme and parsed.scheme not in ('http', 'https', ''):
                return False
            # Use path only (strips ?ver=6.6.2 and similar query strings)
            path = (parsed.path or '').lower().rstrip('/')
            # Reject non-HTML extensions even when followed by query strings
            if any(path.endswith(ext) for ext in _BAD_LINK_EXTS):
                return False
            # Reject WordPress junk and asset path patterns
            bad_segments = {
                '/css/', '/js/', '/fonts/', '/font/',
                '/assets/css/', '/assets/js/',
                '/static/css/', '/static/js/',
                '/wp-content/', '/wp-includes/', '/wp-json/',
                '/wp-admin/', '/feed/', '/xmlrpc',
            }
            url_lower = url.lower()
            if any(seg in url_lower for seg in bad_segments):
                return False
            return True

        for m in matches[:12]:
            results['suggestions'].append({
                'text':         f"{m['record_title']}: {m['snippet']}",
                'source':       'website_content',
                'confidence':   round(m['score'], 3),
                'record_type':  m['record_type'],
                'record_id':    m['record_id'],
                'record_title': m['record_title'],
                'is_exact':     m.get('is_exact', False),
            })
            for img in m['images']:
                if img['url'] and img['url'] not in seen_images and len(results['images']) < 8:
                    seen_images.add(img['url'])
                    results['images'].append(img)
            for lnk in m['links']:
                if _is_good_link(lnk['url']) and lnk['url'] not in seen_links and len(results['related_links']) < 10:
                    seen_links.add(lnk['url'])
                    results['related_links'].append(lnk)
            ctx = m.get('context_text') or m.get('full_text') or m.get('snippet', '')
            context_parts.append(
                f"[{m['record_type']}] {m['record_title']} (score {m['score']:.2f}):\n{ctx[:1000]}"
            )

        results['context_text'] = '\n\n'.join(context_parts)
        app.logger.info(
            f"Search '{query}': {len(matches)} candidates -> "
            f"{len(results['suggestions'])} shown, exact={results['exact_match']}, "
            f"crawled_pages_searched={len(all_crawled)}"
        )

    except Exception as e:
        app.logger.error(f"search_website_content error: {e}")

    return results


# ---------------------------------------------------------------------------
#  Anthropic Claude API
# ---------------------------------------------------------------------------

def call_claude_api(query: str, website_search: dict = None) -> dict:
    import anthropic

    api_key = app.config.get('ANTHROPIC_API_KEY', '') or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        print("[claude] ERROR: ANTHROPIC_API_KEY not configured")
        return None

    print(f"[claude] Calling Claude API for query: {query}")

    USE_CACHE  = not IS_PRODUCTION
    cache_file = None

    if USE_CACHE:
        cache_dir = os.path.join(app.root_path, 'cache')
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, f"ai_{hashlib.md5(query.encode()).hexdigest()}.json")
        if os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file) < 3600):
            try:
                with open(cache_file, 'r') as f:
                    cached = json.load(f)
                print("[claude] Cache hit")
                return cached
            except Exception:
                pass

    if website_search is None:
        website_search = search_website_content(query)

    context_text   = website_search.get('context_text', '')
    has_db_content = bool(context_text.strip())

    system_prompt = (
        "You are Tutoy, the official AI assistant for DAP-COE "
        "(Development Academy of the Philippines - Center of Excellence on Public Sector Productivity). "
        "STRICT RULES - follow every rule exactly: "
        "1. Respond with VALID JSON ONLY - no markdown, no backticks, no extra text outside the JSON. "
        "2. CONTENT SOURCE PRIORITY: "
        "   - If 'Website Context' is provided and relevant, base your answer on it. "
        "   - The context includes content from the main site, nav pages, card buttons, AND their "
        "     subpages (crawled up to 3 levels deep). Prioritize the most specific match. "
        "   - If 'Website Context' is empty or irrelevant, use your general knowledge about public sector "
        "     productivity, Philippine governance, DAP programs, and digital transformation. "
        "     NEVER say you cannot answer. "
        "3. claude_says RULES: "
        "   - Write 120 to 200 words maximum. Be concise, direct, and informative. "
        "   - DO NOT end with generic community/networking sentences. "
        "   - DO NOT repeat the same idea twice in different words. "
        "   - End with a specific, actionable insight or fact relevant to the query. "
        "   - Use plain prose - no bullet points, no headers inside this field. "
        "4. key_points: exactly 4 short strings, each under 15 words, each a distinct insight. "
        "   No duplicates. No generic filler. Each point must add new information. "
        "5. global_suggestions: exactly 3 strings - specific follow-up questions the user "
        "   would actually want to ask next, directly related to the query topic. "
        "6. Leave 'image' as empty string '' always - images are handled separately. "
        "7. If the query is completely unrelated to DAP-COE or public sector (e.g. cooking, sports), "
        "   answer briefly with general knowledge and suggest a related DAP-COE topic."
    )

    context_block = (
        f"\n\n=== Website Context (use this if relevant) ===\n{context_text}\n=== End Context ==="
        if has_db_content
        else "\n\n=== Website Context ===\n(No matching content found - use general knowledge.)\n=== End Context ==="
    )

    user_prompt = (
        f"User query: {query}"
        f"{context_block}\n\n"
        "IMPORTANT REMINDERS:\n"
        "- claude_says must be 120-200 words, no generic ending sentences\n"
        "- If no website context matched, use general knowledge - never refuse to answer\n"
        "- key_points must be 4 distinct, specific points\n\n"
        "Respond with ONLY this valid JSON structure:\n"
        '{\n'
        '  "gemini_says": "your 120-200 word response here",\n'
        '  "key_points": ["point 1", "point 2", "point 3", "point 4"],\n'
        '  "image": "",\n'
        '  "global_suggestions": ["question 1", "question 2", "question 3"],\n'
        '  "quick_navigation": ["Home", "Programs", "Services", "Contact"],\n'
        '  "related_links": []\n'
        '}'
    )

    try:
        client  = anthropic.Anthropic(api_key=api_key)
        print("[claude] Making request to Claude API...")

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )

        text = message.content[0].text.strip() if message.content else ''
        print(f"[claude] Response (first 200): {text[:200]}")

        if not text:
            print("[claude] Empty content from Claude")
            return None

        parsed = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            for pat in ['```json', '```']:
                if pat in text:
                    try:
                        parsed = json.loads(text.split(pat)[1].split('```')[0].strip())
                        break
                    except Exception:
                        pass
            if not parsed:
                start = text.find('{')
                if start >= 0:
                    for end_pos in range(len(text), start, -1):
                        chunk = text[start:end_pos].strip()
                        try:
                            parsed = json.loads(chunk)
                            break
                        except Exception:
                            diff = chunk.count('{') - chunk.count('}')
                            if diff > 0:
                                try:
                                    parsed = json.loads(chunk + '}' * diff)
                                    break
                                except Exception:
                                    pass

        if not parsed:
            print(f"[claude] Could not parse JSON. Raw: {text[:500]}")
            return None

        result = {
            'gemini_says':                 str(parsed.get('gemini_says', '')).strip() or 'No response available.',
            'key_points':                  parsed.get('key_points', []) or [],
            'image':                       parsed.get('image', '') or '',
            'global_suggestions':          parsed.get('global_suggestions', []) or [],
            'quick_navigation':            parsed.get('quick_navigation', []) or ['Home', 'Programs', 'Services', 'Contact'],
            'website_content_suggestions': [],
            'related_images':              website_search.get('images', [])[:6],
            'related_links':               website_search.get('related_links', [])[:8],
        }

        if USE_CACHE and cache_file:
            try:
                with open(cache_file, 'w') as f:
                    json.dump(result, f)
                print("[claude] Response cached (local dev only)")
            except Exception as e:
                print(f"[claude] Cache write error: {e}")
        else:
            print("[claude] Cache skipped (production mode)")

        return result

    except Exception as e:
        print(f"[claude] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
    return None


# ---------------------------------------------------------------------------
#  MAIN SITE ROUTES
# ---------------------------------------------------------------------------
def _build_training_items(nav_link):
    """
    Returns a list of dicts: {title, description, image}
    Sources (in priority order):
      1. Button contents of any Card whose title contains 'training'
         (each button becomes one training card, using its content as description
          and button_images[0] as the image)
      2. Fallback: nav_link.page_content parsed as line-separated titles
    """
    items = []
 
    # -- Try to get items from Cards whose title mentions 'training' ----------
    training_cards = db.session.query(Card).filter(
        Card.title.ilike('%training%')
    ).order_by(Card.card_order).all()
 
    for card in training_cards:
        buttons      = card.get_buttons()
        btn_contents = card.get_button_contents()
        btn_images   = card.get_button_images()
        btn_links    = card.get_button_links()
 
        for idx, btn_label in enumerate(buttons):
            idx_str  = str(idx)
            content  = btn_contents.get(idx_str, '')
            raw_imgs = btn_images.get(idx_str, [])
            link_url = btn_links.get(idx_str, '')
 
            # Pick first image from the button's gallery (if any)
            img_file = ''
            if isinstance(raw_imgs, list) and raw_imgs:
                first = raw_imgs[0]
                if isinstance(first, dict):
                    img_file = (first.get('src') or first.get('filename') or '').strip()
                elif isinstance(first, str):
                    img_file = first.strip()
            # Fall back to the card's own image
            if not img_file and card.image:
                img_file = card.image.strip()
 
            # Strip path prefixes ? bare filename
            img_file = re.sub(r'^(static/images/|images/|static/)', '', img_file)
 
            items.append({
                'title':       btn_label.strip(),
                'description': _strip_html(content)[:300] if content else '',
                'image':       img_file,
                'link_url':    link_url,
            })
 
    # -- Fallback: parse nav_link.page_content as newline-separated titles ----
    if not items and nav_link.page_content:
        plain = _strip_html(nav_link.page_content)
        for line in plain.splitlines():
            line = line.strip(' -*')
            if len(line) > 3:
                items.append({'title': line, 'description': '', 'image': '', 'link_url': ''})
 
    return items

@app.route('/nav-page/<int:nav_id>')
def nav_page(nav_id):
    nav_link = db.session.get(NavigationLink, nav_id)
    if not nav_link:
        return redirect(url_for('index'))
 
    nav_links       = get_all_nav_links()
    logo_image      = get_content('logo_image', 'images/dap-logo.png')
    published_pages = (
        db.session.query(Page)
        .filter_by(is_published=True)
        .order_by(Page.page_order)
        .all()
    )
 
    link_text_lower = (nav_link.link_text or '').lower()
 
    # -- Shared hero bg resolver (mirrors whats_new logic exactly) ------------
 # -- Shared hero bg resolver (mirrors whats_new logic exactly) ------------
    def _resolve_hero_bg(first_image_filename=None):
        if first_image_filename:
            clean = first_image_filename.split('/')[-1]
            return f"/static/images/{clean}"
        if nav_link.background_image:
            clean = nav_link.background_image.split('/')[-1]
            return f"/static/images/{clean}"
        return ''
 
    # -- Common footer kwargs --------------------------------------------------
    footer_kwargs = dict(
        company_name     = get_content('company_name',     'Development Academy of The Philippines'),
        company_subtitle = get_content('company_subtitle', 'Center of Excellence on Public Sector Productivity'),
        company_address  = get_content('company_address',  'DAP Building, San Miguel Avenue, Pasig City 1500'),
        company_phone    = get_content('company_phone',    '+632 631 0921 to 30'),
        company_fax      = get_content('company_fax',      '+632 631 2123'),
        company_email    = get_content('company_email',    'coe_psp@dap.edu.ph'),
        apo_logo         = get_content('apo_logo',         'images/apo.png'),
    )
 
    # -- What's New -----------------------------------------------------------
    if 'new' in link_text_lower:
        articles = (
            NewsArticle.query
            .filter_by(is_published=True)
            .order_by(
                NewsArticle.article_order.asc(),
                NewsArticle.published_at.desc()
            )
            .all()
        )
        hero_bg_url = ''
        for a in [x for x in articles if not x.is_archived]:
            if a.article_image:
                hero_bg_url = _resolve_hero_bg(a.article_image)
                break
        if not hero_bg_url:
            hero_bg_url = _resolve_hero_bg()
 
        return render_template(
            'whats_new_page.html',
            nav_link           = nav_link,
            nav_links          = nav_links,
            published_pages    = published_pages,
            logo_image         = logo_image,
            articles           = articles,
            news_section_title = nav_link.link_text,
            GOV_IMAGE_BASE     = GOV_IMAGE_BASE,
            hero_bg_url        = hero_bg_url,
            **footer_kwargs,
        )
 
    # -- Trainings -------------------------------------------------------------
    is_trainings = any(kw in link_text_lower for kw in (
        'training', 'capacity', 'course', 'seminar', 'workshop'
    ))
 
    if is_trainings:
        programs = (
            TrainingProgram.query
            .filter_by(is_published=True)
            .order_by(
                TrainingProgram.program_order.asc(),
                TrainingProgram.published_at.desc()
            )
            .all()
        )
 
        hero_bg_url = ''
        for p in [x for x in programs if not x.is_archived]:
            if p.background_image:
                hero_bg_url = _resolve_hero_bg(p.background_image)
                break
        if not hero_bg_url:
            for p in [x for x in programs if not x.is_archived]:
                if p.cover_image:
                    hero_bg_url = _resolve_hero_bg(p.cover_image)
                    break
        if not hero_bg_url:
            hero_bg_url = _resolve_hero_bg()
 
        return render_template(
            'trainings_page.html',
            nav_link        = nav_link,
            nav_links       = nav_links,
            published_pages = published_pages,
            logo_image      = logo_image,
            section_title   = nav_link.link_text,
            programs        = programs,
            GOV_IMAGE_BASE  = GOV_IMAGE_BASE,
            hero_bg_url     = hero_bg_url,
            **footer_kwargs,
        )
 
    # -- Default nav page ------------------------------------------------------
    return render_template(
        'nav_page.html',
        nav_link        = nav_link,
        nav_links       = nav_links,
        published_pages = published_pages,
        logo_image      = logo_image,
    )

@app.route('/<path:path>')
def catch_all(path):
    if any(path.startswith(p) for p in ('nav-page/', 'admin', 'card-content/', 'page/', 'api/')):
        return redirect(url_for('index'))
    path_text = path.replace('-', ' ').replace('_', ' ').title()
    nav_link  = db.session.query(NavigationLink).filter(
        NavigationLink.link_text.ilike(f'%{path_text}%')
    ).first()
    if nav_link:
        return redirect(url_for('nav_page', nav_id=nav_link.id))
    return redirect(url_for('index'))


@app.route('/card-content/<int:card_id>/<int:button_index>')
def card_content(card_id, button_index):
    card = db.session.get(Card, card_id)
    if not card:
        return redirect(url_for('index'))
    buttons         = card.get_buttons()
    button_contents = card.get_button_contents()
    if button_index < 0 or button_index >= len(buttons):
        return redirect(url_for('index'))
    nav_links       = get_all_nav_links()
    logo_image      = get_content('logo_image', 'images/logo.png')
    published_pages = db.session.query(Page).filter_by(is_published=True).order_by(Page.page_order).all()
    return render_template('card_content.html',
                           card=card,
                           button_text=buttons[button_index],
                           button_content=button_contents.get(str(button_index), ''),
                           button_index=button_index,
                           nav_links=nav_links,
                           published_pages=published_pages,
                           logo_image=logo_image)


@app.route('/page/<slug>')
def view_page(slug):
    page = db.session.query(Page).filter_by(slug=slug, is_published=True).first()
    if not page:
        return redirect(url_for('index'))
    nav_links       = get_all_nav_links()
    published_pages = db.session.query(Page).filter_by(is_published=True).order_by(Page.page_order).all()
    logo_image      = get_content('logo_image', 'images/dap-logo.png')
    return render_template('custom_page.html',
                           page=page,
                           blocks=page.get_blocks_ordered(),
                           nav_links=nav_links,
                           published_pages=published_pages,
                           logo_image=logo_image)


@app.route('/news/<slug>')
def news_article_page(slug):
    article         = NewsArticle.query.filter_by(slug=slug, is_published=True).first_or_404()
    nav_links       = get_all_nav_links()
    published_pages = db.session.query(Page).filter_by(is_published=True).order_by(Page.page_order).all()
    logo_image      = get_content('logo_image', 'images/logo.png')

    # Fetch other published, non-archived articles for the sidebar,
    # excluding the one currently being viewed
    recent_articles = (
        NewsArticle.query
        .filter(
            NewsArticle.is_published == True,
            NewsArticle.is_archived == False,
            NewsArticle.slug != slug,
        )
        .order_by(NewsArticle.published_at.desc())
        .limit(5)
        .all()
    )

    return render_template(
        'news_article.html',
        article=article,
        nav_links=nav_links,
        published_pages=published_pages,
        logo_image=logo_image,
        recent_articles=recent_articles,
        GOV_IMAGE_BASE=GOV_IMAGE_BASE,
    )


@app.route('/')
def index():
    now               = datetime.now()
    current_year      = now.year
    hero_title        = get_content('hero_title', 'LEADING THE MOVEMENT IN <br>ADVANCING INNOVATION AND <br>PRODUCTIVITY IN THE <span class="text-[#cdae2c]">PUBLIC SECTOR</span>')
    hero_image        = get_content('hero_image', 'images/Hero-Banner.png')
    hero_opacity      = get_content('hero_opacity', '90')
    search_placeholder = get_content('search_placeholder', 'Ask Tutoy anything about Public Sector Productivity...?')
    cards             = get_all_cards()
    nav_links         = get_all_nav_links()
    published_pages   = db.session.query(Page).filter_by(is_published=True).order_by(Page.page_order).all()
    company_name      = get_content('company_name',     'Development Academy of The Philippines')
    company_subtitle  = get_content('company_subtitle', 'Center of Excellence on Public Sector Productivity')
    company_address   = get_content('company_address',  'DAP Building, San Miguel Avenue, Pasig City 1500')
    company_phone     = get_content('company_phone',    '+632 631 0921 to 30')
    company_fax       = get_content('company_fax',      '+632 631 2123')
    company_email     = get_content('company_email',    'coe_psp@dap.edu.ph')
    logo_image        = get_content('logo_image',       'images/dap-logo.png')
    gemini_image      = get_content('gemini_image',     'images/gemini.png')
    apo_logo          = get_content('apo_logo',         'images/apo.png')

    context = dict(
        now=now, hero_title=hero_title, hero_image=hero_image, hero_opacity=hero_opacity,
        search_placeholder=search_placeholder, cards=cards, nav_links=nav_links,
        published_pages=published_pages, company_name=company_name,
        company_subtitle=company_subtitle, company_address=company_address,
        company_phone=company_phone, company_fax=company_fax, company_email=company_email,
        logo_image=logo_image, gemini_image=gemini_image, apo_logo=apo_logo,
        current_year=current_year,
    )
    resp = make_response(render_template('index.html', **context))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma']  = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# ---------------------------------------------------------------------------
#  AI SEARCH ENDPOINT
# ---------------------------------------------------------------------------

@app.route('/api/search', methods=['GET'])
def api_search():
    query = request.args.get('q', '').strip()

    if not query:
        return jsonify({
            'gemini_says': 'Please enter a search query.',
            'key_points': [], 'image': '',
            'global_suggestions': [], 'quick_navigation': [],
            'website_content_suggestions': [], 'related_images': [], 'related_links': [],
        })

    if is_greeting(query):
        return jsonify({
            'gemini_says': (
                "Hello! I'm Tutoy, the official AI assistant for DAP-COE "
                "(Development Academy of the Philippines - Center of Excellence on Public Sector Productivity). "
                "I'm here to help you explore our programs, training courses, and services."
            ),
            'key_points': [
                'Explore DAP-COE programs and services',
                'Search for training courses and schedules',
                'Learn about GovLab and other initiatives',
                'Get guided through enrollment and registration',
            ],
            'image': '',
            'global_suggestions': [
                'What training programs are available?',
                'Tell me about GovLab',
                'How do I enroll in a course?',
            ],
            'quick_navigation':            ['Home', 'Programs', 'Services', 'Contact'],
            'website_content_suggestions': [], 'related_images': [], 'related_links': [],
        })

    # -- Run DB search first (Claude needs its context output) ----------------
    # But cap the live-fetch phase so Claude never waits more than 4 s for it.
    website_search = search_website_content(query)

    # -- Matched image (fast - pure in-memory loop) ---------------------------
    matched_image = ''
    for card in get_all_cards():
        if query.lower() in (card.title or '').lower():
            matched_image = _image_url(card.image)
            break
    if not matched_image and website_search.get('images'):
        matched_image = website_search['images'][0]['url']

    # -- Call Claude in a thread so we can apply a hard timeout ---------------
    # This prevents a slow Anthropic response from blocking the whole worker.
    ai_result_box = [None]

    def _call_claude():
        try:
            ai_result_box[0] = call_claude_api(query, website_search=website_search)
        except Exception as e:
            print(f"[claude] Thread error: {e}")

    claude_thread = threading.Thread(target=_call_claude, daemon=True)
    claude_thread.start()
    claude_thread.join(timeout=28)   # hard ceiling - never block the request longer than 28 s

    ai_response = ai_result_box[0]

    if ai_response:
        print("[claude] Got valid response from Claude API")
        if not ai_response.get('image') and matched_image:
            ai_response['image'] = matched_image
        ai_response['website_content_suggestions'] = []
        ai_response['related_images'] = website_search.get('images', [])[:6]
        ai_response['related_links']  = website_search.get('related_links', [])[:8]
        return jsonify(ai_response)

    print("[claude] Claude API returned None - DB-only fallback")
    db_snippets   = [s.get('text', '') for s in website_search.get('suggestions', [])[:4]]
    fallback_says = (
        f'Here\'s what I found on our website about "{query}".'
        if website_search.get('suggestions')
        else 'I couldn\'t connect to the AI service right now. Try searching "trainings", "GovLab", or "community".'
    )

    return jsonify({
        'gemini_says':                 fallback_says,
        'key_points':                  db_snippets or [f'Search query: "{query}"', 'Browse our website for more information'],
        'image':                       matched_image,
        'global_suggestions':          ['Tell me about our programs', 'What services do we offer?', 'How can I enroll?'],
        'quick_navigation':            ['Home', 'Programs', 'Services', 'Contact'],
        'website_content_suggestions': [],
        'related_images':              website_search.get('images', [])[:6],
        'related_links':               website_search.get('related_links', [])[:8],
    })

    website_search = search_website_content(query)

    matched_image = ''
    for card in get_all_cards():
        if query.lower() in (card.title or '').lower():
            matched_image = _image_url(card.image)
            break
    if not matched_image and website_search.get('images'):
        matched_image = website_search['images'][0]['url']

    ai_response = call_claude_api(query, website_search=website_search)

    if ai_response:
        print("[claude] Got valid response from Claude API")
        if not ai_response.get('image') and matched_image:
            ai_response['image'] = matched_image
        ai_response['website_content_suggestions'] = []
        ai_response['related_images'] = website_search.get('images', [])[:6]
        ai_response['related_links']  = website_search.get('related_links', [])[:8]
        return jsonify(ai_response)

    print("[claude] Claude API returned None - DB-only fallback")
    db_snippets   = [s.get('text', '') for s in website_search.get('suggestions', [])[:4]]
    fallback_says = (
        f'Here\'s what I found on our website about "{query}".'
        if website_search.get('suggestions')
        else 'I couldn\'t connect to the AI service right now. Try searching "trainings", "GovLab", or "community".'
    )

    return jsonify({
        'gemini_says':                 fallback_says,
        'key_points':                  db_snippets or [f'Search query: "{query}"', 'Browse our website for more information'],
        'image':                       matched_image,
        'global_suggestions':          ['Tell me about our programs', 'What services do we offer?', 'How can I enroll?'],
        'quick_navigation':            ['Home', 'Programs', 'Services', 'Contact'],
        'website_content_suggestions': [],
        'related_images':              website_search.get('images', [])[:6],
        'related_links':               website_search.get('related_links', [])[:8],
    })


# ---------------------------------------------------------------------------
#  NEWS ARTICLE ROUTES (Admin API)
# ---------------------------------------------------------------------------

def _parse_news_dt(value):
    """Parse ISO / datetime-local string ? aware datetime."""
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


# AFTER - safe, handles None datetimes and missing columns

@app.route('/admin/api/news-articles')
@login_required
def api_list_news():
    try:
        articles = NewsArticle.query.order_by(
            NewsArticle.published_at.desc()
        ).all()

        def _dt(val):
            if val is None:
                return None
            try:
                return val.isoformat()
            except Exception:
                return str(val)

        return jsonify({
            'success': True,
            'articles': [{
                'id':            a.id,
                'title':         a.title or '',
                'slug':          a.slug or '',
                'excerpt':       a.excerpt or '',
                'body':          a.body or '',
                'cover_image':   a.cover_image or '',
                'article_image': getattr(a, 'article_image', None) or '',
                'published_at':  _dt(a.published_at),
                'is_published':  bool(a.is_published),
                'is_archived':   bool(a.is_archived),
                'nav_link_id':   a.nav_link_id,
                'card_id':       a.card_id,
            } for a in articles]
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        app.logger.error(f"api_list_news error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
 
 
@app.route('/admin/api/add-news-article', methods=['POST'])
@login_required
def api_add_news_article():
    data  = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'success': False, 'error': 'Title is required'}), 400
 
    article = NewsArticle(
        title         = title,
        slug          = NewsArticle.unique_slug(title),
        excerpt       = (data.get('excerpt') or '').strip() or None,
        body          = data.get('body') or None,
        cover_image   = (data.get('cover_image') or '').strip() or None,
        article_image = (data.get('article_image') or '').strip() or None,  # ? NEW
        published_at  = _parse_news_dt(data.get('published_at')),
        is_published  = bool(data.get('is_published', False)),
        is_archived   = bool(data.get('is_archived', False)),
        nav_link_id   = data.get('nav_link_id') or None,
        card_id       = data.get('card_id') or None,
    )
    db.session.add(article)
    try:
        db.session.commit()
        return jsonify({'success': True, 'id': article.id, 'slug': article.slug})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
 
 
@app.route('/admin/api/update-news-article', methods=['POST'])
@login_required
def api_update_news_article():
    data       = request.get_json(silent=True) or {}
    article_id = data.get('id')
    if not article_id:
        return jsonify({'success': False, 'error': 'id is required'}), 400
 
    article = NewsArticle.query.get(article_id)
    if not article:
        return jsonify({'success': False, 'error': 'Article not found'}), 404
 
    if 'title' in data and data['title'].strip():
        article.title = data['title'].strip()
    if 'excerpt' in data:
        article.excerpt = (data['excerpt'] or '').strip() or None
    if 'body' in data:
        article.body = data['body'] or None
    if 'cover_image' in data:
        article.cover_image = (data['cover_image'] or '').strip() or None
    if 'article_image' in data:                                            # ? NEW
        article.article_image = (data['article_image'] or '').strip() or None
    if 'published_at' in data:
        article.published_at = _parse_news_dt(data['published_at'])
    if 'is_published' in data:
        article.is_published = bool(data['is_published'])
    if 'is_archived' in data:
        article.is_archived = bool(data['is_archived'])
    if 'nav_link_id' in data:
        article.nav_link_id = data['nav_link_id'] or None
    if 'card_id' in data:
        article.card_id = data['card_id'] or None
 
    article.updated_at = datetime.now(timezone.utc)
 
    try:
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/api/delete-news-article', methods=['POST'])
@login_required
def api_delete_news_article():
    data    = request.get_json(silent=True) or {}
    article = NewsArticle.query.get(data.get('id'))
    if not article:
        return jsonify({'success': False, 'error': 'Article not found'}), 404
    db.session.delete(article)
    try:
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

def _parse_program_dt(value):
    """Parse ISO / datetime-local string ? aware datetime."""
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)
 
 
@app.route('/admin/api/training-programs')
@login_required
def api_list_training_programs():
    try:
        programs = TrainingProgram.query.order_by(
            TrainingProgram.program_order.asc(),
            TrainingProgram.published_at.desc()
        ).all()
 
        def _dt(val):
            if val is None:
                return None
            try:
                return val.isoformat()
            except Exception:
                return str(val)
 
        return jsonify({
            'success': True,
            'programs': [{
                'id':           p.id,
                'title':        p.title or '',
                'slug':         p.slug or '',
                'excerpt':      p.excerpt or '',
                'body':         p.body or '',
                'cover_image':  p.cover_image or '',
                'program_order':p.program_order,
                'published_at': _dt(p.published_at),
                'is_published': bool(p.is_published),
                'is_archived':  bool(p.is_archived),
                'nav_link_id':  p.nav_link_id,
         'background_image': p.background_image or '',
            } for p in programs]
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
 
 
@app.route('/admin/api/add-training-program', methods=['POST'])
@login_required
def api_add_training_program():
    data  = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'success': False, 'error': 'Title is required'}), 400
 
    program = TrainingProgram(
        title         = title,
        slug          = TrainingProgram.unique_slug(title),
        excerpt       = (data.get('excerpt') or '').strip() or None,
        body          = data.get('body') or None,
        cover_image       = (data.get('cover_image') or '').strip() or None,
        background_image  = (data.get('background_image') or '').strip() or None,
        program_order     = int(data.get('program_order', 0)),
        published_at  = _parse_program_dt(data.get('published_at')),
        is_published  = bool(data.get('is_published', False)),
        is_archived   = bool(data.get('is_archived', False)),
        nav_link_id   = data.get('nav_link_id') or None,
    )
    db.session.add(program)
    try:
        db.session.commit()
        return jsonify({'success': True, 'id': program.id, 'slug': program.slug})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
 
 
@app.route('/admin/api/update-training-program', methods=['POST'])
@login_required
def api_update_training_program():
    data = request.get_json(silent=True) or {}
    pid  = data.get('id')
    if not pid:
        return jsonify({'success': False, 'error': 'id is required'}), 400
 
    program = TrainingProgram.query.get(pid)
    if not program:
        return jsonify({'success': False, 'error': 'Program not found'}), 404
 
    if 'title' in data and (data['title'] or '').strip():
        program.title = data['title'].strip()
    if 'excerpt' in data:
        program.excerpt = (data['excerpt'] or '').strip() or None
    if 'body' in data:
        program.body = data['body'] or None
    if 'cover_image' in data:
        program.cover_image = (data['cover_image'] or '').strip() or None
    if 'background_image' in data:
        program.background_image = (data['background_image'] or '').strip() or None
    if 'program_order' in data:
        program.program_order = int(data['program_order'] or 0)
    if 'published_at' in data:
        program.published_at = _parse_program_dt(data['published_at'])
    if 'is_published' in data:
        program.is_published = bool(data['is_published'])
    if 'is_archived' in data:
        program.is_archived = bool(data['is_archived'])
    if 'nav_link_id' in data:
        program.nav_link_id = data['nav_link_id'] or None
 
    program.updated_at = datetime.now(timezone.utc)
 
    try:
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
 
 
@app.route('/admin/api/delete-training-program', methods=['POST'])
@login_required
def api_delete_training_program():
    data    = request.get_json(silent=True) or {}
    program = TrainingProgram.query.get(data.get('id'))
    if not program:
        return jsonify({'success': False, 'error': 'Program not found'}), 404
    db.session.delete(program)
    try:
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
 
 
@app.route('/admin/api/upload-training-image', methods=['POST'])
@login_required
def upload_training_image():
    """Dedicated upload endpoint for training program cover images."""
    try:
        file = request.files.get('file') or request.files.get('image')
        if not file or file.filename == '':
            return no_cache_json({'success': False, 'error': 'No file uploaded'})
        if not allowed_file(file.filename):
            return no_cache_json({'success': False, 'error': 'Invalid file type'})
        filename      = f"{int(datetime.now().timestamp())}_{secure_filename(file.filename)}"
        upload_folder = app.config['UPLOAD_FOLDER']
        os.makedirs(upload_folder, exist_ok=True)
        save_path = os.path.join(upload_folder, filename)
        file.save(save_path)
        if not os.path.exists(save_path):
            return no_cache_json({'success': False, 'error': f'File failed to save at {save_path}'})
        return no_cache_json({
            'success':  True,
            'filename': filename,
            'url':      f"{GOV_IMAGE_BASE}/{filename}",
            'saved_to': save_path,
        })
    except Exception as e:
        return no_cache_json({'success': False, 'error': str(e)})

# ---------------------------------------------------------------------------
#  ADMIN ROUTES
# ---------------------------------------------------------------------------

@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_panel'))
    login_error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = Admin.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('admin_panel'))
        login_error = 'Invalid credentials'
    return render_template('admin_login.html', login_error=login_error)


@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for('admin_login'))


@app.route('/admin/panel')
@login_required
def admin_panel():
    return render_template('admin_panel.html',
                           content_sections=get_all_content(),
                           cards=get_all_cards(),
                           nav_links=get_all_nav_links(),
                           professionals=get_all_professionals(),
                           logo_image=get_content('logo_image', 'images/dap-logo.png'))


@app.route('/admin/page-builder')
@login_required
def page_builder():
    return render_template('page_builder.html', logo_image=get_content('logo_image', 'images/dap-logo.png'))


# ---------------------------------------------------------------------------
#  ADMIN API ROUTES
# ---------------------------------------------------------------------------

@app.route('/admin/api/upload-health')
@login_required
def upload_health():
    try:
        upload_folder = app.config.get('UPLOAD_FOLDER', '')
        os.makedirs(upload_folder, exist_ok=True)
        test_path = os.path.join(upload_folder, '.health_check')
        with open(test_path, 'w') as f:
            f.write('ok')
        os.remove(test_path)
        return no_cache_json({'ok': True, 'url': GOV_IMAGE_BASE, 'upload_folder': upload_folder})
    except Exception as e:
        return no_cache_json({'ok': False, 'error': str(e), 'upload_folder': app.config.get('UPLOAD_FOLDER', 'NOT SET')})


@app.route('/admin/api/upload-image', methods=['POST'])
@login_required
def upload_image():
    try:
        file = request.files.get('file') or request.files.get('image')
        if not file or file.filename == '':
            return no_cache_json({'success': False, 'error': 'No file uploaded'})
        if not allowed_file(file.filename):
            return no_cache_json({'success': False, 'error': 'Invalid file type. Allowed: png, jpg, jpeg, gif, webp'})
        filename      = f"{int(datetime.now().timestamp())}_{secure_filename(file.filename)}"
        upload_folder = app.config['UPLOAD_FOLDER']
        os.makedirs(upload_folder, exist_ok=True)
        save_path = os.path.join(upload_folder, filename)
        file.save(save_path)
        if not os.path.exists(save_path):
            return no_cache_json({'success': False, 'error': f'File failed to save at {save_path}'})
        return no_cache_json({
            'success': True, 'filename': filename,
            'url': f"{GOV_IMAGE_BASE}/{filename}", 'saved_to': save_path,
        })
    except Exception as e:
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/update-content', methods=['POST'])
@login_required
def update_content():
    try:
        if request.is_json:
            key, value = request.json.get('key'), request.json.get('value', '')
        else:
            key, value = request.form.get('key'), request.form.get('value', '')
        if not key:
            return no_cache_json({'success': False, 'error': 'Key is required'})
        value_str = str(value).strip() if value is not None else ''
        if value_str in ('undefined', 'null', 'None'):
            return no_cache_json({'success': False, 'error': f'Invalid value "{value_str}" blocked.'})
        content = db.session.query(ContentSection).filter_by(content_key=key).first()
        if content:
            content.content_value = value_str
            db.session.commit()
            clear_ai_cache()
            return no_cache_json({'success': True, 'message': 'Content saved'})
        return no_cache_json({'success': False, 'error': f'Content key "{key}" not found'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


# -- Card routes --------------------------------------------------------------

@app.route('/admin/api/update-card-title', methods=['POST'])
@login_required
def update_card_title():
    try:
        card_id = request.form.get('id')
        title   = request.form.get('title', '').strip()
        if not card_id:
            return no_cache_json({'success': False, 'error': 'Card ID required'})
        if not title:
            return no_cache_json({'success': False, 'error': 'Title cannot be empty'})
        card = db.session.get(Card, int(card_id))
        if not card:
            return no_cache_json({'success': False, 'error': f'Card {card_id} not found'})
        card.title = title
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True, 'message': 'Title saved'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/update-card-image', methods=['POST'])
@login_required
def update_card_image():
    try:
        card_id = request.form.get('id')
        image   = request.form.get('image', '').strip()
        if not card_id:
            return no_cache_json({'success': False, 'error': 'Card ID required'})
        card = db.session.get(Card, int(card_id))
        if not card:
            return no_cache_json({'success': False, 'error': f'Card {card_id} not found'})
        card.image = image
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True, 'message': 'Image saved'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/update-card', methods=['POST'])
@login_required
def update_card():
    try:
        card_id = request.form.get('id')
        if not card_id:
            return no_cache_json({'success': False, 'error': 'Card ID required'})
        card = db.session.get(Card, int(card_id))
        if not card:
            return no_cache_json({'success': False, 'error': 'Card not found'})
        card.title            = request.form.get('title') or card.title
        card.image            = request.form.get('image') or card.image
        card.background_image = request.form.get('background_image', '') or card.background_image
        buttons = request.form.getlist('buttons')
        if buttons:
            card.buttons = json.dumps([b for b in buttons if b.strip()])
        try:
            card.button_contents = json.dumps(json.loads(request.form.get('button_contents', '{}')))
        except Exception:
            card.button_contents = json.dumps({})
        try:
            card.button_images = json.dumps(json.loads(request.form.get('button_images', '{}')))
        except Exception:
            card.button_images = json.dumps({})
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True, 'message': 'Card saved'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/add-card', methods=['POST'])
@login_required
def add_card_api():
    try:
        title       = request.form.get('title', '').strip()
        image       = request.form.get('image', '').strip()
        buttons_raw = request.form.get('buttons', '[]')
        if not title:
            return no_cache_json({'success': False, 'error': 'Card title is required'}, 400)
        try:
            buttons_list = json.loads(buttons_raw)
            if not isinstance(buttons_list, list):
                buttons_list = []
        except (json.JSONDecodeError, TypeError):
            buttons_list = []
        max_order = db.session.query(db.func.max(Card.card_order)).scalar() or 0
        new_card  = Card(
            title=title, image=image or None,
            buttons=json.dumps(buttons_list),
            button_contents=json.dumps({}),
            button_links=json.dumps({}),
            button_images=json.dumps({}),
            button_background_images=json.dumps({}),
            card_order=max_order + 1,
        )
        db.session.add(new_card)
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True, 'message': 'Card created',
                              'card': {'id': new_card.id, 'title': new_card.title,
                                       'image': new_card.image, 'buttons': buttons_list,
                                       'card_order': new_card.card_order}})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)}, 500)


@app.route('/admin/api/delete-card', methods=['POST'])
@login_required
def delete_card_api():
    try:
        data    = request.get_json(silent=True) or {}
        card_id = data.get('id')
        if not card_id:
            return no_cache_json({'success': False, 'error': 'Card ID required'}, 400)
        card = db.session.get(Card, int(card_id))
        if not card:
            return no_cache_json({'success': False, 'error': 'Card not found'}, 404)
        db.session.delete(card)
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True, 'message': f'Card {card_id} deleted'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)}, 500)


# -- Button routes ------------------------------------------------------------

@app.route('/admin/api/update-button-name', methods=['POST'])
@login_required
def update_button_name():
    try:
        card_id      = request.form.get('card_id', '').strip()
        button_index = request.form.get('button_index', '').strip()
        new_name     = request.form.get('name', '').strip()
        if not card_id:
            return no_cache_json({'success': False, 'error': 'card_id is required'}, 400)
        if button_index == '':
            return no_cache_json({'success': False, 'error': 'button_index is required'}, 400)
        if not new_name:
            return no_cache_json({'success': False, 'error': 'Button name cannot be empty'}, 400)
        card_id_int = int(card_id)
        btn_idx     = int(button_index)
        card = db.session.get(Card, card_id_int)
        if not card:
            return no_cache_json({'success': False, 'error': f'Card {card_id_int} not found'}, 404)
        buttons = json.loads(card.buttons) if card.buttons else []
        if btn_idx < 0 or btn_idx >= len(buttons):
            return no_cache_json({'success': False, 'error': f'button_index {btn_idx} out of range'}, 400)
        old_name          = buttons[btn_idx]
        buttons[btn_idx]  = new_name
        card.buttons      = json.dumps(buttons)
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True, 'message': f'Renamed "{old_name}" to "{new_name}"',
                              'old_name': old_name, 'new_name': new_name})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)}, 500)


@app.route('/admin/api/update-button', methods=['POST'])
@login_required
def update_button():
    try:
        data = request.json
        card = db.session.get(Card, int(data.get('card_id', 0)))
        if not card:
            return no_cache_json({'success': False, 'error': 'Card not found'})
        idx = str(data.get('button_index'))

        def _load(f):
            try:
                return json.loads(f) if f else {}
            except Exception:
                return {}

        btn_links = _load(card.button_links)
        btn_conts = _load(card.button_contents)
        btn_bgs   = _load(card.button_background_images)
        btn_imgs  = _load(card.button_images)

        for field, store in [('link_url', btn_links), ('content', btn_conts), ('background_image', btn_bgs)]:
            val = data.get(field, '')
            if val:
                store[idx] = val
            elif idx in store:
                del store[idx]

        raw_imgs = data.get('images', [])
        if raw_imgs:
            clean_imgs = []
            for img in raw_imgs:
                if isinstance(img, dict):
                    src     = (img.get('src') or img.get('filename') or '').strip()
                    caption = (img.get('caption') or '').strip()
                    if src:
                        clean_imgs.append({'src': src, 'caption': caption})
                elif isinstance(img, str) and img.strip():
                    clean_imgs.append({'src': img.strip(), 'caption': ''})
            btn_imgs[idx] = clean_imgs
        elif idx in btn_imgs:
            del btn_imgs[idx]

        card.button_links             = json.dumps(btn_links)
        card.button_contents          = json.dumps(btn_conts)
        card.button_background_images = json.dumps(btn_bgs)
        card.button_images            = json.dumps(btn_imgs)
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True, 'message': 'Button saved'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/update-button-link', methods=['POST'])
@login_required
def update_button_link():
    try:
        data      = request.json
        card      = db.session.get(Card, data.get('card_id'))
        if not card:
            return no_cache_json({'success': False, 'message': 'Card not found'})
        btn_links = card.get_button_links()
        link_url  = data.get('link_url', '')
        idx       = str(data.get('button_index'))
        old_url   = btn_links.get(idx, '')

        if link_url.strip():
            btn_links[idx] = link_url.strip()
        else:
            btn_links.pop(idx, None)

        card.button_links = json.dumps(btn_links)
        db.session.commit()
        clear_ai_cache()

        # Bust cache for old URL
        for key in [old_url, f"__deep__{old_url}"]:
            if key and key in _url_cache:
                del _url_cache[key]

        # Auto-crawl new button URL in background
        new_url = link_url.strip()
        if new_url and new_url not in ('#', ''):
            def _bg_crawl(u=new_url):
                with app.app_context():
                    db.session.query(CrawledPage).filter_by(source_url=u).delete()
                    db.session.commit()
                    crawl_url_deep(u)
            threading.Thread(target=_bg_crawl, daemon=True).start()
            print(f"[crawler] Auto-crawl triggered for button link: {new_url}")

        return no_cache_json({'success': True})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'message': str(e)})


@app.route('/admin/api/update-button-images', methods=['POST'])
@login_required
def update_button_images():
    try:
        data     = request.json
        card     = db.session.get(Card, data.get('card_id'))
        if not card:
            return no_cache_json({'success': False, 'message': 'Card not found'})
        btn_imgs   = card.get_button_images()
        clean_imgs = []
        for img in data.get('images', []):
            if isinstance(img, dict):
                src     = (img.get('src') or img.get('filename') or '').strip()
                caption = (img.get('caption') or '').strip()
                if src:
                    clean_imgs.append({'src': src, 'caption': caption})
            elif isinstance(img, str) and img.strip():
                clean_imgs.append({'src': img.strip(), 'caption': ''})
        btn_imgs[str(data.get('button_index'))] = clean_imgs
        card.button_images = json.dumps(btn_imgs)
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True})
    except Exception as e:
        return no_cache_json({'success': False, 'message': str(e)})


@app.route('/admin/api/delete-gallery-image', methods=['POST'])
@login_required
def delete_gallery_image():
    try:
        data      = request.json
        card      = db.session.get(Card, int(data.get('card_id')))
        if not card:
            return no_cache_json({'success': False, 'message': 'Card not found'})
        btn_imgs  = card.get_button_images()
        idx       = str(data.get('button_index'))
        img_index = data.get('image_index')
        if idx in btn_imgs and 0 <= img_index < len(btn_imgs[idx]):
            btn_imgs[idx].pop(img_index)
            if not btn_imgs[idx]:
                del btn_imgs[idx]
            card.button_images = json.dumps(btn_imgs)
            db.session.commit()
            clear_ai_cache()
            return no_cache_json({'success': True})
        return no_cache_json({'success': False, 'message': 'Image not found'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'message': str(e)})


# -- Navigation routes --------------------------------------------------------

def _normalise_images_list(raw_images) -> list:
    if not raw_images:
        return []
    if isinstance(raw_images, str):
        try:
            raw_images = json.loads(raw_images)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(raw_images, list):
        return []
    clean = []
    base  = GOV_IMAGE_BASE.rstrip('/')
    for item in raw_images:
        if isinstance(item, dict):
            src     = (item.get('src') or item.get('filename') or item.get('url') or '').strip()
            caption = (item.get('caption') or '').strip()
        elif isinstance(item, str):
            src, caption = item.strip(), ''
        else:
            continue
        if not src:
            continue
        if src.startswith(base + '/'):
            src = src[len(base) + 1:]
        src = re.sub(r'^(static/images/|images/|static/)', '', src)
        if src:
            clean.append({'src': src, 'caption': caption})
    return clean


@app.route('/admin/api/update-nav-link', methods=['POST'])
@login_required
def update_nav_link_api():
    try:
        if request.is_json:
            d                = request.json
            link_id          = d.get('nav_id')
            text             = d.get('link_text')
            url              = d.get('link_url')
            content          = d.get('page_content', '')
            image            = d.get('image', '')
            background_image = d.get('background_image', '')
            images_raw       = d.get('images', [])
        else:
            link_id          = request.form.get('id')
            text             = request.form.get('text')
            url              = request.form.get('url')
            content          = request.form.get('content', '')
            image            = request.form.get('image', '')
            background_image = request.form.get('background_image', '')
            images_raw       = json.loads(request.form.get('images', '[]'))

        link = db.session.get(NavigationLink, link_id)
        if link:
            old_url        = link.link_url
            link.link_text = text or link.link_text
            link.link_url  = url or None
            link.page_content = content
            if image:
                link.image = image
            if background_image:
                link.background_image = background_image
            link.images = json.dumps(_normalise_images_list(images_raw))
            db.session.commit()
            clear_ai_cache()

            # Bust URL cache for old and new URLs
            for key in [old_url, f"__deep__{old_url}", url, f"__deep__{url}"]:
                if key and key in _url_cache:
                    del _url_cache[key]

            # Auto-crawl new/changed URL in background
            new_url = link.link_url
            if new_url and new_url not in ('#', ''):
                nav_id = link.id
                def _bg_crawl(u=new_url, nid=nav_id):
                    with app.app_context():
                        db.session.query(CrawledPage).filter_by(source_url=u).delete()
                        db.session.commit()
                        crawl_url_deep(u, nav_link_id=nid)
                threading.Thread(target=_bg_crawl, daemon=True).start()
                print(f"[crawler] Auto-crawl triggered for nav link: {new_url}")

            return no_cache_json({'success': True})
        return no_cache_json({'success': False, 'error': 'Not found'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/update-nav-images', methods=['POST'])
@login_required
def update_nav_images():
    try:
        data     = request.json
        nav_link = db.session.get(NavigationLink, data.get('nav_id'))
        if not nav_link:
            return no_cache_json({'success': False, 'message': 'Not found'})
        nav_link.images = json.dumps(_normalise_images_list(data.get('images', [])))
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True})
    except Exception as e:
        return no_cache_json({'success': False, 'message': str(e)})


@app.route('/admin/api/add-nav-link', methods=['POST'])
@login_required
def add_nav_link_api():
    try:
        link_text    = request.form.get('text', '').strip()
        link_url     = request.form.get('url', '').strip() or None
        page_content = request.form.get('content', '')
        image        = request.form.get('image', '').strip() or None
        if not link_text:
            return no_cache_json({'success': False, 'error': 'Link text is required'}, 400)
        max_order = db.session.query(db.func.max(NavigationLink.link_order)).scalar() or 0
        new_nav   = NavigationLink(
            link_text=link_text, link_url=link_url,
            page_content=page_content, image=image,
            images=json.dumps([]), link_order=max_order + 1,
        )
        db.session.add(new_nav)
        db.session.commit()
        clear_ai_cache()

        # Auto-crawl if a URL was provided
        if link_url and link_url not in ('#', ''):
            nav_id = new_nav.id
            def _bg_crawl(u=link_url, nid=nav_id):
                with app.app_context():
                    crawl_url_deep(u, nav_link_id=nid)
            threading.Thread(target=_bg_crawl, daemon=True).start()
            print(f"[crawler] Auto-crawl triggered for new nav link: {link_url}")

        return no_cache_json({'success': True, 'message': 'Navigation link created',
                              'nav': {'id': new_nav.id, 'link_text': new_nav.link_text,
                                      'link_url': new_nav.link_url, 'link_order': new_nav.link_order}})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)}, 500)


@app.route('/admin/api/delete-nav-link', methods=['POST'])
@login_required
def delete_nav_link_api():
    try:
        link = db.session.get(NavigationLink, request.form.get('id'))
        if link:
            # Clean up crawled pages for this nav link's URL
            if link.link_url and link.link_url not in ('#', ''):
                db.session.query(CrawledPage).filter_by(source_url=link.link_url).delete()
            db.session.delete(link)
            db.session.commit()
            clear_ai_cache()
            return no_cache_json({'success': True})
        return no_cache_json({'success': False, 'error': 'Not found'})
    except Exception as e:
        return no_cache_json({'success': False, 'error': str(e)})


# -- Professional routes ------------------------------------------------------

@app.route('/admin/api/update-professional', methods=['POST'])
@login_required
def update_professional():
    try:
        prof = db.session.get(SuggestedProfessional, int(request.form.get('id')))
        if prof:
            prof.name        = request.form.get('name') or prof.name
            prof.title       = request.form.get('title', '')
            prof.description = request.form.get('description', '')
            db.session.commit()
            clear_ai_cache()
            return no_cache_json({'success': True})
        return no_cache_json({'success': False, 'error': 'Not found'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


# -- Deep Crawl admin routes --------------------------------------------------

@app.route('/admin/api/crawl-nav-links', methods=['POST'])
@login_required
def trigger_crawl():
    """Start a full deep crawl of all nav link URLs and card button URLs in the background."""
    def run():
        with app.app_context():
            crawl_all_nav_links()
    threading.Thread(target=run, daemon=True).start()
    return no_cache_json({
        'success': True,
        'message': 'Deep crawl started in background. Indexes all nav links and card button URLs. Check status after 1-2 minutes.'
    })


@app.route('/admin/api/crawl-single', methods=['POST'])
@login_required
def crawl_single_url():
    """Crawl a single URL on demand."""
    data = request.get_json(silent=True) or {}
    url  = (data.get('url') or '').strip()
    if not url or url in ('#', ''):
        return no_cache_json({'success': False, 'error': 'URL is required'}, 400)

    def _run(u=url):
        with app.app_context():
            db.session.query(CrawledPage).filter_by(source_url=u).delete()
            db.session.commit()
            crawl_url_deep(u)

    threading.Thread(target=_run, daemon=True).start()
    return no_cache_json({'success': True, 'message': f'Crawl started for {url}'})


@app.route('/admin/api/crawl-status')
@login_required
def crawl_status():
    """Return crawl statistics."""
    total    = db.session.query(CrawledPage).count()
    by_depth = db.session.query(CrawledPage.depth, db.func.count(CrawledPage.id))\
                         .group_by(CrawledPage.depth).all()
    latest   = db.session.query(CrawledPage)\
                         .order_by(CrawledPage.crawled_at.desc()).limit(5).all()
    return no_cache_json({
        'total_pages_indexed': total,
        'by_depth': {str(d): c for d, c in by_depth},
        'latest': [{'url': p.page_url, 'title': p.page_title,
                    'depth': p.depth, 'crawled_at': p.crawled_at.isoformat()} for p in latest],
    })


@app.route('/admin/api/clear-crawl-data', methods=['POST'])
@login_required
def clear_crawl_data():
    """Wipe all crawled pages."""
    deleted = db.session.query(CrawledPage).delete()
    db.session.commit()
    return no_cache_json({'success': True, 'deleted': deleted})


# -- Analytics routes ---------------------------------------------------------

def _get_ga4_client():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    import json as _json

    token_path = os.path.join(basedir, 'ga4_token.json')
    with open(token_path) as f:
        token_data = _json.load(f)
    creds = Credentials(
        token=token_data['token'], refresh_token=token_data['refresh_token'],
        token_uri=token_data['token_uri'], client_id=token_data['client_id'],
        client_secret=token_data['client_secret'], scopes=token_data['scopes'],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_data['token'] = creds.token
        with open(token_path, 'w') as f:
            _json.dump(token_data, f, indent=2)
    return BetaAnalyticsDataClient(credentials=creds)


def _ga4_mv(row, index):
    try:
        return row.metric_values[index].value
    except (IndexError, AttributeError):
        return "0"


def _ga4_pct_change(current, previous):
    try:
        c, p = float(current or 0), float(previous or 0)
        return 0 if p == 0 else round((c - p) / p * 100, 1)
    except (ValueError, TypeError):
        return 0


@app.route('/admin/api/analytics')
@login_required
def admin_analytics():
    if not GA4_AVAILABLE:
        return no_cache_json({'ok': False, 'error': 'google-analytics-data not installed.'}, 500)
    if not GA4_PROPERTY_ID:
        return no_cache_json({'ok': False, 'error': 'GA4_PROPERTY_ID not set.'}, 500)
    if not os.path.exists(os.path.join(basedir, GA4_CREDENTIALS_FILE)):
        return no_cache_json({'ok': False, 'error': 'GA4 credentials file not found.'}, 500)
    try:
        client = _get_ga4_client()
        prop   = f"properties/{GA4_PROPERTY_ID}"

        summary_resp = client.run_report(RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date="28daysAgo", end_date="today")],
            metrics=[Metric(name="activeUsers"), Metric(name="screenPageViews"),
                     Metric(name="sessions"), Metric(name="bounceRate"),
                     Metric(name="averageSessionDuration")]))
        s_row = summary_resp.rows[0] if summary_resp.rows else None

        compare_resp = client.run_report(RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date="28daysAgo", end_date="today"),
                         DateRange(start_date="56daysAgo", end_date="29daysAgo")],
            metrics=[Metric(name="activeUsers"), Metric(name="screenPageViews")]))
        curr_users = compare_resp.rows[0].metric_values[0].value if compare_resp.rows else "0"
        prev_users = compare_resp.rows[1].metric_values[0].value if len(compare_resp.rows) > 1 else "0"
        curr_views = compare_resp.rows[0].metric_values[1].value if compare_resp.rows else "0"
        prev_views = compare_resp.rows[1].metric_values[1].value if len(compare_resp.rows) > 1 else "0"

        daily_resp = client.run_report(RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date="6daysAgo", end_date="today")],
            dimensions=[Dimension(name="date")], metrics=[Metric(name="screenPageViews")],
            order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))]))
        daily_data = [{"date": r.dimension_values[0].value, "views": r.metric_values[0].value}
                      for r in daily_resp.rows]

        sources_resp = client.run_report(RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date="28daysAgo", end_date="today")],
            dimensions=[Dimension(name="sessionDefaultChannelGroup")],
            metrics=[Metric(name="sessions")],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)]))
        sources_data = [{"source": r.dimension_values[0].value, "sessions": r.metric_values[0].value}
                        for r in sources_resp.rows[:6]]

        pages_resp = client.run_report(RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date="28daysAgo", end_date="today")],
            dimensions=[Dimension(name="pagePath")],
            metrics=[Metric(name="screenPageViews"), Metric(name="averageSessionDuration")],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)]))
        pages_data = [{"path": r.dimension_values[0].value, "views": r.metric_values[0].value,
                       "avg_time": r.metric_values[1].value} for r in pages_resp.rows[:8]]

        raw_bounce = float(_ga4_mv(s_row, 3)) if s_row else 0.0
        bounce_pct = round(raw_bounce * 100, 1) if raw_bounce <= 1 else round(raw_bounce, 1)

        return no_cache_json({
            "ok": True,
            "summary": {
                "users":       _ga4_mv(s_row, 0) if s_row else "0",
                "page_views":  _ga4_mv(s_row, 1) if s_row else "0",
                "sessions":    _ga4_mv(s_row, 2) if s_row else "0",
                "bounce_rate": str(bounce_pct),
                "avg_session": _ga4_mv(s_row, 4) if s_row else "0",
                "users_delta": _ga4_pct_change(curr_users, prev_users),
                "views_delta": _ga4_pct_change(curr_views, prev_views),
            },
            "daily": daily_data, "sources": sources_data, "pages": pages_data,
        })
    except Exception as e:
        app.logger.error(f"GA4 analytics error: {e}")
        return no_cache_json({'ok': False, 'error': str(e)}, 500)


# ---------------------------------------------------------------------------
#  PAGE BUILDER ROUTES
# ---------------------------------------------------------------------------

@app.route('/admin/api/pages', methods=['GET'])
@login_required
def get_pages():
    try:
        pages = db.session.query(Page).order_by(Page.page_order).all()
        return no_cache_json({'success': True, 'pages': [{
            'id': p.id, 'title': p.title, 'slug': p.slug,
            'layout_template': p.layout_template, 'is_published': p.is_published,
            'blocks_count': len(p.blocks),
        } for p in pages]})
    except Exception as e:
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/save-page', methods=['POST'])
@login_required
def save_page_builder():
    try:
        data         = request.get_json()
        title        = data.get('title', '')
        slug         = data.get('slug', '')
        is_published = data.get('is_published', False)
        blocks       = data.get('blocks', [])
        if not title or not slug:
            return no_cache_json({'success': False, 'error': 'Title and slug required'})
        slug = ''.join(c for c in slug.lower().replace(' ', '-') if c.isalnum() or c == '-')
        page = db.session.query(Page).filter_by(slug=slug).first()
        if page:
            page.title        = title
            page.description  = data.get('description', '')
            page.is_published = is_published
            PageBlock.query.filter_by(page_id=page.id).delete()
        else:
            max_order = db.session.query(db.func.max(Page.page_order)).scalar() or 0
            page = Page(title=title, slug=slug, description=data.get('description', ''),
                        is_published=is_published, layout_template='content_blocks',
                        page_order=max_order + 1)
            db.session.add(page)
            db.session.flush()
        for idx, block_data in enumerate(blocks):
            block_info  = block_data.get('data', {})
            block_type  = block_data.get('type', 'text')
            content_obj = block_info.get('content', {})
            if block_type == 'text':
                content, heading = content_obj.get('text', ''), content_obj.get('title', '')
            elif block_type == 'image':
                content, heading = content_obj.get('image', ''), content_obj.get('caption', '')
            elif block_type == 'button':
                content, heading = content_obj.get('buttonLink', '#'), content_obj.get('buttonText', 'Button')
            elif block_type == 'divider':
                content, heading = '', ''
            else:
                content, heading = json.dumps(content_obj), content_obj.get('title', '')
            db.session.add(PageBlock(page_id=page.id, block_type=block_type,
                                     block_order=idx, content=content, heading=heading))
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True, 'message': 'Page saved',
                              'page': {'id': page.id, 'title': page.title,
                                       'slug': page.slug, 'is_published': page.is_published}})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/pages', methods=['POST'])
@login_required
def create_page():
    try:
        data  = request.get_json()
        title = data.get('title', '')
        if not title:
            return no_cache_json({'success': False, 'error': 'Title required'})
        slug = ''.join(c for c in title.lower().replace(' ', '-') if c.isalnum() or c == '-')
        if db.session.query(Page).filter_by(slug=slug).first():
            slug = f"{slug}-{''.join(secrets.choice(string.ascii_lowercase) for _ in range(4))}"
        page = Page(title=title, slug=slug,
                    layout_template=data.get('layout_template', 'content_blocks'),
                    page_order=db.session.query(Page).count())
        db.session.add(page)
        db.session.commit()
        return no_cache_json({'success': True, 'page': {'id': page.id, 'title': page.title, 'slug': page.slug}})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/pages/<int:page_id>', methods=['GET'])
@login_required
def get_page(page_id):
    try:
        page = db.session.get(Page, page_id)
        if not page:
            return no_cache_json({'success': False, 'error': 'Not found'})
        return no_cache_json({'success': True, 'page': {
            'id': page.id, 'title': page.title, 'slug': page.slug,
            'description': page.description, 'layout_template': page.layout_template,
            'is_published': page.is_published,
            'blocks': [{
                'id': b.id, 'type': b.block_type, 'order': b.block_order,
                'content': b.content, 'heading': b.heading, 'subheading': b.subheading,
                'image_url': b.image_url, 'image_alt_text': b.image_alt_text,
                'image_caption': b.image_caption, 'card_title': b.card_title,
                'card_description': b.card_description, 'card_image': b.card_image,
                'card_buttons': b.get_card_buttons(), 'background_color': b.background_color,
                'text_alignment': b.text_alignment, 'layout_columns': b.layout_columns,
                'is_visible': b.is_visible,
            } for b in page.get_blocks_ordered()],
        }})
    except Exception as e:
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/pages/<int:page_id>', methods=['PUT'])
@login_required
def update_page(page_id):
    try:
        page = db.session.get(Page, page_id)
        if not page:
            return no_cache_json({'success': False, 'error': 'Not found'})
        data = request.get_json()
        for f in ('title', 'description', 'layout_template', 'is_published'):
            if f in data:
                setattr(page, f, data[f])
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/pages/<int:page_id>', methods=['DELETE'])
@login_required
def delete_page(page_id):
    try:
        page = db.session.get(Page, page_id)
        if not page:
            return no_cache_json({'success': False, 'error': 'Not found'})
        db.session.delete(page)
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/blocks', methods=['POST'])
@login_required
def create_block():
    try:
        data  = request.get_json()
        page  = db.session.get(Page, data.get('page_id'))
        if not page:
            return no_cache_json({'success': False, 'error': 'Page not found'})
        block = PageBlock(page_id=page.id, block_type=data.get('block_type', 'text'),
                          block_order=len(page.blocks))
        db.session.add(block)
        db.session.commit()
        return no_cache_json({'success': True, 'block': {'id': block.id, 'type': block.block_type}})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/blocks/<int:block_id>', methods=['PUT'])
@login_required
def update_block(block_id):
    try:
        block = db.session.get(PageBlock, block_id)
        if not block:
            return no_cache_json({'success': False, 'error': 'Not found'})
        data  = request.get_json()
        for f in ('content', 'heading', 'subheading', 'image_url', 'image_alt_text',
                  'image_caption', 'card_title', 'card_description', 'card_image',
                  'background_color', 'text_alignment', 'layout_columns', 'is_visible'):
            if f in data:
                setattr(block, f, data[f])
        if 'card_buttons' in data:
            block.card_buttons = json.dumps(data['card_buttons'])
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/blocks/<int:block_id>', methods=['DELETE'])
@login_required
def delete_block(block_id):
    try:
        block = db.session.get(PageBlock, block_id)
        if not block:
            return no_cache_json({'success': False, 'error': 'Not found'})
        db.session.delete(block)
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/blocks/reorder', methods=['POST'])
@login_required
def reorder_blocks():
    try:
        for item in request.get_json().get('block_orders', []):
            block = db.session.get(PageBlock, item['block_id'])
            if block:
                block.block_order = item['order']
        db.session.commit()
        return no_cache_json({'success': True})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


# ---------------------------------------------------------------------------
#  DEBUG
# ---------------------------------------------------------------------------

@app.route('/debug-upload')
def debug_upload():
    upload_folder = app.config.get('UPLOAD_FOLDER', 'NOT SET')
    static_images = os.path.join(app.root_path, 'static', 'images')
    try:
        files = os.listdir(upload_folder)[-5:] if os.path.exists(upload_folder) else []
    except Exception:
        files = []
    crawled_count = db.session.query(CrawledPage).count()
    return jsonify({
        'GOV_IMAGE_BASE':      GOV_IMAGE_BASE,
        'UPLOAD_FOLDER':       upload_folder,
        'folder_exists':       os.path.exists(upload_folder),
        'are_same':            os.path.abspath(upload_folder) == os.path.abspath(static_images),
        'recent_files':        files,
        'url_cache_size':      len(_url_cache),
        'crawled_pages_in_db': crawled_count,
        'ga4_available':       GA4_AVAILABLE,
        'ga4_property_set':    bool(GA4_PROPERTY_ID),
    })


# ---------------------------------------------------------------------------
#  DB INIT
# ---------------------------------------------------------------------------

def init_db():
    with app.app_context():
        db.create_all()
        if not Admin.query.filter_by(username='admin').first():
            admin = Admin(username='admin')
            admin.set_password('admin123')
            db.session.add(admin)

        default_content = [
            ('hero_title',           'LEADING THE MOVEMENT IN <br>ADVANCING INNOVATION AND <br>PRODUCTIVITY IN THE <span class="text-[#cdae2c]">PUBLIC SECTOR</span>', 1),
            ('hero_image',           'images/Hero-Banner.png', 2),
            ('search_placeholder',   'Ask Tutoy anything about Public Sector Productivity?', 3),
            ('loading_text',         'Generating...', 4),
            ('ai_label',             'Claude says', 5),
            ('key_points_title',     'Key Points', 6),
            ('related_topics_title', 'Related Topics', 7),
            ('quick_actions_title',  'Quick Actions', 8),
            ('logo_image',           'images/dap-logo.png', 9),
            ('gemini_image',         'images/gemini.png', 10),
            ('apo_logo',             'images/apo.png', 11),
            ('company_name',         'Development Academy of The Philippines', 12),
            ('company_subtitle',     'Center of Excellence on Public Sector Productivity', 13),
            ('company_address',      'DAP Building, San Miguel Avenue, Pasig City 1500', 14),
            ('company_phone',        '+632 631 0921 to 30', 15),
            ('company_fax',          '+632 631 2123', 16),
            ('company_email',        'coe_psp@dap.edu.ph', 17),
            ('site_title',           'COE Public-Sector Productivity', 18),
        ]
        for key, value, order in default_content:
            if not db.session.query(ContentSection).filter_by(content_key=key).first():
                db.session.add(ContentSection(content_key=key, content_value=value, section_order=order))

        default_cards = [
            (1, 'Whats New?',            'slide1.png', ['Trainings and Capacity Development', 'Knowledge Products', 'Community']),
            (2, 'Productivity Challenge', 'slide2.jpg', ['2025 Paper-Less', 'Previous Challenge', 'Submit an Entry']),
            (3, 'Governance Lab',         'govlab.jpg', ['About Us', 'What is GovLab?', 'Join Us']),
        ]
        for card_id, title, image, buttons in default_cards:
            if not db.session.get(Card, card_id):
                db.session.add(Card(id=card_id, title=title, image=image,
                                    buttons=json.dumps(buttons), card_order=card_id))

        nav_links_data = ['About Us', 'Whats New', 'Trainings', 'Conferences', 'Community',
                          'Knowledge Products', 'Productivity Challenge', 'GovLab', 'NextGenPh']
        for i, link_text in enumerate(nav_links_data):
            if not db.session.query(NavigationLink).filter_by(link_text=link_text).first():
                db.session.add(NavigationLink(link_text=link_text, link_url='#', link_order=i + 1))
        seed_programs = [
            (
                'Development of Public-Sector Productivity Specialists (Foundation Course)',
                'dpsps-foundation-course',
                'Equip technical staff and officers with knowledge and skills in measurement, '
                'analysis, planning, and troubleshooting to increase organizational productivity.',
                'The DPSPS-FC is a training program designed to equip technical staff and officers '
                'of Management Division and related offices of public-sector organizations (PSO) '
                'with the knowledge and skills in measurement, analysis, planning, and '
                'troubleshooting to increase their respective organizations\' productivity.',
                1,
            ),
            (
                'Designing Citizen-Centered Public Services',
                'designing-citizen-centered-public-services',
                'Assist government agencies in developing solutions so that their services address '
                'their clients\' needs and expectations.',
                'The program aims to assist government agencies in developing solutions so that '
                'their services address their clients\' needs and expectations. The discussions and '
                'activities in this program offer participating agencies a different approach to '
                'streamlining and process improvement by viewing services from the point of view '
                'or perspective of your clients or constituents.',
                2,
            ),
            (
                'Public Service Value Chain',
                'public-service-value-chain',
                'Enable government agencies to analyze, map, and improve end-to-end organizational '
                'processes to enhance efficiency, effectiveness, and public value delivery.',
                'The PSVC is a capability-building intervention that enables government agencies to '
                'analyze, map, and improve their end-to-end organizational processes, including '
                'management, core, and support functions, to enhance efficiency, effectiveness, and '
                'the delivery of public value to citizens and stakeholders.',
                3,
            ),
        ]
        for title, slug, excerpt, body, order in seed_programs:
            if not TrainingProgram.query.filter_by(slug=slug).first():
                db.session.add(TrainingProgram(
                    title         = title,
                    slug          = slug,
                    excerpt       = excerpt,
                    body          = body,
                    program_order = order,
                    is_published  = True,
                    is_archived   = False,
                ))

        db.session.commit()

        # Start periodic background re-crawl
        _schedule_recrawl()

@app.route('/admin/api/delete-single-image', methods=['POST'])
@login_required
def delete_single_image():
    """Delete a single uploaded image file from disk."""
    try:
        data     = request.get_json(silent=True) or {}
        filename = (data.get('filename') or '').strip()
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return no_cache_json({'success': False, 'error': 'Invalid filename'}), 400
 
        upload_folder = app.config.get('UPLOAD_FOLDER', '')
        file_path     = os.path.join(upload_folder, filename)
 
        if os.path.exists(file_path):
            os.remove(file_path)
            return no_cache_json({'success': True, 'message': f'{filename} deleted'})
        return no_cache_json({'success': True, 'message': 'File not found (already deleted)'})
    except Exception as e:
        return no_cache_json({'success': False, 'error': str(e)}), 500

# Add this route to your Render-deployed app
@app.route('/proxy/claude', methods=['POST'])
def proxy_claude():
    import anthropic
    
    data = request.get_json()
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    
    if not api_key:
        return jsonify({'error': 'No API key'}), 500
    
    # Simple secret to prevent abuse
    secret = request.headers.get('X-Proxy-Secret', '')
    if secret != os.environ.get('PROXY_SECRET', ''):
        return jsonify({'error': 'Unauthorized'}), 401

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=data.get('model', 'claude-sonnet-4-6'),
        max_tokens=data.get('max_tokens', 512),
        system=data.get('system', ''),
        messages=data.get('messages', [])
    )
    return jsonify({'content': [{'text': message.content[0].text}]})


if __name__ == '__main__':
    init_db()
    app.run(host="0.0.0.0", port=8000, debug=False)
