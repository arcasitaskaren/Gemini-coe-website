import os
from datetime import timedelta

class Config:
    """Base configuration"""

    # Supabase / PostgreSQL connection
    DATABASE_URL = os.getenv('DATABASE_URL')

    # Fallback if env variable not set
    if not DATABASE_URL:
        DATABASE_URL="postgresql://postgres.qwjhlktcubyeeusmawop:arcasitask2810@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require"

    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Force SSL for Supabase connections
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {
            "sslmode": "require"
        }
    }

    # Flask security & session
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    PERMANENT_SESSION_LIFETIME = timedelta(hours=24)

    SESSION_COOKIE_SECURE = False  # Set True if using HTTPS
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'

    # Upload settings
    UPLOAD_FOLDER = 'static/images'
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5MB
    ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif'}

    # Gemini AI
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')