from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
import json

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
    background_image = db.Column(db.String(255))  # New: background image for the card
    buttons = db.Column(db.Text)  # JSON array stored as text
    button_contents = db.Column(db.Text)  # JSON object mapping button index to content with text
    button_links = db.Column(db.Text)  # JSON object mapping button index to link URL
    button_images = db.Column(db.Text)  # JSON object mapping button index to image gallery (multiple images with captions)
    button_background_images = db.Column(db.Text)  # JSON object mapping button index to background image filename
    card_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def get_buttons(self):
        """Parse buttons JSON"""
        try:
            if self.buttons:
                return json.loads(self.buttons)
            return []
        except:
            return []
    
    def get_button_contents(self):
        """Parse button contents JSON"""
        try:
            if self.button_contents:
                return json.loads(self.button_contents)
            return {}
        except:
            return {}
    
    def get_button_links(self):
        """Parse button links JSON"""
        try:
            if self.button_links:
                return json.loads(self.button_links)
            return {}
        except:
            return {}
    
    def get_button_images(self):
        """Parse button images JSON - returns dict with button index mapping to list of image objects"""
        try:
            if self.button_images:
                return json.loads(self.button_images)
            return {}
        except:
            return {}
    
    def get_button_background_images(self):
        """Parse button background images JSON - returns dict with button index mapping to background image filename"""
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
    link_url = db.Column(db.String(255))  # External link URL - prioritized if set
    page_content = db.Column(db.Text)  # Page content - used if no link_url
    background_image = db.Column(db.String(255))  # New: background image for the page
    image = db.Column(db.String(255))
    images = db.Column(db.Text)  # JSON array of image objects with captions for image gallery (multiple images)
    link_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def get_images(self):
        """Parse images JSON - returns list of image objects with captions"""
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
    """Represents a page with custom layout and content blocks"""
    __tablename__ = 'pages'
    
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), unique=True, nullable=False)  # URL-friendly name
    description = db.Column(db.Text)  # SEO description
    layout_template = db.Column(db.String(50), default='content_blocks')  # 'content_blocks', 'cards', 'navigation'
    is_published = db.Column(db.Boolean, default=False)
    page_order = db.Column(db.Integer, default=0)  # For ordering pages in navigation
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship to blocks
    blocks = db.relationship('PageBlock', backref='page', lazy=True, cascade='all, delete-orphan')
    
    def get_blocks_ordered(self):
        """Get blocks ordered by position"""
        return sorted(self.blocks, key=lambda x: x.block_order)


class PageBlock(db.Model):
    """Represents a content block within a page (heading, text, image, card, etc.)"""
    __tablename__ = 'page_blocks'
    
    id = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.Integer, db.ForeignKey('pages.id'), nullable=False)
    block_type = db.Column(db.String(50), nullable=False)  # 'heading', 'text', 'image', 'card', 'card_grid', 'divider'
    block_order = db.Column(db.Integer, default=0)
    
    # Content fields (JSON for flexibility)
    content = db.Column(db.Text)  # Main text content
    heading = db.Column(db.String(500))  # For heading blocks
    subheading = db.Column(db.String(500))  # For subheading
    image_url = db.Column(db.String(500))  # Image URL or filename
    image_alt_text = db.Column(db.String(255))
    image_caption = db.Column(db.Text)
    
    # Card-specific fields
    card_title = db.Column(db.String(255))
    card_description = db.Column(db.Text)
    card_image = db.Column(db.String(500))
    card_buttons = db.Column(db.Text)  # JSON array
    
    # Styling
    background_color = db.Column(db.String(50))  # Color/class name
    text_alignment = db.Column(db.String(20), default='left')  # 'left', 'center', 'right'
    layout_columns = db.Column(db.Integer, default=1)  # For grid layouts: 1, 2, 3, 4
    custom_css = db.Column(db.Text)  # Custom CSS classes or inline styles
    
    # Metadata
    is_visible = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def get_card_buttons(self):
        """Parse card buttons JSON"""
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
