import os
from dotenv import load_dotenv

# Load .env FIRST (use absolute path for production reliability)
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, ".env"))
# In your main Flask app (app.py)
import subprocess
import threading

# Run upload_receiver in a background thread
def start_upload_receiver():
    subprocess.Popen(['python', 'upload_receiver.py'])

threading.Thread(target=start_upload_receiver, daemon=True).start()

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, make_response
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from config import Config
from models import db, ContentSection, Card, NavigationLink, FooterSection, Admin, SuggestedProfessional, Page, PageBlock
from werkzeug.utils import secure_filename
import json
from datetime import datetime
import requests
import hashlib
import re
import time
import secrets
import string

# Initialize Flask
app = Flask(__name__)
app.config.from_object(Config)

# ✅ Detect environment — cache only works reliably on local dev
IS_PRODUCTION = os.environ.get('FLASK_ENV', 'production') == 'production'

# ✅ Gov.ph upload server config (set these in Render environment variables)
GOV_UPLOAD_URL = os.environ.get('GOV_UPLOAD_URL', 'http://coe-psp.dap.gov.ph:5001/upload_receiver')  # local dev
# For production, set GOV_UPLOAD_URL in environment variables to the public URL with port
GOV_IMAGE_BASE   = os.environ.get('GOV_IMAGE_BASE',   'http://coe-psp.dap.gov.ph/static/images')
GOV_UPLOAD_TOKEN = os.environ.get('GOV_UPLOAD_TOKEN', '')

# Debug: Verify API key is loaded
api_key_check = os.getenv('GROQ_API_KEY', 'NOT FOUND')
print(f"\n[DEBUG] GROQ_API_KEY from environment: {'LOADED' if api_key_check != 'NOT FOUND' else 'NOT FOUND'}")
if api_key_check != 'NOT FOUND':
    print(f"[DEBUG] API Key starts with: {api_key_check[:10]}...")
print(f"[DEBUG] IS_PRODUCTION: {IS_PRODUCTION}")
print(f"[DEBUG] GOV_UPLOAD_URL: {GOV_UPLOAD_URL}")
print(f"[DEBUG] GOV_IMAGE_BASE: {GOV_IMAGE_BASE}")
print()

# Initialize extensions
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'admin_login'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Admin, int(user_id))

# Custom Jinja2 filters
@app.template_filter('fromjson')
def fromjson_filter(value):
    """Parse JSON string into Python object"""
    try:
        if isinstance(value, str):
            return json.loads(value)
        return value
    except (json.JSONDecodeError, TypeError):
        return []

# Create upload folder if it doesn't exist (for local dev only)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ---------------------------------------------
# Helper utilities
# ---------------------------------------------

def escape_html(text):
    if not text:
        return text
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#39;')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def get_content(key, default=''):
    content = db.session.query(ContentSection).filter_by(content_key=key).first()
    return content.content_value if content else default

def get_all_content():
    return db.session.query(ContentSection).order_by(ContentSection.section_order).all()

def get_all_cards():
    return db.session.query(Card).order_by(Card.card_order).all()

def get_all_nav_links():
    return db.session.query(NavigationLink).order_by(NavigationLink.link_order).all()

def get_all_professionals():
    return db.session.query(SuggestedProfessional).order_by(SuggestedProfessional.professional_order).all()

def clear_ai_cache():
    """Delete all cached AI responses so the next search hits fresh DB data."""
    cache_dir = os.path.join(app.root_path, 'cache')
    if os.path.exists(cache_dir):
        for f in os.listdir(cache_dir):
            if f.startswith('ai_') and f.endswith('.json'):
                try:
                    os.remove(os.path.join(cache_dir, f))
                except OSError:
                    pass

def no_cache_json(data, status=200):
    """Return a JSON response with cache-busting headers."""
    resp = make_response(jsonify(data), status)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

def is_greeting(query):
    greetings = [
        'hello', 'hi', 'hey', 'greetings', 'good morning', 'good afternoon',
        'good evening', 'howdy', 'hola', 'bonjour', 'kamusta', 'kumusta',
        'what are you', 'who are you', 'introduce yourself'
    ]
    q = query.lower().strip()
    for greeting in greetings:
        if q == greeting or q.startswith(greeting + ' ') or q.startswith(greeting + '!'):
            return True
    return False


# -----------------------------------------------------------------------------
#  IMAGE URL HELPER
#  ✅ FIX: Now builds full URLs pointing to the gov.ph static server.
#         Handles both legacy bare filenames and already-full URLs gracefully.
# -----------------------------------------------------------------------------

