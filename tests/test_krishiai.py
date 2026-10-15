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
    resolve_place_query,
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
    with pytest.raises(ValueError, match="Please provide a crop name"):
        compute_fertilizer_recommendation("", 1.0, "acre")

    with pytest.raises(ValueError, match="Please provide a crop name"):
        compute_fertilizer_recommendation("   ", 1.0, "acre")

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


# ==============================================================================
# 7. Indian Agricultural Hubs / Marathi & Hindi Transliteration
# ==============================================================================

def test_resolve_place_query_marathi():
    assert resolve_place_query("कोल्हापूर") == "Kolhapur"
    assert resolve_place_query("पुणे") == "Pune"
    assert resolve_place_query("नाशिक") == "Nashik"
    assert resolve_place_query("बारामती") == "Baramati"
    assert resolve_place_query("सांगली") == "Sangli"


def test_resolve_place_query_hindi():
    assert resolve_place_query("भोपाल") == "Bhopal"
    assert resolve_place_query("इंदौर") == "Indore"
    assert resolve_place_query("लखनऊ") == "Lucknow"


def test_resolve_place_query_english_passthrough():
    assert resolve_place_query("Kolhapur") == "Kolhapur"
    assert resolve_place_query("Baramati") == "Baramati"


# ==============================================================================
# 8. DAP Nitrogen Deduction & Fertilizer RDF Disclaimer
# ==============================================================================

def test_fertilizer_dap_nitrogen_deduction():
    # 1 bag DAP (50kg) contains 18% N = 9.0 kg Nitrogen.
    # When DAP is applied for Phosphorus, remaining Nitrogen required from Urea
    # must be reduced so Urea is not over-recommended.
    res = compute_fertilizer_recommendation("sugarcane", 1.0, "acre")
    dap_bags = res["commercial_bags"]["dap"]["bags"]
    n_from_dap = dap_bags * 9.0
    total_n_required = res["pure_nutrients_kg"]["nitrogen_n"]

    expected_remaining_n = max(0.0, total_n_required - n_from_dap)
    expected_urea_bags = round(expected_remaining_n / 20.7, 1)

    assert res["commercial_bags"]["urea"]["bags"] == expected_urea_bags
    assert "disclaimer" in res
    assert "ICAR" in res["disclaimer"]


# ==============================================================================
# 9. Guest Analysis Rate Limiter
# ==============================================================================

def test_guest_analysis_limiter():
    from fastapi import HTTPException
    from main import check_guest_analysis_rate_limit, _guest_analysis_history, _guest_analysis_lock

    guest_ip = "192.168.1.200"
    with _guest_analysis_lock:
        _guest_analysis_history.pop(guest_ip, None)

    for _ in range(10):
        check_guest_analysis_rate_limit(guest_ip)

    with pytest.raises(HTTPException) as exc:
        check_guest_analysis_rate_limit(guest_ip)
    assert exc.value.status_code == 429
    assert "Daily guest analysis quota reached" in exc.value.detail

    with _guest_analysis_lock:
        _guest_analysis_history.pop(guest_ip, None)


# ==============================================================================
# 10. Groundnut & Extended Crops in Fertilizer RDF
# ==============================================================================

def test_groundnut_fertilizer_calculation():
    # Test Groundnut (भुईमूग) calculation for 1 acre
    res = compute_fertilizer_recommendation("groundnut", 1.0, "acre")
    assert res["crop_key"] == "groundnut"
    assert res["pure_nutrients_kg"]["nitrogen_n"] == 10.0
    assert res["pure_nutrients_kg"]["phosphorus_p"] == 20.0
    assert res["pure_nutrients_kg"]["potassium_k"] == 10.0
    assert res["commercial_bags"]["dap"]["bags"] > 0
    assert "Gypsum" in res["special_nutrient"]["en"]
    assert "जिप्सम" in res["special_nutrient"]["mr"]


