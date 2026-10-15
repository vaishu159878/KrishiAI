import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager, asynccontextmanager
from pydantic import BaseModel, EmailStr

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from pwdlib import PasswordHash
import jwt

# --------------------------------------------------
# Logging configuration (CloudWatch-compatible)
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("krishiai")

# --------------------------------------------------
# Environment configuration
# --------------------------------------------------

load_dotenv()

AI_API_KEY = os.getenv("AI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
is_production = ENVIRONMENT == "production"

# Candidate models in priority order for automatic resilience against deprecations
CANDIDATE_MODELS = list(dict.fromkeys([
    GEMINI_MODEL,
    "gemini-flash-latest",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
]))

if not AI_API_KEY:
    raise RuntimeError("AI_API_KEY is not configured")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")

if not JWT_SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY is not configured")

# --------------------------------------------------
# Gemini client
# --------------------------------------------------

client = genai.Client(api_key=AI_API_KEY)




class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# --------------------------------------------------
# Authentication helpers
# --------------------------------------------------

password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hash.verify(password, hashed_password)



MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

# Very common passwords that must not be accepted (compared in lowercase).
COMMON_PASSWORDS = {
    "password", "password1", "password12", "password123", "passw0rd",
    "12345678", "123456789", "1234567890", "12341234", "11111111",
    "00000000", "87654321", "987654321", "qwerty123", "qwertyui",
    "qwertyuiop", "abcd1234", "abc12345", "iloveyou", "admin123",
    "welcome1", "welcome123", "letmein1", "pass1234", "india123",
    "krishiai", "krishi123", "farmer123", "kisan123", "shetkari",
}


def validate_password(password: str, username: str, email: str) -> str | None:
    """Return an error message, or None when the password is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        return (
            f"Password must be between {MIN_PASSWORD_LENGTH} and "
            f"{MAX_PASSWORD_LENGTH} characters."
        )

    lowered = password.lower()

    if lowered in COMMON_PASSWORDS or len(set(lowered)) == 1:
        return "That password is too common. Please choose a different one."

    if lowered in {username.lower(), email.lower(), email.split("@")[0].lower()}:
        return "Password must not be the same as your username or email."

    return None


import threading
import time

import httpx

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
from collections import deque

# --------------------------------------------------
# Login rate limiting (per email and per client IP)
# --------------------------------------------------
# Protects against single-account brute-force and IP password spraying
# behind CloudFront/Nginx reverse proxies.

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 600  # 10 minutes

LOGIN_MAX_FAILURES_PER_IP = 15
LOGIN_IP_WINDOW_SECONDS = 900  # 15 minutes

_login_failures: dict[str, deque] = {}
_login_ip_failures: dict[str, deque] = {}
_login_lock = threading.Lock()


def _recent_failures(email: str, now: float):
    """Return the deque of failures inside the window (caller holds the lock)."""
    failures = _login_failures.get(email)

    if failures is None:
        return None

    while failures and now - failures[0] > LOGIN_WINDOW_SECONDS:
        failures.popleft()

    if not failures:
        del _login_failures[email]
        return None

    return failures


def login_retry_after(email: str) -> int:
    """Seconds the caller must wait, or 0 when a login attempt is allowed."""
    now = time.monotonic()

    with _login_lock:
        failures = _recent_failures(email, now)

        if failures and len(failures) >= LOGIN_MAX_FAILURES:
            return max(1, int(LOGIN_WINDOW_SECONDS - (now - failures[0])) + 1)

    return 0


def login_ip_retry_after(client_ip: str) -> int:
    """Seconds an IP address must wait after repeated failed attempts across accounts."""
    if not client_ip or client_ip == "unknown":
        return 0

    now = time.monotonic()
    with _login_lock:
        failures = _login_ip_failures.get(client_ip)
        if not failures:
            return 0

        while failures and now - failures[0] > LOGIN_IP_WINDOW_SECONDS:
            failures.popleft()

        if not failures:
            _login_ip_failures.pop(client_ip, None)
            return 0

        if len(failures) >= LOGIN_MAX_FAILURES_PER_IP:
            return max(1, int(LOGIN_IP_WINDOW_SECONDS - (now - failures[0])) + 1)

    return 0


def record_login_failure(email: str, client_ip: str = "") -> None:
    now = time.monotonic()

    with _login_lock:
        if len(_login_failures) > 10000:
            for key in list(_login_failures):
                _recent_failures(key, now)

            if len(_login_failures) > 10000:
                _login_failures.clear()

        _login_failures.setdefault(
            email, deque(maxlen=LOGIN_MAX_FAILURES)
        ).append(now)

        if client_ip and client_ip != "unknown":
            if len(_login_ip_failures) > 10000:
                _login_ip_failures.clear()

            _login_ip_failures.setdefault(
                client_ip, deque(maxlen=LOGIN_MAX_FAILURES_PER_IP)
            ).append(now)


def clear_login_failures(email: str, client_ip: str = "") -> None:
    with _login_lock:
        _login_failures.pop(email, None)
        if client_ip and client_ip != "unknown":
            _login_ip_failures.pop(client_ip, None)


# --------------------------------------------------
# Registration rate limiting (per IP address)
# --------------------------------------------------

REGISTRATION_MAX_PER_IP = 5
REGISTRATION_WINDOW_SECONDS = 3600  # 1 hour

_registration_history: dict[str, deque] = {}
_registration_lock = threading.Lock()


def get_client_ip(request: Request) -> str:
    """Extract client IP, taking X-Forwarded-For (from reverse proxy) into account."""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_registration_rate_limit(client_ip: str) -> None:
    now = time.monotonic()
    with _registration_lock:
        if len(_registration_history) > 10000:
            _registration_history.clear()

        attempts = _registration_history.get(client_ip)
        if attempts is None:
            attempts = deque(maxlen=REGISTRATION_MAX_PER_IP)
            _registration_history[client_ip] = attempts

        while attempts and now - attempts[0] > REGISTRATION_WINDOW_SECONDS:
            attempts.popleft()

        if len(attempts) >= REGISTRATION_MAX_PER_IP:
            raise HTTPException(
                status_code=429,
                detail="Too many accounts created from this network. Please try again in an hour.",
            )

        attempts.append(now)


# --------------------------------------------------
# Guest analysis rate limiting (per IP address)
# --------------------------------------------------

GUEST_ANALYSIS_MAX_PER_IP = 10
GUEST_ANALYSIS_WINDOW_SECONDS = 86400  # 24 hours

_guest_analysis_history: dict[str, deque] = {}
_guest_analysis_lock = threading.Lock()


def check_guest_analysis_rate_limit(client_ip: str) -> None:
    now = time.monotonic()
    with _guest_analysis_lock:
        if len(_guest_analysis_history) > 10000:
            _guest_analysis_history.clear()

        attempts = _guest_analysis_history.get(client_ip)
        if attempts is None:
            attempts = deque(maxlen=GUEST_ANALYSIS_MAX_PER_IP)
            _guest_analysis_history[client_ip] = attempts

        while attempts and now - attempts[0] > GUEST_ANALYSIS_WINDOW_SECONDS:
            attempts.popleft()

        if len(attempts) >= GUEST_ANALYSIS_MAX_PER_IP:
            raise HTTPException(
                status_code=429,
                detail="Daily guest analysis quota reached (10 per 24 hours). Please create a free account to continue saving analyses.",
            )

        attempts.append(now)


# Used so that unknown emails take as long to reject as wrong passwords,
# which stops response timing from revealing which emails are registered.
DUMMY_PASSWORD_HASH = hash_password("not-a-real-password-for-timing-only")


ACCESS_TOKEN_EXPIRE_HOURS = 24


def create_access_token(user_id: int, username: str = "", email: str = "") -> str:
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(user_id),
        "username": username,
        "email": email,
        "iat": now,
        "exp": now + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
    }

    return jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")


def get_user_id_from_token(token: str) -> int:
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET_KEY,
            algorithms=["HS256"],
        )

        user_id = payload.get("sub")

        if not user_id:
            raise HTTPException(
                status_code=401,
                detail="Invalid authentication token.",
            )

        return int(user_id)

    except (jwt.InvalidTokenError, ValueError):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )




def get_current_user_id(
    authorization: str | None = Header(default=None),
) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Please log in to continue.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[7:].strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token.",
        )

    return get_user_id_from_token(token)


def get_optional_user_id(
    authorization: str | None = Header(default=None),
) -> int | None:
    """Return user_id if valid bearer token is provided; otherwise None (guest mode)."""
    if not authorization or not authorization.startswith("Bearer "):
        return None

    token = authorization[7:].strip()
    if not token:
        return None

    try:
        return get_user_id_from_token(token)
    except Exception:
        return None



# --------------------------------------------------
# PostgreSQL connection pool
# --------------------------------------------------

db_pool: pool.ThreadedConnectionPool | None = None


def init_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            dsn=DATABASE_URL,
        )
        logger.info("PostgreSQL ThreadedConnectionPool initialized (2-20 connections).")


def close_db_pool():
    global db_pool
    if db_pool is not None:
        db_pool.closeall()
        db_pool = None
        logger.info("PostgreSQL ThreadedConnectionPool closed.")


@contextmanager
def get_db_connection():
    if db_pool is None:
        init_db_pool()
    conn = db_pool.getconn()
    try:
        with conn:
            yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)


# --------------------------------------------------
# Database initialization
# --------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize connection pool and verify tables & indexes
    init_db_pool()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS crop_analyses (
                    id SERIAL PRIMARY KEY,
                    crop VARCHAR(150),
                    problem TEXT,
                    language VARCHAR(10) NOT NULL,
                    analysis TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(100) NOT NULL UNIQUE,
                    email VARCHAR(255) NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                ALTER TABLE crop_analyses
                ADD COLUMN IF NOT EXISTS user_id INTEGER
                REFERENCES users(id) ON DELETE CASCADE
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_analyses_user_created
                ON crop_analyses (user_id, created_at DESC)
            """)
    logger.info("Database initialized: schema and idx_analyses_user_created index verified.")

    yield

    close_db_pool()


