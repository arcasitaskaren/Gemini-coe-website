from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
import json
import re as _re

db = SQLAlchemy()

class ContentSection(db.Model):
    __tablename__ = 'content_sections'
    
    id = db.Column(db.Integer, primary_key=True)
    content_key = db.Column(db.String(100), unique=True, nullable=False)
    content_value = db.Column(db.Text)
    section_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Card(db.Model):
    __tablename__ = 'cards'
    
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    image = db.Column(db.String(255))
    background_image = db.Column(db.String(255))
    buttons = db.Column(db.Text)  # JSON array stored as text
    button_contents = db.Column(db.Text)  # JSON object mapping button index to content with text
    button_links = db.Column(db.Text)  # JSON object mapping button index to link URL
    button_images = db.Column(db.Text)  # JSON object mapping button index to image gallery (multiple images with captions)
    button_background_images = db.Column(db.Text)  # JSON object mapping button index to background image filename
    card_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def get_buttons(self):
        try:
            if self.buttons:
                return json.loads(self.buttons)
            return []
        except:
            return []
    
    def get_button_contents(self):
        try:
            if self.button_contents:
                return json.loads(self.button_contents)
            return {}
        except:
            return {}
    
    def get_button_links(self):
        try:
            if self.button_links:
                return json.loads(self.button_links)
            return {}
        except:
            return {}
    
    def get_button_images(self):
        try:
            if self.button_images:
                return json.loads(self.button_images)
            return {}
        except:
            return {}
    
    def get_button_background_images(self):
        try:
            if self.button_background_images:
                return json.loads(self.button_background_images)
            return {}
        except:
            return {}

class NavigationLink(db.Model):
    __tablename__ = 'navigation_links'
    
    id = db.Column(db.Integer, primary_key=True)
    link_text = db.Column(db.String(100), nullable=False)
    link_url = db.Column(db.String(255))
    page_content = db.Column(db.Text)
    background_image = db.Column(db.String(255))
    image = db.Column(db.String(255))
    images = db.Column(db.Text)  # JSON array of image objects with captions
    link_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def get_images(self):
        try:
            if self.images:
                return json.loads(self.images)
            return []
        except:
            return []

class FooterSection(db.Model):
    __tablename__ = 'footer_sections'
    
    id = db.Column(db.Integer, primary_key=True)
    section_title = db.Column(db.String(100))
    section_content = db.Column(db.Text)
    section_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SuggestedProfessional(db.Model):
    __tablename__ = 'suggested_professionals'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(255))
    image = db.Column(db.String(255))
    description = db.Column(db.Text)
    professional_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Page(db.Model):
    __tablename__ = 'pages'
    
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), unique=True, nullable=False)
    description = db.Column(db.Text)
    layout_template = db.Column(db.String(50), default='content_blocks')
    is_published = db.Column(db.Boolean, default=False, nullable=False)
    page_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    blocks = db.relationship('PageBlock', backref='page', lazy=True, cascade='all, delete-orphan')
    
    def get_blocks_ordered(self):
        return sorted(self.blocks, key=lambda x: x.block_order)


class PageBlock(db.Model):
    __tablename__ = 'page_blocks'
    
    id = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.Integer, db.ForeignKey('pages.id'), nullable=False)
    block_type = db.Column(db.String(50), nullable=False)
    block_order = db.Column(db.Integer, default=0)
    
    content = db.Column(db.Text)
    heading = db.Column(db.String(500))
    subheading = db.Column(db.String(500))
    image_url = db.Column(db.String(500))
    image_alt_text = db.Column(db.String(255))
    image_caption = db.Column(db.Text)
    
    card_title = db.Column(db.String(255))
    card_description = db.Column(db.Text)
    card_image = db.Column(db.String(500))
    card_buttons = db.Column(db.Text)
    
    background_color = db.Column(db.String(50))
    text_alignment = db.Column(db.String(20), default='left')
    layout_columns = db.Column(db.Integer, default=1)
    custom_css = db.Column(db.Text)
    
    is_visible = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def get_card_buttons(self):
        try:
            if self.card_buttons:
                return json.loads(self.card_buttons)
            return []
        except:
            return []


class Admin(UserMixin, db.Model):
    __tablename__ = 'admins'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
        
