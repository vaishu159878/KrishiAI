import os
import sys

test_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(test_dir, "..")))
sys.path.insert(0, os.path.abspath(os.path.join(test_dir, "..", "backend")))
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

os.environ.setdefault("AI_API_KEY", "test_gemini_api_key")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test_jwt_secret_key_for_testing_purposes_only_123456")

import pytest
from main import (
    validate_password,
    record_login_failure,
    login_retry_after,
    clear_login_failures,
    build_weather_advice,
    compute_fertilizer_recommendation,
    is_transient_error,
)


# ==============================================================================
# 1. Password Validation Rules
# ==============================================================================

def test_password_too_short():
    err = validate_password("abc12", "farmer1", "farmer1@example.com")
    assert err is not None
    assert "between 8 and 128" in err


def test_password_too_long():
    long_pass = "A" * 129
    err = validate_password(long_pass, "farmer1", "farmer1@example.com")
    assert err is not None
    assert "between 8 and 128" in err


def test_password_common_dictionary():
    for bad in ["password", "12345678", "qwerty123", "farmer123", "kisan123"]:
        err = validate_password(bad, "farmer1", "farmer1@example.com")
        assert err is not None
        assert "too common" in err


def test_password_matches_username_or_email():
    err1 = validate_password("Shetkari1", "shetkari1", "other@example.com")
    assert err1 is not None
    assert "same as your username or email" in err1

    err2 = validate_password("RameshKumar", "user1", "rameshkumar@example.com")
    assert err2 is not None
    assert "same as your username or email" in err2


def test_password_single_repeated_char():
    err = validate_password("11111111", "user1", "user@example.com")
    assert err is not None
    assert "too common" in err


def test_password_valid():
    err = validate_password("KisanSafe#2026", "ramesh", "ramesh@kisan.in")
    assert err is None


# ==============================================================================
# 2. Login Rate Limiting (In-Memory Failure Lockout)
# ==============================================================================

def test_login_limiter_allows_under_limit():
    test_email = "test_limiter_ok@example.com"
    clear_login_failures(test_email)

    assert login_retry_after(test_email) == 0

    record_login_failure(test_email)
    record_login_failure(test_email)
    assert login_retry_after(test_email) == 0

    clear_login_failures(test_email)


def test_login_limiter_blocks_on_fifth_failure():
    test_email = "test_limiter_block@example.com"
    clear_login_failures(test_email)

    for _ in range(5):
        record_login_failure(test_email)

    retry_after = login_retry_after(test_email)
    assert retry_after > 0
    assert retry_after <= 600

    # Clearing failures immediately restores access
    clear_login_failures(test_email)
    assert login_retry_after(test_email) == 0


# ==============================================================================
# 3. Weather Agricultural Advice Engine
# ==============================================================================

def test_weather_advice_heavy_rain():
    current = {"temperature": 26.0, "humidity": 70.0, "wind_kmh": 10.0, "rain_mm": 5.0}
    days = [
        {"date": "2026-10-15", "temp_max": 28, "temp_min": 20, "rain_mm": 35.0, "rain_probability": 90, "wind_max": 15},
        {"date": "2026-10-16", "temp_max": 27, "temp_min": 19, "rain_mm": 10.0, "rain_probability": 60, "wind_max": 12},
    ]

    advice = build_weather_advice(current, days)
    codes = [item["code"] for item in advice]

    assert "heavy_rain" in codes


def test_weather_advice_heat_and_humid_disease():
    current = {"temperature": 30.0, "humidity": 88.0, "wind_kmh": 8.0, "rain_mm": 0.0}
    days = [
        {"date": "2026-10-15", "temp_max": 40.0, "temp_min": 24, "rain_mm": 0.0, "rain_probability": 10, "wind_max": 10},
    ]

    advice = build_weather_advice(current, days)
    codes = [item["code"] for item in advice]

    assert "heat" in codes
    assert "humid_disease" in codes