# --------------------------------------------------
# Transient error classifier for retries
# --------------------------------------------------

def is_transient_error(exc: Exception) -> bool:
    """Return True if exception is a temporary/transient failure suitable for retry."""
    status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status_code in (429, 500, 502, 503, 504):
        return True
    if status_code in (400, 401, 403, 404):
        return False

    err_str = str(exc).lower()
    transient_indicators = [
        "429", "500", "502", "503", "504",
        "resource_exhausted", "quota", "rate limit",
        "service unavailable", "deadline exceeded", "timeout",
        "connection reset", "connection refused", "temporarily unavailable"
    ]
    return any(indicator in err_str for indicator in transient_indicators)


# --------------------------------------------------
# Supported languages
# --------------------------------------------------

LANGUAGES = {
    "en": {
        "name": "English",
        "error": (
            "KrishiAI is temporarily unable to connect to the AI service. "
            "Please try again shortly."
        ),
        "disclaimer": (
            "This AI-generated guidance is for informational purposes only. "
            "Consult a qualified agricultural expert before taking action."
        ),
    },
    "mr": {
        "name": "Marathi",
        "error": (
            "KrishiAI सध्या AI सेवेशी संपर्क साधू शकत नाही. "
            "कृपया थोड्या वेळाने पुन्हा प्रयत्न करा."
        ),
        "disclaimer": (
            "हे AI-निर्मित मार्गदर्शन केवळ माहितीसाठी आहे. "
            "योग्य उपाय करण्यापूर्वी पात्र कृषी तज्ज्ञांचा सल्ला घ्या."
        ),
    },
    "hi": {
        "name": "Hindi",
        "error": (
            "KrishiAI फिलहाल AI सेवा से संपर्क नहीं कर पा रहा है। "
            "कृपया कुछ देर बाद फिर कोशिश करें।"
        ),
        "disclaimer": (
            "यह AI द्वारा तैयार की गई जानकारी केवल मार्गदर्शन के लिए है। "
            "कोई कदम उठाने से पहले योग्य कृषि विशेषज्ञ से सलाह लें।"
        ),
    },
}



# --------------------------------------------------
# FastAPI application
# --------------------------------------------------

app = FastAPI(
    title="KrishiAI API",
    description="AI-powered agricultural assistance platform",
    version="1.3.0",
    lifespan=lifespan,
    docs_url=None if is_production else "/docs",
    redoc_url=None if is_production else "/redoc",
    openapi_url=None if is_production else "/openapi.json",
)

# --------------------------------------------------
# User registration
# --------------------------------------------------

@app.post("/auth/register", status_code=201)
def register_user(user: RegisterRequest, request: Request):
    client_ip = get_client_ip(request)
    check_registration_rate_limit(client_ip)

    username = user.username.strip()
    email = str(user.email).strip().lower()

    if len(username) < 3 or len(username) > 100:
        raise HTTPException(
            status_code=400,
            detail="Username must be between 3 and 100 characters.",
        )

    password_error = validate_password(user.password, username, email)

    if password_error:
        raise HTTPException(status_code=400, detail=password_error)

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO users (username, email, password_hash)
                    VALUES (%s, %s, %s)
                    RETURNING id, username, email, created_at
                    """,
                    (username, email, hash_password(user.password)),
                )

                new_user = cursor.fetchone()

        return {
            "message": "Account created successfully.",
            "user": {
                "id": new_user[0],
                "username": new_user[1],
                "email": new_user[2],
                "created_at": new_user[3].isoformat(),
            },
        }

    except psycopg2.errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="That username or email is already registered.",
        )
    except psycopg2.OperationalError:
        raise HTTPException(
            status_code=503,
            detail="Database service unavailable. Please try again shortly.",
        )


# --------------------------------------------------
# User login
# --------------------------------------------------

@app.post("/auth/login")
def login_user(user: LoginRequest, request: Request):
    client_ip = get_client_ip(request)
    email = str(user.email).strip().lower()

    # Check network/IP-level brute force protection
    ip_retry_after = login_ip_retry_after(client_ip)
    if ip_retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed login attempts from this network. Please try again in {ip_retry_after} seconds.",
            headers={"Retry-After": str(ip_retry_after)},
        )

    # Check email-level brute force protection
    retry_after = login_retry_after(email)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, username, email, password_hash
                    FROM users
                    WHERE email = %s
                    """,
                    (email,),
                )
                existing_user = cursor.fetchone()
    except psycopg2.OperationalError:
        raise HTTPException(
            status_code=503,
            detail="Database service unavailable. Please try again shortly.",
        )

    if existing_user:
        password_ok = verify_password(user.password, existing_user[3])
    else:
        verify_password(user.password, DUMMY_PASSWORD_HASH)
        password_ok = False

    if not password_ok:
        record_login_failure(email, client_ip)

        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    clear_login_failures(email, client_ip)

    token = create_access_token(existing_user[0], existing_user[1], existing_user[2])

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        "user": {
            "id": existing_user[0],
            "username": existing_user[1],
            "email": existing_user[2],
        },
    }