class CrawledPage(db.Model):
    __tablename__ = 'crawled_pages'

    id          = db.Column(db.Integer, primary_key=True)
    source_url  = db.Column(db.String(500), index=True)
    page_url    = db.Column(db.String(500), unique=True)
    page_title  = db.Column(db.String(300), default='')
    text_content= db.Column(db.Text, default='')
    nav_link_id = db.Column(db.Integer, db.ForeignKey('navigation_links.id'), nullable=True)
    crawled_at  = db.Column(db.DateTime, default=datetime.utcnow)
    depth       = db.Column(db.Integer, default=0)

    def __repr__(self):
        return f'<CrawledPage {self.page_url}>'


class NewsArticle(db.Model):
    """A publishable news article that can be linked to a nav link or card."""
    __tablename__ = "news_articles"

    id            = db.Column(db.Integer,  primary_key=True)
    title         = db.Column(db.String(500), nullable=False)
    slug          = db.Column(db.String(500), nullable=False, unique=True)
    excerpt       = db.Column(db.Text)
    body          = db.Column(db.Text)
    cover_image   = db.Column(db.String(500))

    # ── Article sidebar image (separate from cover_image) ───────────────
    # Displayed as the article thumbnail/sidebar image in the What's New listing.
    article_image = db.Column(db.String(500), nullable=True)

    published_at  = db.Column(db.DateTime(timezone=True),
                              default=lambda: datetime.now(timezone.utc))
    is_published  = db.Column(db.Boolean, nullable=False, default=False)
    is_archived   = db.Column(db.Boolean, nullable=False, default=False)
    nav_link_id   = db.Column(db.Integer,
                              db.ForeignKey("navigation_links.id", ondelete="SET NULL"),
                              nullable=True)
    card_id       = db.Column(db.Integer,
                              db.ForeignKey("cards.id", ondelete="SET NULL"),
                              nullable=True)
    article_order = db.Column(db.Integer, nullable=False, default=0)
    created_at    = db.Column(db.DateTime(timezone=True),
                              default=lambda: datetime.now(timezone.utc))
    updated_at    = db.Column(db.DateTime(timezone=True),
                              default=lambda: datetime.now(timezone.utc),
                              onupdate=lambda: datetime.now(timezone.utc))

    @staticmethod
    def _make_slug(title: str) -> str:
        import re as _re
        s = title.lower().strip()
        s = _re.sub(r"[^\w\s-]", "", s)
        s = _re.sub(r"[\s_-]+", "-", s)
        return s[:480]

    @classmethod
    def unique_slug(cls, title: str) -> str:
        base = cls._make_slug(title)
        slug, counter = base, 1
        while cls.query.filter_by(slug=slug).first():
            slug = f"{base}-{counter}"
            counter += 1
        return slug

    def formatted_date(self) -> str:
        if not self.published_at:
            return ""
        dt = self.published_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%B %d, %Y").replace(" 0", " ")

    def __repr__(self):
        return f"<NewsArticle id={self.id} slug={self.slug!r}>"

        # ── Add this class to models.py (alongside NewsArticle) ─────────────────────

class TrainingProgram(db.Model):
    """A publishable training program that appears on the Trainings nav page."""
    __tablename__ = "training_programs"

    id            = db.Column(db.Integer,  primary_key=True)
    title         = db.Column(db.String(500), nullable=False)
    slug          = db.Column(db.String(500), nullable=False, unique=True)
    excerpt       = db.Column(db.Text)          # short card teaser
    body          = db.Column(db.Text)          # full rich-text description
    cover_image       = db.Column(db.String(500))   # filename only (bare, no path prefix)
    background_image  = db.Column(db.String(500))   # hero background on trainings listing page
    program_order = db.Column(db.Integer, nullable=False, default=0)
    is_published  = db.Column(db.Boolean, nullable=False, default=False)
    is_archived   = db.Column(db.Boolean, nullable=False, default=False)
    nav_link_id   = db.Column(
        db.Integer,
        db.ForeignKey("navigation_links.id", ondelete="SET NULL"),
        nullable=True,
    )
    published_at  = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    created_at    = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at    = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # ── Slug helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _make_slug(title: str) -> str:
        import re as _re
        s = title.lower().strip()
        s = _re.sub(r"[^\w\s-]", "", s)
        s = _re.sub(r"[\s_-]+", "-", s)
        return s[:480]

    @classmethod
    def unique_slug(cls, title: str) -> str:
        base = cls._make_slug(title)
        slug, counter = base, 1
        while cls.query.filter_by(slug=slug).first():
            slug = f"{base}-{counter}"
            counter += 1
        return slug

    def formatted_date(self) -> str:
        if not self.published_at:
            return ""
        dt = self.published_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%B %d, %Y").replace(" 0", " ")

    def __repr__(self):
        return f"<TrainingProgram id={self.id} slug={self.slug!r}>"
