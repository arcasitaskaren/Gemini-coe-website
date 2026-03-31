# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
Setup script for DAP COE CMS Flask Application
Initializes database with default data and creates necessary folders
Compatible with PostgreSQL / Supabase
"""

import os
import sys
import json
import psycopg2
from urllib.parse import urlparse

# -------------------------------------------------
# Force UTF-8 output (Windows-safe)
# -------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def safe_print(text):
    """Safely print emoji text on Windows terminals."""
    try:
        print(text)
    except UnicodeEncodeError:
        fallback = (
            text.replace("??", "[DIR]")
                .replace("???", "[DB]")
                .replace("??", "[TABLES]")
                .replace("??", "[DATA]")
                .replace("??", "[USER]")
                .replace("?", "[OK]")
                .replace("?", "[ERROR]")
                .replace("?", "[FAILED]")
                .replace("??", "[WARNING]")
                .replace("?", "[INFO]")
                .replace("?", "")
                .replace("??", "")
                .replace("??", "")
                .replace("", "-")
        )
        print(fallback)


from app import app, db
from models import Admin, ContentSection, Card, NavigationLink, FooterSection, SuggestedProfessional, Page, PageBlock
from config import Config


def verify_database_connection():
    """Verify connection to the Supabase PostgreSQL database."""
    safe_print("???  Verifying Supabase PostgreSQL connection...")
    try:
        # Parse the DATABASE_URL from config
        db_url = Config.SQLALCHEMY_DATABASE_URI
        parsed = urlparse(db_url)

        connection = psycopg2.connect(
            host=parsed.hostname,
            user=parsed.username,
            password=parsed.password,
            port=parsed.port or 5432,
            dbname=parsed.path.lstrip("/"),
            sslmode="require"  # Supabase requires SSL
        )
        connection.close()
        safe_print(f"  ? Connected to Supabase PostgreSQL successfully")
        return True

    except psycopg2.OperationalError as e:
        safe_print(f"  ? Error connecting to Supabase: {e}")
        safe_print("  ??  Check your DATABASE_URL and Supabase project status.")
        return False
    except Exception as e:
        safe_print(f"  ? Unexpected error: {e}")
        return False


def insert_default_data():
    """Insert default content, cards, and navigation links."""
    safe_print("\n?? Inserting default data...")

    default_content = [
        {
            "content_key": "hero_title",
            "content_value": 'LEADING THE MOVEMENT IN <br>ADVANCING INNOVATION AND <br>PRODUCTIVITY IN THE <span class="text-[#cdae2c]">PUBLIC SECTOR</span>',
            "section_order": 1,
        },
        {
            "content_key": "hero_image",
            "content_value": "images/Hero-Banner.png",
            "section_order": 2,
        },
        {
            "content_key": "search_placeholder",
            "content_value": "What do you want to know?",
            "section_order": 3,
        },
    ]

    for content in default_content:
        existing = ContentSection.query.filter_by(
            content_key=content["content_key"]
        ).first()
        if not existing:
            section = ContentSection(**content)
            db.session.add(section)
            safe_print(f"  ? Added content: {content['content_key']}")
        else:
            safe_print(f"  ? Content already exists: {content['content_key']}")

    default_cards = [
        {
            "title": "Whats New?",
            "image": "slide1.png",
            "buttons": ["Trainings and Capacity Development", "Knowledge Products", "Community"],
            "button_contents": {
                "0": "Information about our training programs and capacity development initiatives.",
                "1": "Access our knowledge products including research studies and publications.",
                "2": "Join our community of professionals dedicated to public sector innovation."
            },
            "button_images": {},
            "card_order": 1,
        },
        {
            "title": "Productivity Challenge",
            "image": "slide2.jpg",
            "buttons": ["2025 Paper-Less", "Previous Challenge", "Submit an Entry"],
            "button_contents": {
                "0": "The 2025 Paper-Less Challenge aims to promote digital transformation in the public sector.",
                "1": "View previous challenges and their outcomes.",
                "2": "Submit your entry for the current Productivity Challenge."
            },
            "button_images": {},
            "card_order": 2,
        },
        {
            "title": "Governance Lab",
            "image": "govlab.jpg",
            "buttons": ["About Us", "What is GovLab?", "Join Us"],
            "button_contents": {
                "0": "Learn about the Governance Lab and our mission.",
                "1": "GovLab is our innovation lab dedicated to exploring emerging technologies and methodologies.",
                "2": "Interested in joining? Contact us to learn more about membership opportunities."
            },
            "button_images": {},
            "card_order": 3,
        },
    ]

    for card_data in default_cards:
        existing = db.session.query(Card).filter_by(title=card_data["title"]).first()
        if not existing:
            card = Card(
                title=card_data["title"],
                image=card_data["image"],
                buttons=json.dumps(card_data["buttons"]),
                button_contents=json.dumps(card_data.get("button_contents", {})),
                button_links=json.dumps({}),
                button_images=json.dumps(card_data.get("button_images", {})),
                card_order=card_data["card_order"],
            )
            db.session.add(card)
            safe_print(f"  ? Added card: {card_data['title']}")
        else:
            safe_print(f"  ? Card already exists: {card_data['title']}")

    suggested_professionals_data = [
        {
            "name": "Dr. Maria Santos",
            "title": "Innovation Specialist",
            "description": "Expert in digital transformation and public sector innovation.",
            "order": 1
        },
        {
            "name": "Engr. Juan Dela Cruz",
            "title": "Technology Consultant",
            "description": "Specializes in implementing emerging technologies in government.",
            "order": 2
        },
        {
            "name": "Ms. Sofia Reyes",
            "title": "Change Management Expert",
            "description": "Guides organizations through digital and operational transformation.",
            "order": 3
        },
    ]

    for prof_data in suggested_professionals_data:
        existing = db.session.query(SuggestedProfessional).filter_by(name=prof_data["name"]).first()
        if not existing:
            professional = SuggestedProfessional(
                name=prof_data["name"],
                title=prof_data.get("title"),
                description=prof_data.get("description"),
                professional_order=prof_data.get("order", 0)
            )
            db.session.add(professional)
            safe_print(f"  ? Added professional: {prof_data['name']}")
        else:
            safe_print(f"  ? Professional already exists: {prof_data['name']}")

    nav_links_data = [
        {"text": "About Us",             "url": None, "content": "Learn more about the Development Academy of the Philippines and our mission to advance public sector productivity.", "images": [], "order": 1},
        {"text": "Whats New",            "url": None, "content": "Stay updated with the latest news, announcements, and updates from DAP-COE.", "images": [], "order": 2},
        {"text": "Trainings",            "url": None, "content": "Explore our comprehensive training programs and capacity development courses.", "images": [], "order": 3},
        {"text": "Conferences",          "url": None, "content": "Information about upcoming and past conferences organized by DAP-COE.", "images": [], "order": 4},
        {"text": "Community",            "url": None, "content": "Join our community of professionals dedicated to public sector innovation.", "images": [], "order": 5},
        {"text": "Knowledge Products",   "url": None, "content": "Access our research studies, publications, and knowledge resources.", "images": [], "order": 6},
        {"text": "Productivity Challenge","url": None, "content": "Participate in our annual productivity challenge and showcase your innovations.", "images": [], "order": 7},
        {"text": "GovLab",              "url": None, "content": "Discover our innovation lab dedicated to exploring emerging technologies and methodologies.", "images": [], "order": 8},
        {"text": "NextGenPh",           "url": None, "content": "NextGen Philippines initiative for developing the next generation of leaders.", "images": [], "order": 9},
    ]

    for link_data in nav_links_data:
        existing = db.session.query(NavigationLink).filter_by(link_text=link_data["text"]).first()
        if not existing:
            nav_link = NavigationLink(
                link_text=link_data["text"],
                link_url=link_data["url"],
                page_content=link_data["content"],
                images=json.dumps(link_data.get("images", [])),
                link_order=link_data["order"]
            )
            db.session.add(nav_link)
            safe_print(f"  ? Added navigation link: {link_data['text']}")
        else:
            safe_print(f"  ? Navigation link already exists: {link_data['text']}")

    try:
        db.session.commit()
        safe_print("? Default data inserted successfully")
        return True
    except Exception as e:
        db.session.rollback()
        safe_print(f"? Error inserting default data: {e}")
        return False


def run_schema_updates():
    """
    Add any missing columns using PostgreSQL-compatible ALTER TABLE syntax.
    PostgreSQL does not support ALTER TABLE ... ADD COLUMN IF NOT EXISTS in older versions,
    so we wrap each in a try/except.
    """
    safe_print("\n?? Checking for schema updates...")

    schema_updates = [
        ("cards",            "button_links",             "TEXT"),
        ("cards",            "background_image",         "VARCHAR(255)"),
        ("cards",            "button_background_images", "TEXT"),
        ("navigation_links", "background_image",         "VARCHAR(255)"),
        ("pages",            "is_published",             "BOOLEAN DEFAULT FALSE"),
        ("pages",            "page_order",               "INTEGER DEFAULT 0"),
    ]

    with db.engine.connect() as conn:
        for table, column, col_type in schema_updates:
            try:
                # PostgreSQL 9.6+ supports IF NOT EXISTS
                conn.execute(
                    f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{column}" {col_type}'
                )
                conn.commit()
                safe_print(f"  ? Column '{column}' ensured in '{table}'")
            except Exception as e:
                conn.rollback()
                safe_print(f"  ??  Skipped '{column}' on '{table}': {e}")


def setup():
    safe_print("=" * 70)
    safe_print(" " * 12 + "DAP COE CMS - Supabase/PostgreSQL Setup")
    safe_print("=" * 70)

    # 1. Create local directories
    safe_print("\n?? Creating directories...")
    for directory in ["static/images", "static/uploads", "templates"]:
        os.makedirs(directory, exist_ok=True)
        safe_print(f"  ? {directory} directory created/verified")

    # 2. Verify Supabase connection
    if not verify_database_connection():
        safe_print("\n? Cannot reach Supabase. Check your DATABASE_URL in config.py / .env")
        return False

    # 3. Create tables & seed data inside app context
    safe_print("\n?? Creating database tables...")
    try:
        with app.app_context():
            db.create_all()
            safe_print("  ? Database tables created/verified")

            run_schema_updates()
            insert_default_data()

            # 4. Admin user
            safe_print("\n?? Setting up admin user...")
            admin = db.session.query(Admin).filter_by(username="admin").first()
            if admin:
                safe_print("  ? Default admin user already exists")
            else:
                admin = Admin(username="admin")
                admin.set_password("admin123")
                db.session.add(admin)
                db.session.commit()
                safe_print("  ? Default admin user created")

    except Exception as e:
        safe_print(f"  ? Error initializing database: {e}")
        import traceback
        traceback.print_exc()
        return False

    # 5. Done
    safe_print("\n" + "=" * 70)
    safe_print("? Setup Complete! Your Supabase database is ready.")
    safe_print("=" * 70)

    safe_print("\n?? Next steps:")
    safe_print("   1. Run the application: python app.py")
    safe_print("   2. Visit: http://localhost:5000")
    safe_print("   3. Admin panel: http://localhost:5000/admin")
    safe_print("   4. Page builder: http://localhost:5000/admin/page-builder")

    safe_print("\n?? Default Login Credentials:")
    safe_print("   Username: admin")
    safe_print("   Password: admin123")
    safe_print("\n??  IMPORTANT: Change these credentials before going to production!")

    safe_print("\n?? Your database is now initialized with:")
    safe_print("    Database tables (Pages, PageBlocks, Cards, Nav, etc.)")
    safe_print("    Default content sections")
    safe_print("    3 default cards")
    safe_print("    9 navigation links")
    safe_print("    Default admin user")

    safe_print("\n" + "=" * 70)
    return True


if __name__ == "__main__":
    try:
        success = setup()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        safe_print("\n\n? Setup cancelled by user")
        sys.exit(1)
    except Exception as e:
        safe_print(f"\n? Setup failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)