# --------------------------------------------------
# Current user profile
# --------------------------------------------------

@app.get("/auth/me")
def get_current_user_profile(user_id: int = Depends(get_current_user_id)):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, username, email
                    FROM users
                    WHERE id = %s
                    """,
                    (user_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(
                        status_code=404,
                        detail="User not found.",
                    )
                return {
                    "id": row[0],
                    "username": row[1],
                    "email": row[2],
                }
    except psycopg2.OperationalError:
        raise HTTPException(
            status_code=503,
            detail="Database service unavailable. Please try again shortly.",
        )



# --------------------------------------------------
# CORS configuration
# --------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:8081",
        "http://localhost:8081",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# Root and health endpoints
# --------------------------------------------------

# --------------------------------------------------
# Weather insights (Open-Meteo: free, no API key)
# --------------------------------------------------
# Data by Open-Meteo.com (CC BY 4.0). Their free tier is for non-commercial
# use; a commercial deployment needs one of their paid plans.

OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WEATHER_CACHE_SECONDS = 15 * 60
_weather_cache: dict[tuple[float, float], tuple[float, dict]] = {}
_weather_cache_lock = threading.Lock()


def _num(value, default: float = 0.0) -> float:
    """Open-Meteo can return null for missing values."""
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def build_weather_advice(current: dict, days: list[dict]) -> list[dict]:
    """Simple rule-based farming tips. Returns codes; the website translates."""
    advice: list[dict] = []

    def first_day(predicate, limit: int | None = None):
        for day in days[:limit]:
            if predicate(day):
                return day["date"]
        return None

    heavy_rain = first_day(lambda d: d["rain_mm"] >= 30)

    if heavy_rain:
        advice.append({"code": "heavy_rain", "date": heavy_rain})
    else:
        rain_soon = first_day(
            lambda d: d["rain_mm"] >= 5 or d["rain_probability"] >= 60, 3
        )

        if rain_soon:
            advice.append({"code": "rain_expected", "date": rain_soon})

    hot_day = first_day(lambda d: d["temp_max"] >= 38)

    if hot_day:
        advice.append({"code": "heat", "date": hot_day})

    cold_day = first_day(lambda d: d["temp_min"] <= 8)

    if cold_day:
        advice.append({"code": "cold", "date": cold_day})

    windy_day = first_day(lambda d: d["wind_max"] >= 30)

    if windy_day:
        advice.append({"code": "strong_wind", "date": windy_day})

    if (
        current["humidity"] >= 85
        and 18 <= current["temperature"] <= 32
    ):
        advice.append({"code": "humid_disease", "date": None})

    if (
        len(days) >= 5
        and all(d["rain_mm"] < 1 for d in days)
        and max(d["temp_max"] for d in days) >= 33
    ):
        advice.append({"code": "dry_spell", "date": None})

    spray_day = first_day(
        lambda d: d["rain_mm"] < 1
        and d["rain_probability"] <= 30
        and d["wind_max"] <= 15,
        2,
    )

    if spray_day:
        advice.append({"code": "good_spray_window", "date": spray_day})

    return advice


# --------------------------------------------------
# Devanagari (Marathi/Hindi) to Latin Location Mapping
# --------------------------------------------------
INDIAN_DISTRICT_TRANSLITERATION = {
    # Maharashtra Districts & Agricultural Centers
    "कोल्हापूर": "Kolhapur", "पुणे": "Pune", "नाशिक": "Nashik", "सातारा": "Satara",
    "सांगली": "Sangli", "सोलापूर": "Solapur", "नागपूर": "Nagpur", "अमरावती": "Amravati",
    "औरंगाबाद": "Aurangabad", "छत्रपती संभाजीनगर": "Aurangabad", "संभाजीनगर": "Aurangabad",
    "जळगाव": "Jalgaon", "धुळे": "Dhule", "नंदुरबार": "Nandurbar", "अहमदनगर": "Ahmednagar",
    "अहिल्यानगर": "Ahmednagar", "नांदेड": "Nanded", "लातूर": "Latur", "बीड": "Beed",
    "उस्मानाबाद": "Osmanabad", "धाराशिव": "Osmanabad", "परभणी": "Parbhani", "हिंगोली": "Hingoli",
    "जालना": "Jalna", "बुलढाणा": "Buldhana", "अकोला": "Akola", "वाशीम": "Washim",
    "वर्धा": "Wardha", "चंद्रपूर": "Chandrapur", "गडचिरोली": "Gadchiroli", "भंडारा": "Bhandara",
    "गोंदिया": "Gondia", "ठाणे": "Thane", "पालघर": "Palghar", "रायगड": "Raigad",
    "रत्नागिरी": "Ratnagiri", "सिंधुदुर्ग": "Sindhudurg", "मुंबई": "Mumbai",
    "बारामती": "Baramati", "कराड": "Karad", "मालेगाव": "Malegaon", "पंढरपूर": "Pandharpur",
    "शिर्डी": "Shirdi", "इचलकरंजी": "Ichalkaranji", "अकलूज": "Akluj", "फलटण": "Phaltan",
    "श्रीरामपूर": "Shrirampur", "संगमनेर": "Sangamner", "निफाड": "Niphad", "येवला": "Yeola",
    "राहुरी": "Rahuri", "शिरूर": "Shirur", "इंदापूर": "Indapur", "दौंड": "Daund",
    "वाई": "Wai", "कऱ्हाड": "Karad", "इस्लामपूर": "Islampur", "विटा": "Vita",
    "तासगाव": "Tasgaon", "मिरज": "Miraj", "बार्शी": "Barshi", "करमाळा": "Karmala",
    # Hindi Heartland Agricultural Hubs
    "भोपाल": "Bhopal", "इंदौर": "Indore", "ग्वालियर": "Gwalior", "जबलपुर": "Jabalpur",
    "उज्जैन": "Ujjain", "जयपुर": "Jaipur", "जोधपुर": "Jodhpur", "कोटा": "Kota",
    "उदयपुर": "Udaipur", "लखनऊ": "Lucknow", "कानपुर": "Kanpur", "वाराणसी": "Varanasi",
    "प्रयागराज": "Prayagraj", "इलाहाबाद": "Prayagraj", "आगरा": "Agra", "मेरठ": "Meerut",
    "पटना": "Patna", "गया": "Gaya", "मुजफ्फरपुर": "Muzaffarpur", "रायपुर": "Raipur",
    "रांची": "Ranchi", "चंडीगढ़": "Chandigarh", "लुधियाना": "Ludhiana", "अमृतसर": "Amritsar",
}


def resolve_place_query(query: str) -> str:
    """Resolve Marathi / Hindi Devanagari queries to English for Open-Meteo geocoding."""
    cleaned = query.strip()
    if cleaned in INDIAN_DISTRICT_TRANSLITERATION:
        return INDIAN_DISTRICT_TRANSLITERATION[cleaned]
    for dev, eng in INDIAN_DISTRICT_TRANSLITERATION.items():
        if dev in cleaned or cleaned in dev:
            return eng
    return cleaned


@app.get("/weather/places")
def search_weather_places(
    q: str,
    language: str = "en",
    current_user_id: int | None = Depends(get_optional_user_id),
):
    query = q.strip()

    if len(query) < 2 or len(query) > 80:
        raise HTTPException(
            status_code=400,
            detail="Place name must be between 2 and 80 characters.",
        )

    resolved_query = resolve_place_query(query)

    try:
        response = httpx.get(
            OPEN_METEO_GEOCODING_URL,
            params={
                "name": resolved_query,
                "count": 10,
                "language": "en" if resolved_query != query else language,
                "format": "json",
            },
            timeout=8.0,
        )
        response.raise_for_status()
        results = response.json().get("results") or []
    except (httpx.HTTPError, ValueError):
        raise HTTPException(
            status_code=502,
            detail="The location service is not available right now.",
        )

    # Show places in India first.
    results.sort(key=lambda item: item.get("country_code") != "IN")

    return {
        "places": [
            {
                "name": item.get("name", ""),
                "admin1": item.get("admin1") or "",
                "country": item.get("country") or "",
                "latitude": item["latitude"],
                "longitude": item["longitude"],
            }
            for item in results[:6]
            if "latitude" in item and "longitude" in item
        ]
    }


@app.get("/weather")
def get_weather(
    lat: float,
    lon: float,
    current_user_id: int | None = Depends(get_optional_user_id),
):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="Invalid location.")

    cache_key = (round(lat, 2), round(lon, 2))
    now = time.monotonic()

    with _weather_cache_lock:
        cached = _weather_cache.get(cache_key)

        if cached and now - cached[0] < WEATHER_CACHE_SECONDS:
            return cached[1]

    try:
        response = httpx.get(
            OPEN_METEO_FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": (
                    "temperature_2m,relative_humidity_2m,"
                    "precipitation,weather_code,wind_speed_10m"
                ),
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,precipitation_probability_max,"
                    "wind_speed_10m_max"
                ),
                "forecast_days": 5,
                "timezone": "auto",
            },
            timeout=10.0,
        )
        response.raise_for_status()
        raw = response.json()

        raw_current = raw["current"]
        raw_daily = raw["daily"]

        current = {
            "temperature": _num(raw_current.get("temperature_2m")),
            "humidity": _num(raw_current.get("relative_humidity_2m")),
            "rain_mm": _num(raw_current.get("precipitation")),
            "wind_kmh": _num(raw_current.get("wind_speed_10m")),
            "code": int(_num(raw_current.get("weather_code"))),
        }

        days = []

        for index, date in enumerate(raw_daily["time"]):
            days.append(
                {
                    "date": date,
                    "code": int(_num(raw_daily["weather_code"][index])),
                    "temp_max": _num(raw_daily["temperature_2m_max"][index]),
                    "temp_min": _num(raw_daily["temperature_2m_min"][index]),
                    "rain_mm": _num(raw_daily["precipitation_sum"][index]),
                    "rain_probability": _num(
                        raw_daily["precipitation_probability_max"][index]
                    ),
                    "wind_max": _num(raw_daily["wind_speed_10m_max"][index]),
                }
            )
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        raise HTTPException(
            status_code=502,
            detail="The weather service is not available right now.",
        )

    payload = {
        "current": current,
        "daily": days,
        "advice": build_weather_advice(current, days),
    }

    with _weather_cache_lock:
        if len(_weather_cache) > 500:
            _weather_cache.clear()

        _weather_cache[cache_key] = (now, payload)

    return payload


@app.get("/")
def root():
    return {
        "message": "Welcome to KrishiAI 🌱",
        "status": "running",
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "KrishiAI API",
    }


@app.get("/health/database")
def database_health():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

        return {
            "status": "healthy",
            "database": "connected",
        }

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database connection failed",
        )


# --------------------------------------------------
# Analyze crop
# --------------------------------------------------


MAX_CROP_LENGTH = 100
MAX_PROBLEM_LENGTH = 2000
MAX_WEATHER_LENGTH = 800
DAILY_ANALYSIS_QUOTA = 20


@app.post("/analyze")
async def analyze_crop(
    request: Request,
    crop: str = Form(""),
    problem: str = Form(""),
    language: str = Form("en"),
    weather_context: str = Form(""),
    image: UploadFile | None = File(None),
    current_user_id: int | None = Depends(get_optional_user_id),
):
    language_code = language.strip().lower()

    if language_code not in LANGUAGES:
        language_code = "en"

    selected = LANGUAGES[language_code]
    selected_language = selected["name"]

    crop = crop.strip()
    problem = problem.strip()
    weather_context = weather_context.strip()
    if len(weather_context) > MAX_WEATHER_LENGTH:
        weather_context = weather_context[:MAX_WEATHER_LENGTH]

    if len(crop) > MAX_CROP_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Crop name must be {MAX_CROP_LENGTH} characters or less.",
        )

    if len(problem) > MAX_PROBLEM_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Problem description must be {MAX_PROBLEM_LENGTH} characters or less.",
        )

    if not crop and not problem and not image:
        raise HTTPException(
            status_code=400,
            detail="Please provide a crop name, problem description, or image.",
        )

    # Check 24-hour analysis quota
    client_ip = get_client_ip(request)
    if current_user_id is not None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM crop_analyses
                    WHERE user_id = %s
                      AND created_at >= NOW() - INTERVAL '24 hours'
                    """,
                    (current_user_id,),
                )
                count_24h = cursor.fetchone()[0]

        if count_24h >= DAILY_ANALYSIS_QUOTA:
            raise HTTPException(
                status_code=429,
                detail=f"Daily analysis quota reached ({DAILY_ANALYSIS_QUOTA} per 24 hours). Please try again tomorrow.",
            )
    else:
        # Protect Gemini quota for guests via IP rate limiting
        check_guest_analysis_rate_limit(client_ip)

    crop_details = crop if crop else "Not specified"

    problem_details = (
        problem
        if problem
        else (
            "No written description provided. "
            "Analyze the uploaded image if available."
        )
    )

    weather_details = (
        weather_context
        if weather_context
        else "No local weather forecast data available."
    )

    # Read the uploaded image.
    image_bytes = None
    image_info = None

    if image:
        if not image.content_type or not image.content_type.startswith("image/"):
            raise HTTPException(
                status_code=400,
                detail="Please upload a valid image file.",
            )

        image_bytes = await image.read(MAX_IMAGE_BYTES + 1)

        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Image is too large. The maximum size is 5 MB.",
            )

        if not image_bytes:
            raise HTTPException(
                status_code=400,
                detail="The uploaded image is empty.",
            )

        image_info = {
            "filename": image.filename,
            "content_type": image.content_type,
        }

    # Gemini prompt.
    prompt = f"""
You are KrishiAI, an agricultural assistance AI for Indian farmers.

MANDATORY LANGUAGE RULE:
Write the entire answer in {selected_language}.
Selected language code: {language_code}.

Language requirements:
- For English, write natural, simple English.
- For Marathi, write natural Marathi using Devanagari script.
- For Hindi, write natural Hindi using Devanagari script.
- Translate every heading, explanation, recommendation, and warning.
- Do not answer in English when Marathi or Hindi is selected.
- Crop names and technical scientific terms may include familiar
  scientific names where helpful.
- Keep the answer understandable to an Indian farmer.

Crop:
{crop_details}

Farmer's description:
{problem_details}

Local farm weather forecast & conditions:
{weather_details}

Analyze the uploaded image even when the farmer has not provided
a written description. If the crop is not specified, identify it
only when reasonably possible and explain uncertainty.
If no image is attached, use the crop name and description.

Structure your answer with these sections, translated into the
selected language:

1. Possible crop problem or disease
2. Symptoms observed
3. Recommended actions
4. Prevention measures
5. Weather-related considerations, if relevant (incorporate the local farm weather data provided above, such as rainfall, humidity, wind or heat risks, into your advice regarding spraying timing, irrigation, or fungal disease prevention)

Important safety requirements:
- Do not claim certainty about a disease from an image alone.
- Explain that visual identification may be incorrect.
- Do not invent symptoms unsupported by the input.
- Do not recommend pesticide products or dosages unless reliable,
  crop-specific guidance supports them.
- Encourage checking product labels and consulting a qualified
  agricultural expert or Krishi Vigyan Kendra (KVK).
- Never provide specific pesticide or fertilizer dosages based
  only on an image or an unconfirmed diagnosis.
- Do not recommend burning infected plant material.
- Recommend safe general practices and qualified local advice.
- Clearly distinguish possible causes from confirmed diagnoses.
- This guidance does not replace professional agricultural advice.

Return the complete response in {selected_language}.
"""

    contents = [prompt]

    if image_bytes and image:
        contents.append({
            "inline_data": {
                "mime_type": image.content_type,
                "data": image_bytes,
            }
        })

    # Call Gemini with candidate models and retries for resilience
    response = None
    last_exception = None

    for model_name in CANDIDATE_MODELS:
        for attempt in range(2):
            try:
                logger.info(
                    f"Gemini attempt {attempt + 1}/2; "
                    f"language={language_code}, model={model_name}"
                )

                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=contents,
                )

                if response and response.text:
                    break
            except Exception as exc:
                last_exception = exc
                logger.warning(
                    f"Gemini model {model_name} attempt {attempt + 1} failed: {exc}"
                )

                err_str = str(exc).lower()
                # If model is deprecated or not found (404), break immediately to try next candidate
                if "404" in err_str or "not_found" in err_str or "no longer available" in err_str or "not found" in err_str:
                    logger.info(f"Model {model_name} deprecated/unavailable, falling back to next model...")
                    break

                if not is_transient_error(exc):
                    if "invalid" in err_str or "unsupported" in err_str or "400" in err_str:
                        raise HTTPException(
                            status_code=400,
                            detail="The image or prompt format could not be processed. Please upload a standard JPEG or PNG image.",
                        )
                    break

                if attempt < 1:
                    await asyncio.sleep(1.0)

        if response and response.text:
            break

    if not response or not response.text:
        logger.error(f"All candidate Gemini models failed. Last exception: {last_exception}")
        raise HTTPException(
            status_code=503,
            detail=selected["error"],
        )

    analysis_text = response.text

    # Save the successful analysis to PostgreSQL for authenticated users.
    analysis_id = None
    created_at = None

    if current_user_id is not None:
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO crop_analyses
                            (user_id, crop, problem, language, analysis)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id, created_at
                        """,
                        (
                            current_user_id,
                            crop or None,
                            problem or None,
                            language_code,
                            analysis_text,
                        ),
                    )

                    saved_record = cursor.fetchone()

            analysis_id, created_at = saved_record

        except Exception as exc:
            logger.error(f"Failed to save crop analysis: {exc}")

    return {
        "id": analysis_id,
        "crop": crop,
        "language": selected_language,
        "language_code": language_code,
        "image": image_info,
        "analysis": analysis_text,
        "disclaimer": selected["disclaimer"],
        "created_at": created_at.isoformat() if created_at else None,
        "saved_to_history": bool(current_user_id and analysis_id),
        "is_guest": current_user_id is None,
    }


# --------------------------------------------------
# Retrieve and manage crop analysis history
# --------------------------------------------------

@app.get("/history")
def get_crop_history(
    limit: int = 20,
    current_user_id: int = Depends(get_current_user_id),
):
    limit = max(1, min(limit, 100))

    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT id, crop, problem, language, analysis, created_at
                    FROM crop_analyses
                    WHERE user_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (current_user_id, limit),
                )

                records = cursor.fetchall()

        history = []

        for record in records:
            item = dict(record)

            if item["created_at"]:
                item["created_at"] = item["created_at"].isoformat()

            history.append(item)

        return {
            "count": len(history),
            "history": history,
        }

    except Exception as exc:
        logger.error(f"Failed to retrieve crop history: {exc}")

        raise HTTPException(
            status_code=503,
            detail="Unable to retrieve crop history.",
        )


@app.delete("/history")
def clear_user_history(current_user_id: int = Depends(get_current_user_id)):
    """Delete all past crop analyses for the current authenticated user."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM crop_analyses WHERE user_id = %s",
                    (current_user_id,),
                )
                deleted_count = cursor.rowcount
        logger.info(f"Cleared {deleted_count} history records for user {current_user_id}")
        return {
            "status": "ok",
            "message": "All crop analysis history has been deleted.",
            "deleted_count": deleted_count,
        }
    except Exception as exc:
        logger.error(f"Failed to clear history for user {current_user_id}: {exc}")
        raise HTTPException(status_code=500, detail="Could not clear history.")


