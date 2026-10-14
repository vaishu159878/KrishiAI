import os
import asyncio
from contextlib import contextmanager, asynccontextmanager
from pydantic import BaseModel, EmailStr

import psycopg2
from psycopg2.extras import RealDictCursor

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from pwdlib import PasswordHash
import jwt



# --------------------------------------------------
# Environment configuration
# --------------------------------------------------

load_dotenv()

AI_API_KEY = os.getenv("AI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")

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

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB
from collections import deque

# --------------------------------------------------
# Login rate limiting (in memory, per email address)
# --------------------------------------------------
# Keyed by email rather than IP: behind nginx every request would otherwise
# look like it comes from the same internal address. State is per process and
# resets when the backend restarts; run a single uvicorn worker or move this
# to Redis/the database before scaling out.

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 600  # 10 minutes

_login_failures: dict[str, deque] = {}
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


def record_login_failure(email: str) -> None:
    now = time.monotonic()

    with _login_lock:
        # Crude memory guard against many different made-up emails.
        if len(_login_failures) > 10000:
            for key in list(_login_failures):
                _recent_failures(key, now)

            if len(_login_failures) > 10000:
                _login_failures.clear()

        _login_failures.setdefault(
            email, deque(maxlen=LOGIN_MAX_FAILURES)
        ).append(now)


def clear_login_failures(email: str) -> None:
    with _login_lock:
        _login_failures.pop(email, None)


# Used so that unknown emails take as long to reject as wrong passwords,
# which stops response timing from revealing which emails are registered.
DUMMY_PASSWORD_HASH = hash_password("not-a-real-password-for-timing-only")


from datetime import datetime, timedelta, timezone


def create_access_token(user_id: int, username: str = "", email: str = "") -> str:
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(user_id),
        "username": username,
        "email": email,
        "iat": now,
        "exp": now + timedelta(minutes=30),
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



# --------------------------------------------------
# PostgreSQL connection
# --------------------------------------------------

@contextmanager
def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# --------------------------------------------------
# Database initialization
# --------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the history and users tables when the application starts.
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
                REFERENCES users(id) ON DELETE SET NULL
            """)


    yield


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
    version="1.2.0",
    lifespan=lifespan,
)

# --------------------------------------------------
# User registration
# --------------------------------------------------

@app.post("/auth/register", status_code=201)
def register_user(user: RegisterRequest):
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
def login_user(user: LoginRequest):
    email = str(user.email).strip().lower()

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
        record_login_failure(email)

        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    clear_login_failures(email)

    token = create_access_token(existing_user[0], existing_user[1], existing_user[2])

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 1800,
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


@app.get("/weather/places")
def search_weather_places(
    q: str,
    language: str = "en",
    current_user_id: int = Depends(get_current_user_id),
):
    query = q.strip()

    if len(query) < 2 or len(query) > 80:
        raise HTTPException(
            status_code=400,
            detail="Place name must be between 2 and 80 characters.",
        )

    if language not in {"en", "mr", "hi"}:
        language = "en"

    try:
        response = httpx.get(
            OPEN_METEO_GEOCODING_URL,
            params={
                "name": query,
                "count": 10,
                "language": language,
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
    current_user_id: int = Depends(get_current_user_id),
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


@app.post("/analyze")
async def analyze_crop(
    crop: str = Form(""),
    problem: str = Form(""),
    language: str = Form("en"),
    image: UploadFile | None = File(None),
    current_user_id: int = Depends(get_current_user_id),
):

    language_code = language.strip().lower()

    if language_code not in LANGUAGES:
        language_code = "en"

    selected = LANGUAGES[language_code]
    selected_language = selected["name"]

    crop = crop.strip()
    problem = problem.strip()

    if not crop and not problem and not image:
        raise HTTPException(
            status_code=400,
            detail="Please provide a crop name, problem description, or image.",
        )

    crop_details = crop if crop else "Not specified"

    problem_details = (
        problem
        if problem
        else (
            "No written description provided. "
            "Analyze the uploaded image if available."
        )
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
5. Weather-related considerations, if relevant

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

    # Call Gemini with retries.
    try:
        response = None

        for attempt in range(3):
            try:
                print(
                    f"Gemini attempt {attempt + 1}/3; "
                    f"language={language_code}"
                )

                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model="gemini-3.8-flash",
                    contents=contents,
                )

                if not response or not response.text:
                    raise RuntimeError(
                        "Gemini returned an empty response"
                    )

                break

            except Exception as exc:
                print(
                    f"Gemini attempt {attempt + 1} failed: {exc}"
                )

                if attempt < 2:
                    await asyncio.sleep(3)
                else:
                    raise

    except Exception as exc:
        print(f"Gemini API Error after retries: {exc}")

        return {
            "crop": crop,
            "language": selected_language,
            "language_code": language_code,
            "image": image_info,
            "analysis": selected["error"],
            "disclaimer": selected["disclaimer"],
            "error": "AI service temporarily unavailable",
        }

    analysis_text = response.text

    # Save the successful analysis to PostgreSQL.
    analysis_id = None
    created_at = None

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
        # Keep the AI result available if saving fails.
        print(f"Failed to save crop analysis: {exc}")

    return {
        "id": analysis_id,
        "crop": crop,
        "language": selected_language,
        "language_code": language_code,
        "image": image_info,
        "analysis": analysis_text,
        "disclaimer": selected["disclaimer"],
        "created_at": created_at.isoformat() if created_at else None,
    }


# --------------------------------------------------
# Retrieve crop analysis history
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
        print(f"Failed to retrieve crop history: {exc}")

        raise HTTPException(
            status_code=503,
            detail="Unable to retrieve crop history.",
        )


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


@app.post("/fertilizer/calculate")
def calculate_fertilizer(req: FertilizerCalculateRequest):
    crop_key = req.crop.strip().lower()
    if crop_key not in CROP_FERTILIZER_RDF:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported crop '{req.crop}'. Supported crops: {', '.join(CROP_FERTILIZER_RDF.keys())}",
        )

    if req.area <= 0 or req.area > 10000:
        raise HTTPException(
            status_code=400,
            detail="Area must be a positive number greater than 0 and up to 10,000.",
        )

    unit = req.unit.strip().lower()
    # Normalize area to acres
    if unit in ("acre", "acres"):
        acres = float(req.area)
    elif unit in ("guntha", "gunthas", "gunthe"):
        acres = float(req.area) * 0.025  # 1 acre = 40 gunthas
    elif unit in ("hectare", "hectares", "ha"):
        acres = float(req.area) * 2.47105  # 1 ha = 2.471 acres
    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid unit. Please choose 'acre', 'guntha', or 'hectare'.",
        )

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
        "input_area": req.area,
        "input_unit": unit,
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
                "grade": "46% N (Neem Coated)"
            },
            "dap": {
                "bags": dap_bags,
                "weight_kg": dap_kg,
                "bag_size": "50 kg",
                "grade": "18:46:0"
            },
            "mop": {
                "bags": mop_bags,
                "weight_kg": mop_kg,
                "bag_size": "50 kg",
                "grade": "60% K₂O (Muriate of Potash)"
            }
        },
        "organic_fym_tonnes": fym_tonnes,
        "special_nutrient": crop_info["special_nutrient"],
        "stages": crop_info["stages"],
        "organic_tips": crop_info["organic_tips"]
    }