def _image_url(filename: str) -> str:
    """
    Convert a stored filename/path to a fully-qualified URL served from the
    gov.ph static server.

    Handles:
      - Already full URLs  →  returned as-is
      - 'images/foo.png'   →  strips prefix, builds full URL
      - 'static/foo.png'   →  strips prefix, builds full URL
      - 'foo.png'          →  builds full URL directly
    """
    if not filename:
        return ''
    filename = filename.strip()
    # Already a full URL — return unchanged
    if filename.startswith('http://') or filename.startswith('https://'):
        return filename
    # Strip any legacy path prefixes, keep just the bare filename
    bare = filename
    for prefix in ('static/images/', 'images/', 'static/'):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break
    return f"{GOV_IMAGE_BASE}/{bare}"


# -----------------------------------------------------------------------------
#  ENHANCED SEARCH ENGINE
# -----------------------------------------------------------------------------

SYNONYMS = {
    'training':       ['capacity development', 'learning', 'course', 'seminar', 'workshop', 'program'],
    'course':         ['training', 'program', 'seminar', 'workshop'],
    'program':        ['training', 'course', 'initiative', 'project'],
    'new':            ['latest', 'recent', 'update', 'news', 'announcement'],
    'govlab':         ['governance lab', 'innovation lab', 'lab'],
    'challenge':      ['competition', 'contest', 'submit', 'entry'],
    'productivity':   ['efficiency', 'performance', 'output'],
    'public sector':  ['government', 'agency', 'bureau', 'office'],
    'join':           ['register', 'enroll', 'membership', 'participate'],
    'about':          ['overview', 'mission', 'vision', 'background'],
    'community':      ['network', 'group', 'members', 'professionals'],
    'knowledge':      ['research', 'publication', 'study', 'resource', 'learning'],
    'digital':        ['technology', 'innovation', 'paperless', 'e-government'],
    'paperless':      ['digital', 'paper-less', 'e-document', 'technology'],
    'nextgen':        ['next generation', 'youth', 'leaders', 'future'],
    'enroll':         ['register', 'join', 'sign up', 'apply'],
    'conference':     ['summit', 'forum', 'congress', 'event', 'symposium'],
    'innovation':     ['digital', 'technology', 'improvement', 'reform'],
    'dap':            ['development academy', 'development academy of the philippines'],
    'coe':            ['center of excellence', 'excellence center'],
    'seminar':        ['training', 'workshop', 'webinar', 'session'],
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
    words = re.findall(r"[a-zA-Z0-9']+", text.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


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


def _extract_snippet(haystack: str, tokens: list, window: int = 130) -> str:
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
    start   = max(0, best_pos - 40)
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


# ---------------------------------------------
# Main DB search
# ---------------------------------------------

def search_website_content(query: str) -> dict:
    results = {
        'suggestions':   [],
        'images':        [],
        'related_links': [],
        'context_text':  '',
    }

    if not query or not query.strip():
        return results

    tokens   = _tokenise(query)
    if not tokens:
        return results
    expanded = _expand_keywords(tokens)
    matches  = []

    try:
        for card in db.session.query(Card).all():
            title_score   = _score_text(tokens, expanded, card.title or '')
            btn_contents  = _safe_json_dict(card.button_contents)
            btn_images    = _safe_json_dict(card.button_images)
            btn_links     = _safe_json_dict(card.button_links)
            buttons_list  = _safe_json_list(card.buttons)
            content_blob  = ' '.join(str(v) for v in btn_contents.values())
            content_score = _score_text(tokens, expanded, content_blob)
            overall       = max(title_score, content_score * 0.85)
            if overall < 0.1:
                continue
            snippet = _extract_snippet(content_blob or card.title or '', tokens)
            card_images = []
            if card.image:
                card_images.append({'url': _image_url(card.image), 'alt_text': card.title, 'source': card.title})
            for btn_idx, btn_img_raw in btn_images.items():
                if isinstance(btn_img_raw, list):
                    for img_entry in btn_img_raw:
                        fname   = img_entry.get('filename', '') if isinstance(img_entry, dict) else str(img_entry)
                        caption = (img_entry.get('caption', '') if isinstance(img_entry, dict) else '') or card.title
                        if fname:
                            card_images.append({'url': _image_url(fname), 'alt_text': caption, 'source': card.title})
                elif isinstance(btn_img_raw, str) and btn_img_raw:
                    card_images.append({'url': _image_url(btn_img_raw), 'alt_text': card.title, 'source': card.title})
            card_links = []
            for btn_idx, link_url in btn_links.items():
                if link_url:
                    try:
                        label = buttons_list[int(btn_idx)]
                    except (ValueError, IndexError):
                        label = 'View'
                    card_links.append({'title': label, 'url': link_url, 'context': card.title})
            matches.append({
                'score': overall, 'record_type': 'Card', 'record_id': card.id,
                'record_title': card.title, 'snippet': snippet,
                'images': card_images, 'links': card_links,
            })

        for nav in db.session.query(NavigationLink).all():
            title_score   = _score_text(tokens, expanded, nav.link_text or '')
            content_score = _score_text(tokens, expanded, nav.page_content or '')
            overall       = max(title_score, content_score * 0.85)
            if overall < 0.1:
                continue
            snippet = _extract_snippet(nav.page_content or nav.link_text or '', tokens)
            nav_images = []
            for img_entry in _safe_json_list(nav.images):
                if isinstance(img_entry, dict):
                    fname   = img_entry.get('filename', '')
                    caption = img_entry.get('caption', nav.link_text)
                elif isinstance(img_entry, str):
                    fname   = img_entry
                    caption = nav.link_text
                else:
                    continue
                if fname:
                    nav_images.append({'url': _image_url(fname), 'alt_text': caption, 'source': nav.link_text})
            nav_links_out = []
            if nav.link_url and nav.link_url not in ('#', ''):
                nav_links_out.append({'title': nav.link_text, 'url': nav.link_url, 'context': nav.link_text})
            else:
                nav_links_out.append({'title': nav.link_text, 'url': f'/nav-page/{nav.id}', 'context': nav.link_text})
            for url in re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', nav.page_content or ''):
                nav_links_out.append({
                    'title':   url.replace('https://', '').replace('http://', '').split('/')[0],
                    'url':     url, 'context': nav.link_text,
                })
            matches.append({
                'score': overall, 'record_type': 'NavigationLink', 'record_id': nav.id,
                'record_title': nav.link_text, 'snippet': snippet,
                'images': nav_images, 'links': nav_links_out,
            })

        for cs in db.session.query(ContentSection).all():
            score = _score_text(tokens, expanded, cs.content_value or '')
            if score < 0.2:
                continue
            matches.append({
                'score': score, 'record_type': 'ContentSection', 'record_id': cs.id,
                'record_title': cs.content_key.replace('_', ' ').title(),
                'snippet': _extract_snippet(cs.content_value or '', tokens),
                'images': [], 'links': [],
            })

        matches.sort(key=lambda x: x['score'], reverse=True)
        seen_images, seen_links = set(), set()
        context_parts = []
        for m in matches[:8]:
            results['suggestions'].append({
                'text':         f"{m['record_title']}: {m['snippet']}",
                'source':       'website_content',
                'confidence':   round(m['score'], 3),
                'record_type':  m['record_type'],
                'record_id':    m['record_id'],
                'record_title': m['record_title'],
            })
            for img in m['images']:
                if img['url'] and img['url'] not in seen_images and len(results['images']) < 6:
                    seen_images.add(img['url'])
                    results['images'].append(img)
            for lnk in m['links']:
                if lnk['url'] and lnk['url'] not in seen_links and len(results['related_links']) < 8:
                    seen_links.add(lnk['url'])
                    results['related_links'].append(lnk)
            context_parts.append(
                f"[{m['record_type']}] {m['record_title']} (score {m['score']:.2f}): {m['snippet']}"
            )
        results['context_text'] = '\n'.join(context_parts)
        app.logger.info(
            f"DB search '{query}': {len(matches)} candidates -> "
            f"{len(results['suggestions'])} suggestions, {len(results['images'])} images"
        )
    except Exception as e:
        app.logger.error(f"search_website_content error: {e}")

    return results


# ---------------------------------------------
# Groq API
# ✅ FIX 1: Cache disabled on production (Render ephemeral filesystem)
# ✅ FIX 2: System prompt no longer caps response at 200 words
# ---------------------------------------------

def call_groq_api(query: str, website_search: dict = None) -> dict:
    api_key = app.config.get('GROQ_API_KEY', '') or os.environ.get('GROQ_API_KEY', '')
    if not api_key:
        print("[v0] ERROR: GROQ_API_KEY not configured")
        return None

    print(f"[v0] Calling Groq API for query: {query}")

    # ✅ Only use file cache in local development.
    USE_CACHE = not IS_PRODUCTION
    cache_file = None

    if USE_CACHE:
        cache_dir  = os.path.join(app.root_path, 'cache')
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, f"ai_{hashlib.md5(query.encode()).hexdigest()}.json")
        if os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file) < 3600):
            try:
                with open(cache_file, 'r') as f:
                    cached = json.load(f)
                print(f"[v0] Cache hit")
                return cached
            except Exception:
                pass

    if website_search is None:
        website_search = search_website_content(query)

    context_text   = website_search.get('context_text', '')
    has_db_content = bool(context_text.strip())

    system_prompt = (
        "You are Tutoy, the official AI assistant for DAP-COE "
        "(Development Academy of the Philippines – Center of Excellence on Public Sector Productivity). "
        "RULES: "
        "1. Respond with VALID JSON ONLY – no markdown, no backticks, no extra text. "
        "2. Prioritise information from the 'Website Context' section when available. "
        "3. If website context is empty or off-topic, use general knowledge about public sector "
        "   productivity, Philippine governance, and DAP programs. "
        "4. gemini_says: write a clear, helpful, and detailed response between 150 and 300 words. "
        "   Be thorough and informative – do not truncate or summarise too briefly. "
        "5. key_points: exactly 4 strings, each a concise but meaningful bullet point. "
        "6. global_suggestions: exactly 3 strings representing useful follow-up questions. "
        "7. Leave 'image' as empty string '' – images are handled separately."
    )

    context_block = (
        f"\n\n=== Website Context (prefer this) ===\n{context_text}\n=== End Context ==="
        if has_db_content
        else "\n\n(No specific website content matched – use general knowledge.)"
    )

    user_prompt = (
        f"User query: {query}\n"
        f"{context_block}\n\n"
        "Respond with ONLY valid JSON:\n"
        '{\n'
        '  "gemini_says": "...",\n'
        '  "key_points": ["...", "...", "...", "..."],\n'
        '  "image": "",\n'
        '  "global_suggestions": ["...", "...", "..."],\n'
        '  "quick_navigation": ["Home", "Programs", "Services", "Contact"],\n'
        '  "related_links": []\n'
        '}'
    )

    payload = {
        "model":           "llama-3.3-70b-versatile",
        "messages":        [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature":     0.3,
        "max_tokens":      1024,
        "top_p":           0.95,
        "response_format": {"type": "json_object"},
    }

    try:
        url     = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        print("[v0] Making request to Groq API...")
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        print(f"[v0] Response status: {resp.status_code}")

        if resp.status_code != 200:
            print(f"[v0] Groq error {resp.status_code}: {resp.text[:400]}")
            return None

        data    = resp.json()
        choices = data.get('choices', [])
        if not choices:
            print("[v0] No choices in Groq response")
            return None

        text = choices[0].get('message', {}).get('content', '').strip()
        if not text:
            print("[v0] Empty content from Groq")
            return None

        print(f"[v0] Groq response (first 200): {text[:200]}")

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
            print(f"[v0] Could not parse Groq JSON. Raw: {text[:500]}")
            return None

        result = {
            'gemini_says':                str(parsed.get('gemini_says', '')).strip() or 'No response available.',
            'key_points':                 parsed.get('key_points', []) or [],
            'image':                      parsed.get('image', '') or '',
            'global_suggestions':         parsed.get('global_suggestions', []) or [],
            'quick_navigation':           parsed.get('quick_navigation', []) or ['Home', 'Programs', 'Services', 'Contact'],
            'website_content_suggestions': [],
            'related_images':             website_search.get('images', [])[:6],
            'related_links':              website_search.get('related_links', [])[:8],
        }

        if USE_CACHE and cache_file:
            try:
                with open(cache_file, 'w') as f:
                    json.dump(result, f)
                print("[v0] Response cached (local dev only)")
            except Exception as e:
                print(f"[v0] Cache write error: {e}")
        else:
            print("[v0] Cache skipped (production mode)")

        return result

    except requests.RequestException as e:
        print(f"[v0] Request failed: {e}")
    except Exception as e:
        print(f"[v0] Unexpected error: {e}")
        import traceback; traceback.print_exc()

    return None


# -----------------------------------------------------------------------------
#  MAIN SITE ROUTES
# -----------------------------------------------------------------------------

@app.route('/nav-page/<int:nav_id>')
def nav_page(nav_id):
    nav_link = db.session.get(NavigationLink, nav_id)
    if not nav_link:
        return redirect(url_for('index'))
    nav_links       = get_all_nav_links()
    logo_image      = get_content('logo_image', 'images/dap-logo.png')
    published_pages = db.session.query(Page).filter_by(is_published=True).order_by(Page.page_order).all()
    return render_template('nav_page.html', nav_link=nav_link, nav_links=nav_links,
                           published_pages=published_pages, logo_image=logo_image)


@app.route('/<path:path>')
def catch_all(path):
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
    logo_image      = get_content('logo_image', 'images/dap-logo.png')
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


@app.route('/')
def index():
    now                = datetime.now()
    current_year       = now.year
    hero_title         = get_content('hero_title', 'LEADING THE MOVEMENT IN <br>ADVANCING INNOVATION AND <br>PRODUCTIVITY IN THE <span class="text-[#cdae2c]">PUBLIC SECTOR</span>')
    hero_image         = get_content('hero_image', 'images/Hero-Banner.png')
    hero_opacity       = get_content('hero_opacity', '90')
    search_placeholder = get_content('search_placeholder', 'Ask Tutoy anything about Public Sector Productivity...?')
    cards              = get_all_cards()
    nav_links          = get_all_nav_links()
    published_pages    = db.session.query(Page).filter_by(is_published=True).order_by(Page.page_order).all()
    company_name       = get_content('company_name', 'Development Academy of The Philippines')
    company_subtitle   = get_content('company_subtitle', 'Center of Excellence on Public Sector Productivity')
    company_address    = get_content('company_address', 'DAP Building, San Miguel Avenue, Pasig City 1500')
    company_phone      = get_content('company_phone', '+632 631 0921 to 30')
    company_fax        = get_content('company_fax', '+632 631 2123')
    company_email      = get_content('company_email', 'coe_psp@dap.edu.ph')
    logo_image         = get_content('logo_image', 'images/dap-logo.png')
    gemini_image       = get_content('gemini_image', 'images/gemini.png')
    apo_logo           = get_content('apo_logo', 'images/apo.png')

    context = dict(
        now=now,
        hero_title=hero_title, hero_image=hero_image, hero_opacity=hero_opacity,
        search_placeholder=search_placeholder, cards=cards, nav_links=nav_links,
        published_pages=published_pages, company_name=company_name,
        company_subtitle=company_subtitle, company_address=company_address,
        company_phone=company_phone, company_fax=company_fax, company_email=company_email,
        logo_image=logo_image, gemini_image=gemini_image, apo_logo=apo_logo,
        current_year=current_year,
    )
    resp = make_response(render_template('index.html', **context))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma']        = 'no-cache'
    resp.headers['Expires']       = '0'
    return resp


# ---------------------------------------------
# AI SEARCH ENDPOINT
# ---------------------------------------------

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
                "(Development Academy of the Philippines – Center of Excellence on Public Sector Productivity). "
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
            'quick_navigation': ['Home', 'Programs', 'Services', 'Contact'],
            'website_content_suggestions': [], 'related_images': [], 'related_links': [],
        })

    website_search = search_website_content(query)

    matched_image = ''
    for card in get_all_cards():
        if query.lower() in (card.title or '').lower():
            matched_image = _image_url(card.image)
            break
    if not matched_image and website_search.get('images'):
        matched_image = website_search['images'][0]['url']

    ai_response = call_groq_api(query, website_search=website_search)

    if ai_response:
        print("[v0] Got valid response from Groq API")
        if not ai_response.get('image') and matched_image:
            ai_response['image'] = matched_image
        ai_response['website_content_suggestions'] = []
        ai_response.setdefault('related_images', [])
        ai_response.setdefault('related_links',  [])
        return jsonify(ai_response)

    print("[v0] Groq API returned None – DB-only fallback")
    db_snippets = [s.get('text', '') for s in website_search.get('suggestions', [])[:4]]
    fallback_says = (
        f'Here\'s what I found on our website about "{query}".'
        if website_search.get('suggestions')
        else f'I couldn\'t connect to the AI service right now. Try searching "trainings", "GovLab", or "community".'
    )

    return jsonify({
        'gemini_says':                fallback_says,
        'key_points':                 db_snippets or [f'Search query: "{query}"', 'Browse our website for more information'],
        'image':                      matched_image,
        'global_suggestions':         ['Tell me about our programs', 'What services do we offer?', 'How can I enroll?'],
        'quick_navigation':           ['Home', 'Programs', 'Services', 'Contact'],
        'website_content_suggestions': [],
        'related_images':             website_search.get('images', [])[:6],
        'related_links':              website_search.get('related_links', [])[:8],
    })