# ==============================================================================
# 11. Multilingual Crop Aliases & Datalist Format Resolution
# ==============================================================================

def test_crop_alias_resolution():
    # Marathi names
    res_mr = compute_fertilizer_recommendation("भुईमूग", 2.0, "acre")
    assert res_mr["crop_key"] == "groundnut"

    res_cane = compute_fertilizer_recommendation("ऊस", 1.0, "acre")
    assert res_cane["crop_key"] == "sugarcane"

    res_harbhara = compute_fertilizer_recommendation("हरभरा", 1.0, "acre")
    assert res_harbhara["crop_key"] == "chickpea"

    # Datalist formatted selection
    res_datalist = compute_fertilizer_recommendation("Groundnut / भुईमूग (मूंगफली)", 1.0, "acre")
    assert res_datalist["crop_key"] == "groundnut"


# ==============================================================================
# 12. Offline Expert Agricultural Fallback Guidance
# ==============================================================================

def test_offline_crop_guidance_fallback():
    from main import get_offline_crop_guidance

    # Marathi guidance for groundnut
    mr_guidance = get_offline_crop_guidance("groundnut", "", "mr")
    assert "टिक्का" in mr_guidance or "तांबेरा" in mr_guidance
    assert "जिप्सम" in mr_guidance
    assert "१८००-१८०-१५५१" in mr_guidance  # KCC Helpline

    # English guidance for sugarcane
    en_guidance = get_offline_crop_guidance("sugarcane", "", "en")
    assert "Shoot Borer" in en_guidance or "Red Rot" in en_guidance
    assert "1800-180-1551" in en_guidance


# ==============================================================================
# 13. Universal Crop Support (Works for EVERY crop, not just specific crops)
# ==============================================================================

def test_fertilizer_supports_any_crop_universal():
    # 1. Arbitrary custom crop (Dragon Fruit)
    res_dragon = compute_fertilizer_recommendation("Dragon Fruit", 2.0, "acre")
    assert res_dragon["crop_key"] == "dragon fruit"
    assert res_dragon["acres"] == 2.0
    assert res_dragon["pure_nutrients_kg"]["nitrogen_n"] > 0
    assert res_dragon["commercial_bags"]["urea"]["bags"] >= 0
    assert res_dragon["commercial_bags"]["dap"]["bags"] > 0
    assert res_dragon["commercial_bags"]["mop"]["bags"] > 0
    assert res_dragon["organic_fym_tonnes"] > 0
    assert len(res_dragon["stages"]) > 0

    # 2. Custom crop in Marathi (हळद - Turmeric)
    res_haldi = compute_fertilizer_recommendation("हळद", 1.5, "acre")
    assert res_haldi["crop_key"] == "turmeric"
    assert res_haldi["pure_nutrients_kg"]["nitrogen_n"] > 0

    # 3. Floriculture crop (झेंडू - Marigold)
    res_zendu = compute_fertilizer_recommendation("झेंडू", 1.0, "acre")
    assert res_zendu["crop_key"] == "marigold"

    # 4. Completely unknown rare crop (e.g. Vanilla Orchid or Quinoa)
    res_rare = compute_fertilizer_recommendation("Vanilla Orchid", 1.0, "acre")
    assert res_rare["crop_key"] == "vanilla orchid"
    assert res_rare["commercial_bags"]["dap"]["bags"] > 0
    assert res_rare["commercial_bags"]["urea"]["bags"] >= 0


