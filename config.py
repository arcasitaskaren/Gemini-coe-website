import os
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://postgres.qwjhlktcubyeeusmawop:arcasitask2810@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require')
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"sslmode": "require"}}
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    PERMANENT_SESSION_LIFETIME = timedelta(hours=24)
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    UPLOAD_FOLDER = os.path.join(basedir, 'static', 'images')
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    PREFERRED_URL_SCHEME = 'http'