# -----------------------------------------------------------------------------
#  ADMIN ROUTES
# -----------------------------------------------------------------------------

@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_panel'))
    login_error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user     = Admin.query.filter_by(username=username).first()
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
    return render_template(
        'admin_panel.html',
        content_sections=get_all_content(),
        cards=get_all_cards(),
        nav_links=get_all_nav_links(),
        professionals=get_all_professionals(),
        logo_image=get_content('logo_image', 'images/dap-logo.png'),
        GOV_IMAGE_BASE=GOV_IMAGE_BASE,   # ← NEW: used by all <img> tags in template
    )


@app.route('/admin/page-builder')
@login_required
def page_builder():
    return render_template('page_builder.html', logo_image=get_content('logo_image', 'images/dap-logo.png'))


# ---------------------------------------------
# update_content — guards against saving "undefined"/"null"/"None"
# ---------------------------------------------
@app.route('/admin/api/update-content', methods=['POST'])
@login_required
def update_content():
    try:
        if request.is_json:
            key   = request.json.get('key')
            value = request.json.get('value', '')
        else:
            key   = request.form.get('key')
            value = request.form.get('value', '')

        if not key:
            return no_cache_json({'success': False, 'error': 'Key is required'})

        value_str = str(value).strip() if value is not None else ''
        if value_str in ('undefined', 'null', 'None'):
            return no_cache_json({
                'success': False,
                'error': f'Invalid value "{value_str}" was blocked — not saved. Please enter real content.'
            })

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
        buttons               = request.form.getlist('buttons')
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