def test_crop_category_classification():
    from main import classify_crop_category

    # Fruits
    assert classify_crop_category("Dragon Fruit") == "fruit"
    assert classify_crop_category("Custard Apple") == "fruit"
    assert classify_crop_category("सीताफळ") == "fruit"
    assert classify_crop_category("कलिंगड") == "fruit"

    # Vegetables
    assert classify_crop_category("वांगी") == "vegetable"
    assert classify_crop_category("Cauliflower") == "vegetable"
    assert classify_crop_category("French Beans") == "vegetable"

    # Flowers
    assert classify_crop_category("झेंडू") == "flower"
    assert classify_crop_category("Marigold") == "flower"
    assert classify_crop_category("Rose") == "flower"

    # Spices
    assert classify_crop_category("हळद") == "spice"
    assert classify_crop_category("Ginger") == "spice"
    assert classify_crop_category("Cardamom") == "spice"

    # Oilseeds
    assert classify_crop_category("Sunflower") == "oilseed"
    assert classify_crop_category("तीळ") == "oilseed"

    # Millets / Cereals
    assert classify_crop_category("बाजरी") == "cereal"
    assert classify_crop_category("Ragi") == "cereal"


def test_soil_type_and_modern_fertilizer_alternatives():
    # 1. Medium black baseline
    res_base = compute_fertilizer_recommendation("cotton", 1.0, "acre", soil_type="medium_black")
    # 2. Black cotton soil reduces K requirement (-15%)
    res_bc = compute_fertilizer_recommendation("cotton", 1.0, "acre", soil_type="black_cotton")
    assert res_bc["pure_nutrients_kg"]["potassium_k"] < res_base["pure_nutrients_kg"]["potassium_k"]
    assert "black_cotton" in res_bc["soil_type"]
    assert "potash" in res_bc["soil_advisory"]["en"].lower()

    # 3. Red laterite soil increases P requirement (+20%)
    res_red = compute_fertilizer_recommendation("cotton", 1.0, "acre", soil_type="red_laterite")
    assert res_red["pure_nutrients_kg"]["phosphorus_p"] > res_base["pure_nutrients_kg"]["phosphorus_p"]
    assert "red_laterite" in res_red["soil_type"]

    # 4. Check modern alternatives (Nano Urea & SSP)
    assert "liquid_and_modern_alternatives" in res_base
    assert "nano_urea" in res_base["liquid_and_modern_alternatives"]
    assert res_base["liquid_and_modern_alternatives"]["nano_urea"]["bottles_500ml"] >= 1
    assert "ssp_dap_substitute" in res_base["liquid_and_modern_alternatives"]
    assert res_base["liquid_and_modern_alternatives"]["ssp_dap_substitute"]["ssp_bags_50kg"] > 0


def test_apmc_mandi_rates():
    from main import get_mandi_rates, APMC_MANDI_RATES

    assert len(APMC_MANDI_RATES) >= 8
    # Test all rates
    all_rates = get_mandi_rates()
    assert len(all_rates["rates"]) == len(APMC_MANDI_RATES)

    # Test filtering by commodity (Groundnut)
    gn_rates = get_mandi_rates(commodity="Groundnut")
    assert len(gn_rates["rates"]) >= 1
    assert "Groundnut" in gn_rates["rates"][0]["commodity"]
    assert gn_rates["rates"][0]["modal_price"] > 0

    # Test filtering by market (Solapur)
    solapur_rates = get_mandi_rates(market="Solapur")
    assert len(solapur_rates["rates"]) >= 1
    assert "Solapur" in solapur_rates["rates"][0]["market"]


def test_user_analysis_burst_rate_limit():
    from main import check_user_analysis_rate_limit, _user_analysis_history
    from fastapi import HTTPException

    test_uid = 99999
    _user_analysis_history.pop(test_uid, None)

    # First 6 calls within 60s should succeed
    for _ in range(6):
        check_user_analysis_rate_limit(test_uid)

    # 7th call should raise 429
    with pytest.raises(HTTPException) as exc_info:
        check_user_analysis_rate_limit(test_uid)
    assert exc_info.value.status_code == 429
    assert "Too many analysis requests" in exc_info.value.detail


def test_health_check_endpoint():
    from main import health

    res = health()
    assert "status" in res
    assert res["status"] in ("healthy", "degraded")
    assert "database" in res
    assert "service" in res