@app.delete("/history/{item_id}")
def delete_single_history_item(
    item_id: int,
    current_user_id: int = Depends(get_current_user_id),
):
    """Delete a specific crop analysis record belonging to the current user."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM crop_analyses WHERE id = %s AND user_id = %s",
                    (item_id, current_user_id),
                )
                if cursor.rowcount == 0:
                    raise HTTPException(
                        status_code=404,
                        detail="Analysis record not found.",
                    )
        return {"status": "ok", "message": "Record deleted successfully."}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to delete history record {item_id}: {exc}")
        raise HTTPException(status_code=500, detail="Could not delete record.")


@app.delete("/auth/me")
def delete_user_account(current_user_id: int = Depends(get_current_user_id)):
    """Permanently delete farmer account and all associated crop analyses."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM crop_analyses WHERE user_id = %s",
                    (current_user_id,),
                )
                cursor.execute(
                    "DELETE FROM users WHERE id = %s",
                    (current_user_id,),
                )
        logger.info(f"Account permanently deleted for user {current_user_id}")
        return {
            "status": "ok",
            "message": "Account and all personal data deleted permanently.",
        }
    except Exception as exc:
        logger.error(f"Failed to delete account for user {current_user_id}: {exc}")
        raise HTTPException(status_code=500, detail="Could not delete account.")


