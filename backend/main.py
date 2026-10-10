
import os
import asyncio

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from google import genai


# Load environment variables
load_dotenv()

AI_API_KEY = os.getenv("AI_API_KEY")

if not AI_API_KEY:
    raise RuntimeError("AI_API_KEY is not configured")


# Gemini client
client = genai.Client(api_key=AI_API_KEY)


# Supported languages and localized messages
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


# FastAPI application
app = FastAPI(
    title="KrishiAI API",
    description="AI-powered agricultural assistance platform",
    version="1.1.0",
)


# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.post("/analyze")
async def analyze_crop(
    crop: str = Form(""),
    problem: str = Form(""),
    language: str = Form("en"),
    image: UploadFile | None = File(None),
):
    # Normalize and validate the selected language
    language_code = language.strip().lower()

    if language_code not in LANGUAGES:
        language_code = "en"

    selected = LANGUAGES[language_code]
    selected_language = selected["name"]

    crop = crop.strip()
    problem = problem.strip()

    crop_details = crop if crop else "Not specified"

    problem_details = (
        problem
        if problem
        else "No written description provided. Analyze the uploaded image if available."
    )

    # Read uploaded image
    image_bytes = None

    if image:
        image_bytes = await image.read()

    # Explicit language instructions for Gemini
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
- Crop names and technical scientific terms may include their
  familiar scientific names where helpful.
- Keep the answer understandable to an Indian farmer.

Crop:
{crop_details}

Farmer's description:
{problem_details}

Analyze the uploaded image even when the farmer has not
provided a written description. If the crop is not specified,
identify it only when reasonably possible and explain uncertainty.
If no image is attached, use the crop name and description
provided by the farmer.

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
- Do not invent symptoms that are not supported by the input.
- Do not recommend pesticide products or dosages unless reliable,
  crop-specific guidance supports them.
- Encourage checking the product label and consulting a qualified
  local agricultural expert or Krishi Vigyan Kendra (KVK) before
  applying agricultural chemicals.
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

    image_info = (
        {
            "filename": image.filename,
            "content_type": image.content_type,
        }
        if image
        else None
    )

    # Call Gemini with retry
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

                print("Gemini response received successfully.")
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

    return {
        "crop": crop,
        "language": selected_language,
        "language_code": language_code,
        "image": image_info,
        "analysis": response.text,
        "disclaimer": selected["disclaimer"],
    }