# -----------------------------------------------------------------------------
#  UPLOAD IMAGE
#  ✅ FIX: On production (Render), forward the upload to the gov.ph server
#          instead of saving locally (Render filesystem is ephemeral).
#          On local dev, save locally as before.
#
#  Required Render environment variables:
#    GOV_UPLOAD_URL   = https://coe-psp.dap.gov.ph/upload_receiver
#    GOV_IMAGE_BASE   = https://coe-psp.dap.gov.ph/static/images
#    GOV_UPLOAD_TOKEN = <shared secret matching upload_receiver.py>
# -----------------------------------------------------------------------------

@app.route('/admin/api/upload-image', methods=['POST'])
@login_required
def upload_image():
    try:
        file = request.files.get('file') or request.files.get('image')
        if not file or file.filename == '':
            return no_cache_json({'success': False, 'error': 'No file uploaded'})
        if not allowed_file(file.filename):
            return no_cache_json({'success': False, 'error': 'Invalid file type'})

        if IS_PRODUCTION:
            # ----------------------------------------------------------------
            # PRODUCTION PATH: Forward file to gov.ph upload_receiver
            # ----------------------------------------------------------------
            headers = {}
            if GOV_UPLOAD_TOKEN:
                headers['Authorization'] = f'Bearer {GOV_UPLOAD_TOKEN}'

            try:
                gov_resp = requests.post(
                    GOV_UPLOAD_URL,
                    files={'file': (file.filename, file.stream, file.mimetype)},
                    headers=headers,
                    timeout=30,
                )
            except requests.exceptions.ConnectionError as e:
                print(f"[upload] Connection error to gov.ph: {e}")
                return no_cache_json({'success': False, 'error': f'Could not reach upload server: {e}'})
            except requests.exceptions.Timeout:
                print("[upload] Timeout reaching gov.ph")
                return no_cache_json({'success': False, 'error': 'Upload server timed out. Please try again.'})

            print(f"[upload] Gov.ph response status: {gov_resp.status_code}")

            if gov_resp.status_code == 401:
                return no_cache_json({'success': False, 'error': 'Upload server rejected the request (invalid token).'})

            if gov_resp.status_code != 200:
                return no_cache_json({
                    'success': False,
                    'error': f'Upload server returned {gov_resp.status_code}: {gov_resp.text[:200]}'
                })

            try:
                gov_data = gov_resp.json()
            except Exception:
                return no_cache_json({'success': False, 'error': 'Upload server returned an invalid response.'})

            if not gov_data.get('success'):
                return no_cache_json({'success': False, 'error': gov_data.get('error', 'Upload failed on gov server.')})

            filename = gov_data['filename']
            full_url = f"{GOV_IMAGE_BASE}/{filename}"
            print(f"[upload] File saved on gov.ph as: {filename} → {full_url}")

            return no_cache_json({
                'success':  True,
                'filename': filename,
                'url':      full_url,   # Full URL for immediate use in <img> tags
            })

        else:
            # ----------------------------------------------------------------
            # LOCAL DEV PATH: Save directly to local static/images
            # ----------------------------------------------------------------
            filename = f"{int(datetime.now().timestamp())}_{secure_filename(file.filename)}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            print(f"[upload] Local save: {filename}")
            return no_cache_json({
                'success':  True,
                'filename': filename,
                'url':      f"images/{filename}",
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/update-card-title', methods=['POST'])
@login_required
def update_card_title():
    try:
        card = db.session.get(Card, int(request.form.get('id')))
        if card:
            card.title = request.form.get('title', '')
            db.session.commit()
            clear_ai_cache()
            return no_cache_json({'success': True})
        return no_cache_json({'success': False, 'error': 'Not found'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


@app.route('/admin/api/update-card-image', methods=['POST'])
@login_required
def update_card_image():
    try:
        card = db.session.get(Card, int(request.form.get('id')))
        if card:
            card.image = request.form.get('image', '')
            db.session.commit()
            clear_ai_cache()
            return no_cache_json({'success': True})
        return no_cache_json({'success': False, 'error': 'Not found'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


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
            try: return json.loads(f) if f else {}
            except: return {}

        btn_links = _load(card.button_links)
        btn_conts = _load(card.button_contents)
        btn_bgs   = _load(card.button_background_images)
        btn_imgs  = _load(card.button_images)

        link_url = data.get('link_url', '')
        if link_url: btn_links[idx] = link_url
        elif idx in btn_links: del btn_links[idx]

        content = data.get('content', '')
        if content: btn_conts[idx] = content
        elif idx in btn_conts: del btn_conts[idx]

        bg = data.get('background_image', '')
        if bg: btn_bgs[idx] = bg
        elif idx in btn_bgs: del btn_bgs[idx]

        imgs = data.get('images', [])
        if imgs: btn_imgs[idx] = imgs
        elif idx in btn_imgs: del btn_imgs[idx]

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
        data = request.json
        card = db.session.get(Card, data.get('card_id'))
        if not card:
            return no_cache_json({'success': False, 'message': 'Card not found'})
        btn_links = card.get_button_links()
        link_url  = data.get('link_url', '')
        idx       = str(data.get('button_index'))
        if link_url.strip(): btn_links[idx] = link_url.strip()
        else: btn_links.pop(idx, None)
        card.button_links = json.dumps(btn_links)
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'message': str(e)})


@app.route('/admin/api/update-button-images', methods=['POST'])
@login_required
def update_button_images():
    try:
        data = request.json
        card = db.session.get(Card, data.get('card_id'))
        if not card:
            return no_cache_json({'success': False, 'message': 'Card not found'})
        btn_imgs = card.get_button_images()
        btn_imgs[str(data.get('button_index'))] = data.get('images', [])
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


@app.route('/admin/api/update-nav-images', methods=['POST'])
@login_required
def update_nav_images():
    try:
        data     = request.json
        nav_link = db.session.get(NavigationLink, data.get('nav_id'))
        if not nav_link:
            return no_cache_json({'success': False, 'message': 'Not found'})
        nav_link.images = json.dumps(data.get('images', []))
        db.session.commit()
        clear_ai_cache()
        return no_cache_json({'success': True})
    except Exception as e:
        return no_cache_json({'success': False, 'message': str(e)})


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
            images           = d.get('images', [])
        else:
            link_id          = request.form.get('id')
            text             = request.form.get('text')
            url              = request.form.get('url')
            content          = request.form.get('content', '')
            image            = request.form.get('image', '')
            background_image = request.form.get('background_image', '')
            images           = json.loads(request.form.get('images', '[]'))
        link = db.session.get(NavigationLink, link_id)
        if link:
            link.link_text    = text or link.link_text
            link.link_url     = url or None
            link.page_content = content
            if image:            link.image            = image
            if background_image: link.background_image = background_image
            if images:           link.images           = json.dumps(images)
            db.session.commit()
            clear_ai_cache()
            return no_cache_json({'success': True})
        return no_cache_json({'success': False, 'error': 'Not found'})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


# ---------------------------------------------
# ADD / DELETE CARD
# ---------------------------------------------

@app.route('/admin/api/add-card', methods=['POST'])
@login_required
def add_card_api():
    """Create a brand-new Card row."""
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

        new_card = Card(
            title                    = title,
            image                    = image or None,
            buttons                  = json.dumps(buttons_list),
            button_contents          = json.dumps({}),
            button_links             = json.dumps({}),
            button_images            = json.dumps({}),
            button_background_images = json.dumps({}),
            card_order               = max_order + 1,
        )
        db.session.add(new_card)
        db.session.commit()
        clear_ai_cache()

        return no_cache_json({
            'success': True,
            'message': 'Card created',
            'card': {
                'id':         new_card.id,
                'title':      new_card.title,
                'image':      new_card.image,
                'buttons':    buttons_list,
                'card_order': new_card.card_order,
            }
        })
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)}, 500)


@app.route('/admin/api/delete-card', methods=['POST'])
@login_required
def delete_card_api():
    """Permanently delete a Card and all its associated button data."""
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


# ---------------------------------------------
# ADD / DELETE NAV LINK
# ---------------------------------------------

@app.route('/admin/api/add-nav-link', methods=['POST'])
@login_required
def add_nav_link_api():
    """Create a brand-new NavigationLink row."""
    try:
        link_text    = request.form.get('text', '').strip()
        link_url     = request.form.get('url',  '').strip() or None
        page_content = request.form.get('content', '')
        image        = request.form.get('image', '').strip() or None

        if not link_text:
            return no_cache_json({'success': False, 'error': 'Link text is required'}, 400)

        max_order = db.session.query(db.func.max(NavigationLink.link_order)).scalar() or 0

        new_nav = NavigationLink(
            link_text    = link_text,
            link_url     = link_url,
            page_content = page_content,
            image        = image,
            images       = json.dumps([]),
            link_order   = max_order + 1,
        )
        db.session.add(new_nav)
        db.session.commit()
        clear_ai_cache()

        return no_cache_json({
            'success': True,
            'message': 'Navigation link created',
            'nav': {
                'id':         new_nav.id,
                'link_text':  new_nav.link_text,
                'link_url':   new_nav.link_url,
                'link_order': new_nav.link_order,
            }
        })
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)}, 500)