# --------------------------------------------------
# Fertilizer & Dosage Calculator
# --------------------------------------------------

class FertilizerCalculateRequest(BaseModel):
    crop: str
    area: float
    unit: str = "acre"  # "acre", "guntha", "hectare"


# ICAR & State Agricultural Universities Recommended Doses of Fertilizer (RDF) per acre in kg:
# (N, P2O5, K2O, organic_fym_tonnes, special_nutrient)
CROP_FERTILIZER_RDF = {
    "wheat": {
        "names": {"en": "Wheat", "mr": "गहू", "hi": "गेहूं"},
        "icon": "🌾",
        "n": 48.0, "p": 24.0, "k": 16.0,
        "fym_tonnes": 4.0,
        "special_nutrient": {"en": "Zinc Sulphate (10 kg/acre)", "mr": "झिंक सल्फेट (१० किलो/एकर)", "hi": "जिंक सल्फेट (10 किग्रा/एकड़)"},
        "stages": [
            {"en": "Basal (At Sowing): Full DAP + Full MOP + 1/3rd Urea", "mr": "पेरणीवेळी (पायाभूत खत): संपूर्ण DAP + संपूर्ण MOP + १/३ युरिया", "hi": "बुआई के समय: पूरा DAP + पूरा MOP + 1/3 यूरिया"},
            {"en": "First Irrigation (20-25 days - CRI stage): 1/3rd Urea", "mr": "पहिले पाणी (२०-२५ दिवस): १/३ युरिया", "hi": "पहली सिंचाई (20-25 दिन): 1/3 यूरिया"},
            {"en": "Tillering / Jointing stage (40-45 days): Remaining 1/3rd Urea", "mr": "फुटवे फुटताना (४०-४५ दिवस): उर्वरित १/३ युरिया", "hi": "कल्ले फूटते समय (40-45 दिन): शेष 1/3 यूरिया"},
        ],
        "organic_tips": {
            "en": "Treat seeds with Azotobacter and PSB culture before sowing. Incorporate 4 tonnes of Farm Yard Manure (FYM) during land preparation.",
            "mr": "पेरणीपूर्वी बियाण्यास ॲझोटोबॅक्टर आणि पीएसबी जीवाणू संवर्धकाची बीजप्रक्रिया करा. नांगरणीच्या वेळी ४ टन शेणखत जमिनीत मिसळा.",
            "hi": "बुआई से पहले बीजों को एजोटोबैक्टर और पीएसबी से उपचारित करें। खेत की तैयारी के समय 4 टन गोबर की खाद मिलाएँ।"
        }
    },
    "rice": {
        "names": {"en": "Rice / Paddy", "mr": "भात (धान)", "hi": "चावल / धान"},
        "icon": "🍚",
        "n": 40.0, "p": 20.0, "k": 20.0,
        "fym_tonnes": 5.0,
        "special_nutrient": {"en": "Zinc Sulphate (10 kg/acre at transplanting)", "mr": "झिंक सल्फेट (१० किलो/एकर पुनर्लागवडीवेळी)", "hi": "जिंक सल्फेट (10 किग्रा/एकड़ रोपाई के समय)"},
        "stages": [
            {"en": "Transplanting (Basal): 50% Urea + 100% DAP + 100% MOP", "mr": "लागवड वेळ (पायाभूत): ५०% युरिया + १००% DAP + १००% MOP", "hi": "रोपाई के समय: 50% यूरिया + 100% DAP + 100% MOP"},
            {"en": "Active Tillering (20-25 days after transplanting): 25% Urea", "mr": "फुटवे फुटण्याची वेळ (२०-२५ दिवस): २५% युरिया", "hi": "कल्ले फूटते समय (20-25 दिन बाद): 25% यूरिया"},
            {"en": "Panicle Initiation (40-45 days): Remaining 25% Urea", "mr": "लोंबी बाहेर पडताना (४०-४५ दिवस): उर्वरित २५% युरिया", "hi": "बाली निकलने की शुरुआत (40-45 दिन): शेष 25% यूरिया"},
        ],
        "organic_tips": {
            "en": "Use Azolla or Blue-Green Algae (BGA) in standing water as biofertilizer to fix Nitrogen naturally and save 25% chemical urea.",
            "mr": "पाण्यात अझोला किंवा नीलहरित शेवाळ वापरा, यामुळे हवेतील नत्र जमिनीत स्थिरावते आणि २५% युरियाची बचत होते.",
            "hi": "खड़े पानी में अजोला या नील-हरित शैवाल का प्रयोग करें, इससे प्राकृतिक नाइट्रोजन मिलती है और 25% यूरिया बचता है।"
        }
    },
    "sugarcane": {
        "names": {"en": "Sugarcane", "mr": "ऊस", "hi": "गन्ना"},
        "icon": "🎋",
        "n": 100.0, "p": 45.0, "k": 45.0,
        "fym_tonnes": 10.0,
        "special_nutrient": {"en": "Ferrous Sulphate (10 kg) + Zinc Sulphate (8 kg/acre)", "mr": "फेरस सल्फेट (१० किलो) + झिंक सल्फेट (८ किलो/एकर)", "hi": "फेरस सल्फेट (10 किग्रा) + जिंक सल्फेट (8 किग्रा/एकड़)"},
        "stages": [
            {"en": "Planting (Basal): 10% Urea + 50% DAP + 50% MOP", "mr": "लागवडीवेळी (पायाभूत): १०% युरिया + ५०% DAP + ५०% MOP", "hi": "बुआई के समय: 10% यूरिया + 50% DAP + 50% MOP"},
            {"en": "6 to 8 Weeks (Tillering): 40% Urea + Remaining 50% DAP", "mr": "६ ते ८ आठवडे (फुटवे फुटताना): ४०% युरिया + उर्वरित ५०% DAP", "hi": "6 से 8 सप्ताह (कल्ले): 40% यूरिया + शेष 50% DAP"},
            {"en": "Earthing up (12-16 weeks - Grand Growth): Remaining 50% Urea + 50% MOP", "mr": "मोठी बांधणी (१२-१६ आठवडे): उर्वरित ५०% युरिया + ५०% MOP", "hi": "मिट्टी चढ़ाते समय (12-16 सप्ताह): शेष 50% यूरिया + 50% MOP"},
        ],
        "organic_tips": {
            "en": "Apply 10 tonnes of FYM or press mud (मळी खत). Inoculate setts with Acetobacter biofertilizer to promote vigorous cane thickness.",
            "mr": "१० टन चांगले कुजलेले शेणखत किंवा कारखान्याची मळी वापरा. उसाच्या कांड्यांना ॲसिटोबॅक्टर जीवाणूची प्रक्रिया करा.",
            "hi": "10 टन सड़ी गोबर की खाद या प्रेसमड का उपयोग करें। गन्ने के टुकड़ों को एसीटोबैक्टर जीवाणु से उपचारित करें।"
        }
    },
    "cotton": {
        "names": {"en": "Cotton (Kapas)", "mr": "कापूस", "hi": "कपास"},
        "icon": "☁️",
        "n": 48.0, "p": 24.0, "k": 24.0,
        "fym_tonnes": 5.0,
        "special_nutrient": {"en": "Magnesium Sulphate (10 kg) + Boron (2 kg/acre)", "mr": "मॅग्नेशियम सल्फेट (१० किलो) + बोरॉन (२ किलो/एकर)", "hi": "मैग्नीशियम सल्फेट (10 किग्रा) + बोरॉन (2 किग्रा/एकड़)"},
        "stages": [
            {"en": "At Sowing (Basal): 20% Urea + 100% DAP + 50% MOP", "mr": "पेरणीवेळी (पायाभूत): २०% युरिया + १००% DAP + ५०% MOP", "hi": "बुआई के समय: 20% यूरिया + 100% DAP + 50% MOP"},
            {"en": "Square Formation (30-35 days): 40% Urea", "mr": "पात्या लागताना (३०-३५ दिवस): ४०% युरिया", "hi": "फूल कलियां बनते समय (30-35 दिन): 40% यूरिया"},
            {"en": "Peak Flowering / Boll Development (60-70 days): 40% Urea + 50% MOP", "mr": "फुलोरा व बोंडे भरताना (६०-७० दिवस): ४०% युरिया + ५०% MOP", "hi": "फूल व गूलर बनते समय (60-70 दिन): 40% यूरिया + 50% MOP"},
        ],
        "organic_tips": {
            "en": "Apply 250 kg Neem cake per acre. Neem cake acts as a natural nitrification inhibitor and deters soil pests like white grubs.",
            "mr": "प्रति एकरी २५० किलो निंबोळी पेंड वापरा. यामुळे युरिया वाया जात नाही आणि हुमणीसारख्या किडींचा बंदोबस्त होतो.",
            "hi": "प्रति एकड़ 250 किग्रा नीम की खली डालें। इससे यूरिया का नुकसान कम होता है और दीमक/सफेद लट से बचाव होता है।"
        }
    },
    "soybean": {
        "names": {"en": "Soybean", "mr": "सोयाबीन", "hi": "सोयाबीन"},
        "icon": "🌱",
        "n": 12.0, "p": 32.0, "k": 16.0,
        "fym_tonnes": 4.0,
        "special_nutrient": {"en": "Sulphur (8-10 kg/acre for higher oil content)", "mr": "सल्फर / गंधक (८-१० किलो/एकर तेलाचे प्रमाण वाढवण्यासाठी)", "hi": "सल्फर / गंधक (8-10 किग्रा/एकड़ तेल की मात्रा बढ़ाने के लिए)"},
        "stages": [
            {"en": "At Sowing (Basal): 100% DAP + 100% MOP + Starter Urea", "mr": "पेरणीवेळी (पायाभूत): १००% DAP + १००% MOP + सुरुवातीचा युरिया", "hi": "बुआई के समय: 100% DAP + 100% MOP + शुरुआती यूरिया"},
            {"en": "Flowering stage (30-35 days): Foliar spray of 19:19:19 or 00:52:34 (1 kg/acre in 150L water)", "mr": "फुलोऱ्याच्या वेळी (३०-३५ दिवस): १९:१९:१९ किंवा ००:५२:३४ ची फवारणी (१ किलो/एकर)", "hi": "फूल आते समय (30-35 दिन): 19:19:19 या 00:52:34 का छिड़काव (1 किग्रा/एकड़)"},
        ],
        "organic_tips": {
            "en": "Soybean is a legume that fixes nitrogen. Never overdose on chemical urea as it reduces root nodules. Treat seeds with Bradyrhizobium japonicum.",
            "mr": "सोयाबीन हे कडधान्य पीक स्वतःचे नत्र तयार करते. जास्त युरिया दिल्यास गाठी तयार होत नाहीत. ब्राडीरायझोबियम जीवाणूची बीजप्रक्रिया आवर्जून करा.",
            "hi": "सोयाबीन दलहनी फसल है जो वायुमंडलीय नाइट्रोजन स्वयं लेती है। अधिक यूरिया न डालें। ब्रैडीराइजोबियम कल्चर से बीज उपचार करें।"
        }
    },
    "tomato": {
        "names": {"en": "Tomato", "mr": "टोमॅटो", "hi": "टमाटर"},
        "icon": "🍅",
        "n": 60.0, "p": 40.0, "k": 40.0,
        "fym_tonnes": 8.0,
        "special_nutrient": {"en": "Calcium Nitrate (10 kg) + Boron (1.5 kg/acre - prevents fruit cracking)", "mr": "कॅल्शियम नायट्रेट (१० किलो) + बोरॉन (१.५ किलो/एकर - फळे तडकणे रोखण्यासाठी)", "hi": "कैल्शियम नाइट्रेट (10 किग्रा) + बोरॉन (1.5 किग्रा/एकड़ - फल फटने से रोकने हेतु)"},
        "stages": [
            {"en": "Transplanting (Basal): 1/3rd Urea + 100% DAP + 50% MOP", "mr": "लागवडीवेळी (पायाभूत): १/३ युरिया + १००% DAP + ५०% MOP", "hi": "रोपाई के समय: 1/3 यूरिया + 100% DAP + 50% MOP"},
            {"en": "Vegetative Growth (25-30 days): 1/3rd Urea", "mr": "शाकीय वाढीच्या वेळी (२५-३० दिवस): १/३ युरिया", "hi": "वानस्पतिक वृद्धि (25-30 दिन): 1/3 यूरिया"},
            {"en": "Fruiting / Harvest (45-50 days): Remaining 1/3rd Urea + 50% MOP", "mr": "फळे लागताना (४५-५० दिवस): उर्वरित १/३ युरिया + ५०% MOP", "hi": "फल विकास (45-50 दिन): शेष 1/3 यूरिया + 50% MOP"},
        ],
        "organic_tips": {
            "en": "Incorporate 8 tonnes of FYM enriched with Trichoderma harzianum to protect against wilt and root rot diseases.",
            "mr": "मर रोग आणि मूळ कुज रोखण्यासाठी ट्रायकोडर्मायुक्त शेणखत ८ टन जमिनीत मिसळा.",
            "hi": "उकठा और जड़ सड़न से बचाव के लिए ट्राइकोडर्मा युक्त 8 टन सड़ी गोबर खाद खेत में मिलाएँ।"
        }
    },
    "onion": {
        "names": {"en": "Onion", "mr": "कांदा", "hi": "प्याज"},
        "icon": "🧅",
        "n": 40.0, "p": 20.0, "k": 24.0,
        "fym_tonnes": 8.0,
        "special_nutrient": {"en": "Elemental Sulphur (15 kg/acre - essential for pungent flavor and storage quality)", "mr": "सल्फर / गंधक (१५ किलो/एकर - कांद्याचा तिखटपणा व टिकाऊ क्षमता वाढवण्यासाठी)", "hi": "सल्फर / गंधक (15 किग्रा/एकड़ - प्याज का तीखापन और भंडारण क्षमता बढ़ाने के लिए)"},
        "stages": [
            {"en": "Transplanting (Basal): 50% Urea + 100% DAP + 100% MOP + Sulphur", "mr": "लागवडीवेळी (पायाभूत): ५०% युरिया + १००% DAP + १००% MOP + गंधक", "hi": "रोपाई के समय: 50% यूरिया + 100% DAP + 100% MOP + सल्फर"},
            {"en": "Bulb Initiation (30-45 days): Remaining 50% Urea", "mr": "कांदा पोसताना (३०-४५ दिवस): उर्वरित ५०% युरिया", "hi": "गांठ बनते समय (30-45 दिन): शेष 50% यूरिया"},
        ],
        "organic_tips": {
            "en": "Avoid excess nitrogen after 60 days, as it causes thick necks (डोंगळे) and poor storage life. Apply Vermicompost (1-2 tonnes/acre).",
            "mr": "६० दिवसांनंतर युरिया देणे टाळा, अन्यथा कांद्याची मान जाड होते (डोंगळे) आणि टिकवण क्षमता घटते. गांडूळ खत वापरा.",
            "hi": "60 दिनों के बाद यूरिया न दें, नहीं तो प्याज की गर्दन मोटी हो जाती है और भंडारण में खराब होता है। केंचुआ खाद डालें।"
        }
    },
    "maize": {
        "names": {"en": "Maize (Corn)", "mr": "मका", "hi": "मक्का"},
        "icon": "🌽",
        "n": 48.0, "p": 24.0, "k": 16.0,
        "fym_tonnes": 5.0,
        "special_nutrient": {"en": "Zinc Sulphate (10 kg/acre)", "mr": "झिंक सल्फेट (१० किलो/एकर)", "hi": "जिंक सल्फेट (10 किग्रा/एकड़)"},
        "stages": [
            {"en": "At Sowing (Basal): 1/3rd Urea + 100% DAP + 100% MOP", "mr": "पेरणीवेळी (पायाभूत): १/३ युरिया + १००% DAP + १००% MOP", "hi": "बुआई के समय: 1/3 यूरिया + 100% DAP + 100% MOP"},
            {"en": "Knee-high Stage (30-35 days): 1/3rd Urea", "mr": "गुडघाभर उंचीच्या वेळी (३०-३५ दिवस): १/३ युरिया", "hi": "घुटने तक ऊंचाई होने पर (30-35 दिन): 1/3 यूरिया"},
            {"en": "Tasseling / Silking (50-55 days): Remaining 1/3rd Urea", "mr": "तुरा व कणसे बाहेर पडताना (५०-५५ दिवस): उर्वरित १/३ युरिया", "hi": "मंजरी और भुट्टा निकलते समय (50-55 दिन): शेष 1/3 यूरिया"},
        ],
        "organic_tips": {
            "en": "Maize is an exhaustive crop. Add 5 tonnes FYM per acre. Azotobacter biofertilizer saves about 15-20 kg N/acre.",
            "mr": "मका जमिनीतील अन्नद्रव्ये जास्त प्रमाणात शोषून घेतो. ५ टन शेणखत आणि ॲझोटोबॅक्टर जिवाणू संवर्धक वापरा.",
            "hi": "मक्का भूमि से अधिक पोषक तत्व खींचती है। 5 टन गोबर खाद व एजोटोबैक्टर का उपयोग अवश्य करें।"
        }
    },
    "potato": {
        "names": {"en": "Potato", "mr": "बटाटा", "hi": "आलू"},
        "icon": "🥔",
        "n": 48.0, "p": 32.0, "k": 40.0,
        "fym_tonnes": 8.0,
        "special_nutrient": {"en": "Magnesium Sulphate (10 kg/acre)", "mr": "मॅग्नेशियम सल्फेट (१० किलो/एकर)", "hi": "मैग्नीशियम सल्फेट (10 किग्रा/एकड़)"},
        "stages": [
            {"en": "At Planting (Basal): 50% Urea + 100% DAP + 100% MOP", "mr": "लागवडीवेळी (पायाभूत): ५०% युरिया + १००% DAP + १००% MOP", "hi": "बुआई के समय: 50% यूरिया + 100% DAP + 100% MOP"},
            {"en": "Earthing Up (30-35 days): Remaining 50% Urea", "mr": "माती भरताना (३०-३५ दिवस): उर्वरित ५०% युरिया", "hi": "मिट्टी चढ़ाते समय (30-35 दिन): शेष 50% यूरिया"},
        ],
        "organic_tips": {
            "en": "Potatoes are heavy potassium and organic matter feeders. Always incorporate well-rotted FYM (8 tonnes) or Vermicompost (2 tonnes).",
            "mr": "बटाट्याला पोटॅश आणि सेंद्रिय खतांची जास्त गरज असते. चांगल्या कुजलेल्या शेणखताचा किंवा गांडूळ खताचा भरपूर वापर करा.",
            "hi": "आलू को पोटाश और जैविक खाद की अधिक आवश्यकता होती है। 8 टन सड़ी गोबर खाद या 2 टन केंचुआ खाद प्रयोग करें।"
        }
    }
}