def test_weather_advice_strong_wind():
    current = {"temperature": 25.0, "humidity": 50.0, "wind_kmh": 32.0, "rain_mm": 0.0}
    days = [
        {"date": "2026-10-15", "temp_max": 28, "temp_min": 18, "rain_mm": 0.0, "rain_probability": 5, "wind_max": 35.0},
    ]

    advice = build_weather_advice(current, days)
    codes = [item["code"] for item in advice]

    assert "strong_wind" in codes


# ==============================================================================
# 4. Smart Fertilizer & Dosage Calculator (ICAR Formulas & Land Units)
# ==============================================================================

def test_fertilizer_wheat_2_acres():
    result = compute_fertilizer_recommendation("wheat", 2.0, "acre")

    assert result["crop_key"] == "wheat"
    assert result["acres"] == 2.0
    # Wheat RDF per acre is N: 48, P: 24, K: 16 -> For 2 acres: N: 96, P: 48, K: 32
    assert result["pure_nutrients_kg"]["nitrogen_n"] == 96.0
    assert result["pure_nutrients_kg"]["phosphorus_p"] == 48.0
    assert result["pure_nutrients_kg"]["potassium_k"] == 32.0

    # Bags of commercial fertilizer
    assert result["commercial_bags"]["dap"]["bags"] > 0
    assert result["commercial_bags"]["urea"]["bags"] > 0
    assert result["commercial_bags"]["mop"]["bags"] > 0
    assert result["organic_fym_tonnes"] == 8.0  # 4 tonnes/acre * 2 acres


def test_fertilizer_guntha_conversion():
    # 40 Gunthas = exactly 1 Acre
    res_gunthas = compute_fertilizer_recommendation("rice", 40.0, "guntha")
    res_acre = compute_fertilizer_recommendation("rice", 1.0, "acre")

    assert res_gunthas["acres"] == pytest.approx(1.0, 0.01)
    assert res_gunthas["commercial_bags"]["urea"]["bags"] == res_acre["commercial_bags"]["urea"]["bags"]
    assert res_gunthas["commercial_bags"]["dap"]["bags"] == res_acre["commercial_bags"]["dap"]["bags"]


def test_fertilizer_invalid_inputs():
    with pytest.raises(ValueError, match="Unsupported crop"):
        compute_fertilizer_recommendation("unknown_crop_xyz", 1.0, "acre")

    with pytest.raises(ValueError, match="Area must be"):
        compute_fertilizer_recommendation("wheat", -5.0, "acre")

    with pytest.raises(ValueError, match="Invalid unit"):
        compute_fertilizer_recommendation("wheat", 2.0, "bigha_unsupported")


# ==============================================================================
# 5. Transient Error Detection for Retries
# ==============================================================================

def test_is_transient_error_detection():
    # Transient HTTP status codes
    class FakeHttpError(Exception):
        def __init__(self, code):
            self.code = code

    assert is_transient_error(FakeHttpError(429)) is True
    assert is_transient_error(FakeHttpError(503)) is True
    assert is_transient_error(FakeHttpError(500)) is True

    # Non-transient client errors
    assert is_transient_error(FakeHttpError(400)) is False
    assert is_transient_error(FakeHttpError(401)) is False
    assert is_transient_error(FakeHttpError(404)) is False

    # String error messages
    assert is_transient_error(Exception("ResourceExhausted: quota exceeded")) is True
    assert is_transient_error(Exception("Connection timeout")) is True
    assert is_transient_error(Exception("Invalid argument: unsupported mime type")) is False


# ==============================================================================
# 6. Registration Rate Limiter per Client IP
# ==============================================================================

def test_registration_limiter():
    from fastapi import HTTPException
    from main import check_registration_rate_limit, _registration_history, _registration_lock

    test_ip = "192.168.1.100"
    with _registration_lock:
        _registration_history.pop(test_ip, None)

    for _ in range(5):
        check_registration_rate_limit(test_ip)

    with pytest.raises(HTTPException) as exc_info:
        check_registration_rate_limit(test_ip)
    assert exc_info.value.status_code == 429
    assert "Too many accounts" in exc_info.value.detail

    with _registration_lock:
        _registration_history.pop(test_ip, None)