@app.route('/admin/api/delete-nav-link', methods=['POST'])
@login_required
def delete_nav_link_api():
    """Permanently delete a NavigationLink row."""
    try:
        link = db.session.get(NavigationLink, request.form.get('id'))
        if link:
            db.session.delete(link)
            db.session.commit()
            clear_ai_cache()
            return no_cache_json({'success': True})
        return no_cache_json({'success': False, 'error': 'Not found'})
    except Exception as e:
        return no_cache_json({'success': False, 'error': str(e)})


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


# ---------------------------------------------
# PAGE BUILDER ROUTES
# ---------------------------------------------

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
        slug  = ''.join(c for c in title.lower().replace(' ', '-') if c.isalnum() or c == '-')
        if db.session.query(Page).filter_by(slug=slug).first():
            slug = f"{slug}-{''.join(secrets.choice(string.ascii_lowercase) for _ in range(4))}"
        page  = Page(title=title, slug=slug,
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
                'card_buttons': b.get_card_buttons(),
                'background_color': b.background_color, 'text_alignment': b.text_alignment,
                'layout_columns': b.layout_columns, 'is_visible': b.is_visible,
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
            if f in data: setattr(page, f, data[f])
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
        data = request.get_json()
        page = db.session.get(Page, data.get('page_id'))
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
            if f in data: setattr(block, f, data[f])
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
            if block: block.block_order = item['order']
        db.session.commit()
        return no_cache_json({'success': True})
    except Exception as e:
        db.session.rollback()
        return no_cache_json({'success': False, 'error': str(e)})


# ---------------------------------------------
# DB INIT
# ---------------------------------------------

def init_db():
    with app.app_context():
        db.create_all()
        if not Admin.query.filter_by(username='admin').first():
            admin = Admin(username='admin')
            admin.set_password('admin123')
            db.session.add(admin)
        default_content = [
            ('hero_title', 'LEADING THE MOVEMENT IN <br>ADVANCING INNOVATION AND <br>PRODUCTIVITY IN THE <span class="text-[#cdae2c]">PUBLIC SECTOR</span>', 1),
            ('hero_image', 'images/Hero-Banner.png', 2),
            ('search_placeholder', 'Ask Tutoy anything about Public Sector Productivity?', 3),
            ('loading_text', 'Generating...', 4),
            ('ai_label', 'AI says', 5),
            ('key_points_title', 'Key Points', 6),
            ('related_topics_title', 'Related Topics', 7),
            ('quick_actions_title', 'Quick Actions', 8),
            ('logo_image', 'images/dap-logo.png', 9),
            ('gemini_image', 'images/gemini.png', 10),
            ('apo_logo', 'images/apo.png', 11),
            ('company_name', 'Development Academy of The Philippines', 12),
            ('company_subtitle', 'Center of Excellence on Public Sector Productivity', 13),
            ('company_address', 'DAP Building, San Miguel Avenue, Pasig City 1500', 14),
            ('company_phone', '+632 631 0921 to 30', 15),
            ('company_fax', '+632 631 2123', 16),
            ('company_email', 'coe_psp@dap.edu.ph', 17),
            ('site_title', 'COE Public-Sector Productivity', 18),
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
        db.session.commit()
        
@app.route('/admin/api/upload-health')
@login_required
def upload_health():
    """Ping the gov.ph upload_receiver and report back to the admin panel."""
    try:
        headers = {}
        if GOV_UPLOAD_TOKEN:
            headers['Authorization'] = f'Bearer {GOV_UPLOAD_TOKEN}'
        resp = requests.get(
            GOV_UPLOAD_URL.replace('/upload_receiver', '/health'),
            headers=headers,
            timeout=6,
        )
        if resp.status_code == 200:
            return no_cache_json({'ok': True, 'url': GOV_UPLOAD_URL})
        return no_cache_json({'ok': False, 'error': f'Server returned HTTP {resp.status_code}'})
    except requests.exceptions.ConnectionError:
        return no_cache_json({'ok': False, 'error': 'Connection refused — is upload_receiver.py running?'})
    except requests.exceptions.Timeout:
        return no_cache_json({'ok': False, 'error': 'Request timed out'})
    except Exception as e:
        return no_cache_json({'ok': False, 'error': str(e)})

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