def compute_fertilizer_recommendation(crop: str, area: float, unit: str = "acre") -> dict:
    crop_key = crop.strip().lower()
    if crop_key not in CROP_FERTILIZER_RDF:
        raise ValueError(
            f"Unsupported crop '{crop}'. Supported crops: {', '.join(CROP_FERTILIZER_RDF.keys())}"
        )

    if area <= 0 or area > 10000:
        raise ValueError("Area must be a positive number greater than 0 and up to 10,000.")

    unit_norm = unit.strip().lower()
    # Normalize area to standard acres
    if unit_norm in ("acre", "acres"):
        acres = float(area)
    elif unit_norm in ("guntha", "gunthas", "gunthe"):
        acres = float(area) * 0.025  # 1 acre = 40 gunthas
    elif unit_norm in ("hectare", "hectares", "ha"):
        acres = float(area) * 2.47105  # 1 ha = 2.47105 acres
    else:
        raise ValueError("Invalid unit. Please choose 'acre', 'guntha', or 'hectare'.")

    crop_info = CROP_FERTILIZER_RDF[crop_key]

    # Required pure N-P-K nutrients in kg
    total_n_kg = round(crop_info["n"] * acres, 1)
    total_p_kg = round(crop_info["p"] * acres, 1)
    total_k_kg = round(crop_info["k"] * acres, 1)

    # 1 bag DAP (50 kg) provides 23 kg P2O5 and 9 kg N
    # 1 bag Neem-coated Urea (45 kg) provides 20.7 kg N (46%)
    # 1 bag MOP (50 kg) provides 30 kg K2O (60%)
    dap_bags = round(total_p_kg / 23.0, 1)
    dap_kg = round(dap_bags * 50.0, 1)

    n_from_dap = dap_bags * 9.0
    remaining_n_kg = max(0.0, total_n_kg - n_from_dap)
    urea_bags = round(remaining_n_kg / 20.7, 1)
    urea_kg = round(urea_bags * 45.0, 1)

    mop_bags = round(total_k_kg / 30.0, 1)
    mop_kg = round(mop_bags * 50.0, 1)

    fym_tonnes = round(crop_info["fym_tonnes"] * acres, 1)

    return {
        "crop_key": crop_key,
        "crop_name": crop_info["names"],
        "icon": crop_info["icon"],
        "input_area": area,
        "input_unit": unit_norm,
        "acres": round(acres, 2),
        "pure_nutrients_kg": {
            "nitrogen_n": total_n_kg,
            "phosphorus_p": total_p_kg,
            "potassium_k": total_k_kg,
        },
        "commercial_bags": {
            "urea": {
                "bags": urea_bags,
                "weight_kg": urea_kg,
                "bag_size": "45 kg",
                "grade": "46% N (Neem Coated)",
            },
            "dap": {
                "bags": dap_bags,
                "weight_kg": dap_kg,
                "bag_size": "50 kg",
                "grade": "18:46:0",
            },
            "mop": {
                "bags": mop_bags,
                "weight_kg": mop_kg,
                "bag_size": "50 kg",
                "grade": "60% K₂O (Muriate of Potash)",
            },
        },
        "organic_fym_tonnes": fym_tonnes,
        "special_nutrient": crop_info["special_nutrient"],
        "stages": crop_info["stages"],
        "organic_tips": crop_info["organic_tips"],
        "disclaimer": "Recommended dosage based on ICAR recommended dose of fertilizers (RDF). Actual requirement varies with soil test results, crop variety, and agro-climatic conditions. Consult your local KVK for custom soil test based recommendations.",
    }


@app.post("/fertilizer/calculate")
def calculate_fertilizer(req: FertilizerCalculateRequest):
    try:
        return compute_fertilizer_recommendation(req.crop, req.area, req.unit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))