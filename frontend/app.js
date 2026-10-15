const API_URL = "/api";

let selectedImage = null;
let previewUrl = null;

// ---- Photo compression --------------------------------------------------
// Phone cameras produce 3-8 MB photos. Shrinking them in the browser makes
// uploads far faster and more reliable on weak mobile connections.
const MAX_IMAGE_SIDE = 1600;
const IMAGE_QUALITY = 0.82;
const SKIP_COMPRESSION_BELOW = 400 * 1024; // bytes

let imageJob = 0;                    // lets a newer photo cancel an older job
let imageReady = Promise.resolve();  // resolves when selectedImage is final

function loadViaImageElement(file) {
    return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();

        img.onload = () => {
            URL.revokeObjectURL(url);
            resolve({
                source: img,
                width: img.naturalWidth,
                height: img.naturalHeight
            });
        };

        img.onerror = () => {
            URL.revokeObjectURL(url);
            reject(new Error("Could not read image"));
        };

        img.src = url;
    });
}

async function loadImage(file) {
    if (window.createImageBitmap) {
        try {
            // "from-image" applies the camera's rotation information.
            const bitmap = await createImageBitmap(file, {
                imageOrientation: "from-image"
            });

            return { source: bitmap, width: bitmap.width, height: bitmap.height };
        } catch (error) {
            // Fall through to the <img> based loader.
        }
    }

    return loadViaImageElement(file);
}

// Returns a smaller JPEG, or the original file if that is not possible.
async function compressImage(file) {
    if (file.size < SKIP_COMPRESSION_BELOW) {
        return file;
    }

    try {
        const image = await loadImage(file);
        const longestSide = Math.max(image.width, image.height);
        const scale = Math.min(1, MAX_IMAGE_SIDE / longestSide);

        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(image.width * scale));
        canvas.height = Math.max(1, Math.round(image.height * scale));

        const context = canvas.getContext("2d");
        context.fillStyle = "#ffffff"; // PNG transparency becomes white
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(image.source, 0, 0, canvas.width, canvas.height);

        if (image.source.close) {
            image.source.close();
        }

        const blob = await new Promise(resolve =>
            canvas.toBlob(resolve, "image/jpeg", IMAGE_QUALITY)
        );

        if (!blob || blob.size >= file.size) {
            return file;
        }

        const baseName = (file.name || "crop-photo").replace(/\.[^.]+$/, "");

        return new File([blob], `${baseName}.jpg`, {
            type: "image/jpeg",
            lastModified: Date.now()
        });
    } catch (error) {
        return file; // never block the farmer because compression failed
    }
}

function prepareImage(file) {
    const job = ++imageJob;

    selectedImage = file; // use the original until compression finishes

    imageReady = compressImage(file).then(compressed => {
        if (job === imageJob) {
            selectedImage = compressed;
        }
    });
}
// ---- End photo compression ----------------------------------------------

// Keep the login token persistent across tab reloads & background tab suspends.
let authToken = localStorage.getItem("krishiai_access_token") || sessionStorage.getItem("krishiai_access_token");
let currentUser = null;

try {
    currentUser = JSON.parse(
        localStorage.getItem("krishiai_user") || sessionStorage.getItem("krishiai_user") || "null"
    );
} catch {
    currentUser = null;
}

// Turn FastAPI error responses (string or validation array) into text.
function getErrorMessage(data, fallback) {
    if (typeof data?.detail === "string") {
        return data.detail;
    }

    if (Array.isArray(data?.detail) && data.detail[0]?.msg) {
        return data.detail[0].msg;
    }

    return fallback;
}

// Remove the token AND every piece of the previous user's data from the page.
function clearSession(message) {
    authToken = null;
    currentUser = null;

    localStorage.removeItem("krishiai_access_token");
    localStorage.removeItem("krishiai_user");
    sessionStorage.removeItem("krishiai_access_token");
    sessionStorage.removeItem("krishiai_user");

    window.krishiAIHistoryRecords = [];
    document.getElementById("historyList").innerHTML = "";

    clearWeather();
    document.getElementById("historyStatus").textContent = t("historyLogin");

    const result = document.getElementById("result");
    result.innerHTML = "";
    result.classList.add("hidden");

    updateAuthUI();

    if (message) {
        setAuthStatus(message);
    }
}

function handleSessionExpired() {
    clearSession(t("sessionExpired"));

    document.getElementById("authSection").scrollIntoView({
        behavior: "smooth",
        block: "start"
    });
}

async function restoreLoginSession() {
    updateAuthUI();
    if (authToken) {
        if (!currentUser) {
            try {
                const response = await fetch(`${API_URL}/auth/me`, {
                    headers: { Authorization: `Bearer ${authToken}` }
                });
                if (response.ok) {
                    currentUser = await response.json();
                    localStorage.setItem("krishiai_user", JSON.stringify(currentUser));
                    updateAuthUI();
                } else if (response.status === 401) {
                    clearSession(t("sessionExpired"));
                    return;
                }
            } catch (error) {
                // Ignore background network error on page load
            }
        }
        await loadAnalysisHistory();
    }
}

let authStatusTimeout = null;

function setAuthStatus(message) {
    const el = document.getElementById("authStatus");
    if (!el) return;
    el.textContent = message;

    if (authStatusTimeout) {
        clearTimeout(authStatusTimeout);
        authStatusTimeout = null;
    }

    if (message && (message === t("loginSuccess") || message === t("accountCreated"))) {
        authStatusTimeout = setTimeout(() => {
            if (el.textContent === message) {
                el.textContent = "";
            }
        }, 4000);
    }
}

function updateAuthUI() {
    const authForms = document.getElementById("authForms");
    const accountPanel = document.getElementById("accountPanel");
    const authHeading = document.getElementById("authHeading");
    const authSubheading = document.getElementById("authSubheading");
    const accountName = document.getElementById("accountName");
    const accountEmail = document.getElementById("accountEmail");

    const isLoggedIn = Boolean(authToken);
    if (authForms) authForms.hidden = isLoggedIn;
    if (accountPanel) accountPanel.hidden = !isLoggedIn;

    const guestBanner = document.getElementById("guestModeBanner");
    if (guestBanner) {
        guestBanner.style.display = isLoggedIn ? "none" : "flex";
    }

    if (isLoggedIn) {
        let username = currentUser?.username;
        let email = currentUser?.email;

        // Fallback: If currentUser isn't in memory, attempt reading claims from token
        if (!username && authToken) {
            try {
                const parts = authToken.split(".");
                if (parts.length === 3) {
                    const payload = JSON.parse(atob(parts[1]));
                    if (payload.username) username = payload.username;
                    if (payload.email) email = payload.email;
                    if (!currentUser && (username || email)) {
                        currentUser = {
                            id: payload.sub,
                            username: username || "",
                            email: email || ""
                        };
                        localStorage.setItem("krishiai_user", JSON.stringify(currentUser));
                    }
                }
            } catch (e) {
                // Ignore decoding error
            }
        }

        const displayName = username || t("farmerUser");
        const displayEmail = email || "";

        if (authHeading) {
            authHeading.textContent = t("authTitleLoggedIn").replace("{username}", displayName);
        }
        if (authSubheading) {
            authSubheading.textContent = t("authDescLoggedIn");
        }
        if (accountName) {
            accountName.textContent = displayName;
        }
        if (accountEmail) {
            accountEmail.textContent = displayEmail;
        }
    } else {
        if (authHeading) {
            authHeading.textContent = t("authTitle");
        }
        if (authSubheading) {
            authSubheading.textContent = t("authDescription");
        }
        if (accountName) {
            accountName.textContent = "";
        }
        if (accountEmail) {
            accountEmail.textContent = "";
        }
    }
}

function getAuthErrorMessage(endpoint, status) {
    if (endpoint === "login" && status === 401) {
        return t("errInvalidLogin");
    }

    if (status === 429) {
        return t("errTooManyAttempts");
    }

    if (endpoint === "register" && status === 409) {
        return t("errExists");
    }

    if (status === 400 || status === 422) {
        return t("errCheckDetails");
    }

    return t("errAuthFailed");
}

async function authenticate(endpoint, form) {
    const formData = new FormData(form);
    const body = Object.fromEntries(formData.entries());

    setAuthStatus(t("authWait"));

    let response;

    try {
        response = await fetch(`${API_URL}/auth/${endpoint}`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(body)
        });
    } catch (error) {
        setAuthStatus(t("errConnection"));
        return;
    }

    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
        setAuthStatus(getAuthErrorMessage(endpoint, response.status));
        return;
    }

    if (endpoint === "register") {
        setAuthStatus(t("accountCreated"));
        const regEmail = body.email;
        form.reset();
        if (regEmail) {
            const loginEmailInput = document.getElementById("loginEmail");
            const loginPassInput = document.getElementById("loginPassword");
            if (loginEmailInput) loginEmailInput.value = regEmail;
            if (loginPassInput) loginPassInput.focus();
        }
        return;
    }

    authToken = data.access_token;
    currentUser = data.user;

    localStorage.setItem("krishiai_access_token", authToken);
    localStorage.setItem("krishiai_user", JSON.stringify(currentUser));

    updateAuthUI();
    setAuthStatus(t("loginSuccess"));

    restoreWeather();

    form.reset();

    await loadAnalysisHistory();
}

document.getElementById("loginForm").addEventListener("submit", async event => {
    event.preventDefault();
    await authenticate("login", event.currentTarget);
});

document.getElementById("registerForm").addEventListener("submit", async event => {
    event.preventDefault();
    await authenticate("register", event.currentTarget);
});

document.getElementById("logoutButton").addEventListener("click", () => {
    clearSession(t("loggedOut"));
});

function showImagePreview(file) {
    imageJob++; // cancel any compression that is still running

    const preview = document.getElementById("imagePreview");
    const removeButton = document.getElementById("removeImage");

    // Release the previous preview URL.
    if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
        previewUrl = null;
    }

    if (!file) {
        selectedImage = null;
        preview.removeAttribute("src");
        preview.classList.add("hidden");
        removeButton.classList.add("hidden");
        return;
    }

    // Accept image files only.
    if (!file.type || !file.type.startsWith("image/")) {
        selectedImage = null;

        document.getElementById("image").value = "";
        document.getElementById("cameraInput").value = "";

        preview.removeAttribute("src");
        preview.classList.add("hidden");
        removeButton.classList.add("hidden");

        alert("Please select a valid image.");
        return;
    }

    prepareImage(file);
    previewUrl = URL.createObjectURL(file);

    preview.src = previewUrl;
    preview.classList.remove("hidden");
    removeButton.classList.remove("hidden");
}

// Handle choosing a photo from the gallery.
document.getElementById("image").addEventListener("change", event => {
    const file = event.target.files[0];

    if (file) {
        document.getElementById("cameraInput").value = "";
        showImagePreview(file);
    }
});

// Handle taking a new photo with the camera.
document.getElementById("cameraInput").addEventListener("change", event => {
    const file = event.target.files[0];

    if (file) {
        document.getElementById("image").value = "";
        showImagePreview(file);
    }
});

// Remove the currently selected photo.
document.getElementById("removeImage").addEventListener("click", () => {
    document.getElementById("image").value = "";
    document.getElementById("cameraInput").value = "";

    showImagePreview(null);
});


const translations = {
    en: {
        languageLabel: "Language",
        tagline: "🌾 AI-Powered Agriculture",
        heroTitle: "Your AI Farming",
        heroTitleAccent: "Assistant",
        heroDescription: "Get simple agricultural guidance, crop problem analysis, disease identification and weather-related insights.",
        heroButton: "Analyze Crop Problem →",
        featureImageTitle: "Upload Crop Image",
        featureImageDescription: "Upload a crop photo for analysis.",
        featureAITitle: "AI Guidance",
        featureAIDescription: "Get simple agricultural recommendations.",
        featureWeatherTitle: "Weather Insights",
        featureWeatherDescription: "Understand weather-related crop risks.",
        featureLanguageTitle: "Multiple Languages",
        featureLanguageDescription: "Marathi, Hindi and English support.",
        analysisTagline: "🔍 Crop Analysis",
        analysisTitle: "Describe Your Crop Problem",
        analysisDescription: "Tell KrishiAI what is happening to your crop.",
        cropLabel: "Crop Name",
        cropPlaceholder: "Example: Groundnut, Sugarcane, Cotton...",
        cropFieldHint: "💡 Type or choose any crop in Marathi, Hindi, or English (e.g. Groundnut, भुईमूग, कापूस).",
        problemLabel: "Describe the Problem",
        problemPlaceholder: "Example: Tomato leaves are turning yellow...",
        imageLabel: "Upload Crop Image",
        analyzeButton: "🤖 Analyze with KrishiAI",
        footer: "🌱 KrishiAI — AI-powered agricultural assistance for Indian farmers",
        missingTitle: "⚠️ Missing Information",
        missingMessage: "Please enter a crop name, describe the problem, or upload an image.",
        loadingTitle: "🤖 KrishiAI is analyzing...",
        loadingMessage: "Please wait while KrishiAI processes your crop information.",
        resultTitle: "🌱 KrishiAI Analysis",
        cropResult: "Crop",
        imageResult: "Image",
        guidanceTitle: "🤖 AI Agricultural Guidance",
        warningTitle: "🌱 KrishiAI",
        disclaimer: "This AI-generated guidance is for informational purposes and should not replace advice from a qualified agricultural expert.",
        errorTitle: "❌ Analysis Failed",
        connectionError: "KrishiAI advisory is temporarily busy. Please try again shortly or contact the farmer helpline below.",
        serverError: "",
        busyTitle: "Agricultural Advisory Assistant Busy",
        busyMessage: "KrishiAI is currently experiencing high demand. Please try again in 1 minute, or contact the Kisan Helpline directly for immediate support.",
        busyHelplineNotice: "For immediate free advice from government agriculture experts:",
        busyCallKcc: "Call Kisan Call Center (1800-180-1551)",
        noAnalysis: "No analysis was returned by the AI service.",

        authTagline: "Your Account",
        authTitle: "Login or Create Account",
        authDescription: "Sign in to save and view your personal crop analyses.",
        authTitleLoggedIn: "Welcome, {username}! 👨‍🌾",
        authDescLoggedIn: "Your account is active. Your crop analyses and records are saved automatically.",
        activeSession: "🟢 Active Account",
        loginTitle: "Login",
        emailLabel: "Email",
        passwordLabel: "Password",
        usernameLabel: "Username",
        loginButton: "Login",
        registerTitle: "Create Account",
        registerButton: "Create Account",
        passwordHint: "At least 8 characters.",
        logoutButton: "Logout",
        authWait: "Please wait...",
        loginSuccess: "Login successful.",
        accountCreated: "Account created. Please log in.",
        loggedOut: "You have logged out.",
        sessionExpired: "Your session expired. Please log in again.",
        loginRequired: "Please log in to analyze your crop.",
        loggedInAs: "Logged in as",
        yourAccount: "your account",
        farmerUser: "Farmer",
        errInvalidLogin: "Invalid email or password.",
        errExists: "That username or email is already registered.",
        errCheckDetails: "Please check your details. Username: at least 3 characters. Password: at least 8 characters, and not a common password.",
        errAuthFailed: "Authentication failed.",
        errConnection: "Unable to connect to the server.",
        errTooManyAttempts: "Too many failed attempts. Please wait a few minutes and try again.",
        errImageTooLarge: "The photo is too large. Please choose a smaller photo (up to 5 MB).",
        weatherTagline: "🌦️ Weather Insights",
        weatherTitle: "Weather for Your Farm",
        weatherDescription: "Search for your village or town to see the forecast and simple farming tips.",
        weatherSearchLabel: "Village or town",
        weatherSearchPlaceholder: "Example: Kolhapur",
        weatherSearchButton: "Search",
        weatherLoginRequired: "Please log in to see weather insights.",
        weatherShortQuery: "Please type at least 2 letters.",
        weatherSearching: "Searching...",
        weatherNoPlaces: "No place found. Try your nearest Taluka or District name (e.g. Kolhapur, Baramati, Nashik).",
        weatherChoose: "Choose your place:",
        weatherLoading: "Loading weather...",
        weatherError: "Unable to load weather. Please try again.",
        weatherNow: "Right now",
        weatherHumidity: "Humidity",
        weatherWind: "Wind",
        weatherRain: "Rain",
        weatherForecastTitle: "Next 5 days",
        weatherAdviceTitle: "Farming tips",
        weatherNoAdvice: "No special weather warnings. Normal farming work can continue.",
        weatherToday: "Today",
        weatherTomorrow: "Tomorrow",
        weatherNote: "These are general tips. For exact advice, ask your local agriculture office (Krishi Vigyan Kendra). Always follow the label when spraying.",
        weatherCredit: "Weather data: Open-Meteo.com",
        wx_clear: "Clear sky",
        wx_cloudy: "Cloudy",
        wx_fog: "Fog",
        wx_drizzle: "Light drizzle",
        wx_rain: "Rain",
        wx_showers: "Rain showers",
        wx_thunder: "Thunderstorm",
        wx_snow: "Snow",
        advice_heavy_rain: "Heavy rain expected. Delay spraying and fertilizer, and make sure water can drain from your field.",
        advice_rain_expected: "Rain is likely soon. Avoid spraying or fertilizing just before the rain.",
        advice_heat: "Very hot days ahead. Water early in the morning or in the evening, and mulch to protect the soil.",
        advice_cold: "Cold nights ahead. Protect young plants and avoid watering late in the evening.",
        advice_strong_wind: "Strong wind expected. Do not spray on windy days, and support tall crops.",
        advice_humid_disease: "Humid weather increases the risk of fungal disease. Check leaves regularly for spots or mold.",
        advice_dry_spell: "Little or no rain for the next 5 days. Plan your irrigation.",
        advice_good_spray_window: "Good day for spraying: little wind and no rain.",
        recordsTagline: "📚 Your Records",
        historyTitle: "Recent Crop Analyses",
        historyDescription: "View your previously saved crop recommendations.",
        refreshHistory: "Refresh History",
        filterLabel: "Filter by language:",
        allLanguages: "All languages",
        historyLogin: "Log in to view your saved analyses.",
        historyLoading: "Loading saved analyses...",
        historyNone: "No saved analyses yet.",
        historyNoneLanguage: "No analyses found for this language.",
        historyShowing: "Showing {shown} of {total} saved analysis record(s).",
        historyError: "Unable to load history. Please try refreshing.",
        historyProblem: "Problem:",
        historyLanguage: "Language:",
        historyDate: "Date:",
        historyView: "View recommendations",
        notSpecified: "Not specified",
        dateUnavailable: "Date unavailable",
        voiceStart: "🎙️ Speak Problem",
        voiceStop: "⏹️ Stop Listening",
        voiceListening: "🔴 Listening... Please speak now",
        voiceNotSupported: "Voice recognition is not supported in this browser. Please use Chrome or Edge.",
        voicePermissionDenied: "Microphone permission was denied. Please allow microphone access in your browser settings.",
        voiceNoSpeech: "No speech detected. Please tap and try speaking again.",
        voiceError: "Voice input error. Please try again.",
        ttsPlay: "🔊 Listen to Advice",
        ttsStop: "⏹️ Stop Audio",
        ttsNotSupported: "Audio playback is not supported in this browser.",
        shareWhatsApp: "WhatsApp Share",
        helplineTagline: "📞 24/7 Expert Support",
        helplineTitle: "Emergency Farmer Helplines",
        helplineDescription: "Speak directly with agricultural experts and government advisors for immediate emergency assistance.",
        kccTitle: "Kisan Call Center (KCC)",
        kccDesc: "Government of India toll-free helpline. Free consultation in 22 languages from 6 AM to 10 PM daily.",
        pmfbyTitle: "Crop Insurance Helpline (PMFBY)",
        pmfbyDesc: "Assistance for crop damage claims, drought/flood insurance claims, and policy status.",
        kvkTitle: "Krishi Vigyan Kendra (KVK)",
        kvkDesc: "District agricultural research centers for seed testing, pest outbreaks, and soil health consultation.",
        callNow: "📞 Call Now",
        findKvk: "🌐 Find Your KVK",
        fertTagline: "🧪 Smart Nutrition",
        fertTitle: "Fertilizer & Dosage Calculator",
        fertCropLabel: "Crop Name (Any Crop / कोणतेही पीक)",
        fertCropPlaceholder: "Select or type ANY crop (e.g. Turmeric, Dragon Fruit, Tomato)...",
        fertCropHint: "💡 Works for ALL crops: vegetables, fruits, flowers, cash crops, spices, grains & pulses.",
        fertAreaLabel: "Farm Size / Area",
        fertUnitLabel: "Land Unit",
        unitAcre: "Acres (एकर)",
        unitGuntha: "Gunthas (गुंठा - 1/40 Acre)",
        unitHectare: "Hectares (हेक्टर)",
        fertCalculateBtn: "🧪 Calculate Fertilizer Dosage",
        fertCalculating: "Calculating...",
        listenDosage: "Listen to Dosage",
        shareDosage: "Share on WhatsApp",
        fertBagsTitle: "🛍️ Recommended Bags to Purchase (RDF Estimate):",
        bagsLabel: "Bags",
        pureNutrients: "Pure Nutrients (N-P-K):",
        microNutrient: "Micro / Secondary Nutrient:",
        fertScheduleTitle: "📅 When & How to Apply (Split Schedule):",
        organicBoostersTitle: "Organic & Soil Health Boosters",
        fertDisclaimer: "⚠️ Recommendation based on ICAR / State Agricultural University standards. For precision farming, get your soil tested every 2 years at your nearest Krishi Vigyan Kendra (KVK).",
        fertError: "Please enter a valid area.",
        fertSuccess: "Dosage calculated successfully.",
        fymTonnesText: "Recommended FYM / Compost: {tonnes} tonnes",
        guestModeBanner: "🌱 Guest Mode: Free instant analysis, no account required. Sign in below anytime to save your crop history.",
        guestResultNotice: "🌱 You are in Guest Mode — this diagnosis is not saved to history.",
        aiCautionTitle: "⚠️ Important Advisory Caution",
        aiCautionBody: "AI guidance is for informational support only. Visual diagnosis and pesticide suggestions can be inaccurate. Always verify symptoms with your nearest Krishi Vigyan Kendra (KVK) or local agriculture extension officer before spraying chemical pesticides.",
        privacyTitle: "Privacy & Data Control",
        privacyNotice: "Your crop descriptions and diagnosis records are securely stored in your private account. Voice inputs use your browser's speech recognition service (which sends audio to device/Google speech servers). You can clear your analysis history or delete your account at any time.",
        clearHistoryButton: "🗑️ Clear All History",
        deleteAccountButton: "⚠️ Delete Account",
        confirmClearHistory: "Are you sure you want to delete all your saved crop analyses?",
        confirmDeleteAccount: "Are you sure you want to permanently delete your KrishiAI account and all data? This cannot be undone.",
        historyClearedSuccess: "All crop analysis history has been deleted.",
        accountDeletedSuccess: "Your account and all associated data have been permanently deleted."
    },

    mr: {
        languageLabel: "भाषा",
        tagline: "🌾 कृत्रिम बुद्धिमत्तेवर आधारित शेती",
        heroTitle: "तुमचा AI शेती",
        heroTitleAccent: "सहाय्यक",
        heroDescription: "शेतीविषयक सोपे मार्गदर्शन, पिकांच्या समस्यांचे विश्लेषण, रोगांची ओळख आणि हवामानाशी संबंधित माहिती मिळवा.",
        heroButton: "पिकाच्या समस्येचे विश्लेषण करा →",
        featureImageTitle: "पिकाचा फोटो अपलोड करा",
        featureImageDescription: "विश्लेषणासाठी पिकाचा फोटो अपलोड करा.",
        featureAITitle: "AI मार्गदर्शन",
        featureAIDescription: "शेतीसाठी सोप्या शिफारसी मिळवा.",
        featureWeatherTitle: "हवामानाची माहिती",
        featureWeatherDescription: "हवामानामुळे पिकांवर होणारे संभाव्य परिणाम समजून घ्या.",
        featureLanguageTitle: "अनेक भाषा",
        featureLanguageDescription: "मराठी, हिंदी आणि इंग्रजी भाषांचा पर्याय.",
        analysisTagline: "🔍 पीक विश्लेषण",
        analysisTitle: "तुमच्या पिकाची समस्या सांगा",
        analysisDescription: "तुमच्या पिकाला काय समस्या आहे ते KrishiAI ला सांगा.",
        cropLabel: "पिकाचे नाव",
        cropPlaceholder: "उदाहरण: भुईमूग, ऊस, कापूस, टोमॅटो...",
        cropFieldHint: "💡 तुम्ही कोणत्याही पिकाचे नाव मराठी, इंग्रजी किंवा हिंदीत लिहू शकता (उदा. भुईमूग, कापूस, ऊस, हरभरा, टोमॅटो).",
        problemLabel: "समस्येचे वर्णन करा",
        problemPlaceholder: "उदाहरण: टोमॅटोची पाने पिवळी पडत आहेत...",
        imageLabel: "पिकाचा फोटो अपलोड करा",
        analyzeButton: "🤖 KrishiAI द्वारे विश्लेषण करा",
        footer: "🌱 KrishiAI — भारतीय शेतकऱ्यांसाठी AI आधारित शेती सहाय्य",
        missingTitle: "⚠️ माहिती अपूर्ण आहे",
        missingMessage: "कृपया पिकाचे नाव लिहा, समस्येचे वर्णन करा किंवा फोटो अपलोड करा.",
        loadingTitle: "🤖 KrishiAI विश्लेषण करत आहे...",
        loadingMessage: "कृपया थांबा. तुमच्या पिकाची माहिती तपासली जात आहे.",
        resultTitle: "🌱 KrishiAI पीक विश्लेषण",
        cropResult: "पीक",
        imageResult: "फोटो",
        guidanceTitle: "🤖 शेतीविषयक AI मार्गदर्शन",
        warningTitle: "🌱 KrishiAI",
        disclaimer: "हे AI द्वारे तयार केलेले मार्गदर्शन केवळ माहितीसाठी आहे. पात्र कृषी तज्ज्ञांच्या सल्ल्याची जागा ते घेऊ शकत नाही.",
        errorTitle: "❌ विश्लेषण अयशस्वी",
        connectionError: "सध्या माहिती मिळवण्यात अडचण येत आहे. कृपया १ मिनिटाने पुन्हा प्रयत्न करा किंवा खालील हेल्पलाइनवर संपर्क साधा.",
        serverError: "",
        busyTitle: "कृषी सल्ला सहाय्यक तात्पुरता व्यस्त आहे",
        busyMessage: "KrishiAI सेवेवर सध्या जास्त ताण असल्यामुळे माहिती मिळण्यात अडचण येत आहे. कृपया १ मिनिटाने पुन्हा प्रयत्न करा किंवा थेट किसान कॉल सेंटरवर संपर्क साधा.",
        busyHelplineNotice: "तातडीच्या मोफत सल्ल्यासाठी शासकीय कृषी शास्त्रज्ञांशी थेट संपर्क साधा:",
        busyCallKcc: "किसान कॉल सेंटरला कॉल करा (टोल-फ्री १८००-१८०-१५५१)",
        noAnalysis: "AI सेवेकडून कोणतेही विश्लेषण मिळाले नाही.",

        authTagline: "तुमचे खाते",
        authTitle: "लॉगिन करा किंवा खाते तयार करा",
        authDescription: "तुमची पीक विश्लेषणे जतन करण्यासाठी आणि पाहण्यासाठी साइन इन करा.",
        authTitleLoggedIn: "स्वागत आहे, {username}! 👨‍🌾",
        authDescLoggedIn: "तुमचे खाते सक्रिय आहे. तुमची पीक विश्लेषणे सुरक्षितपणे जतन केली जात आहेत.",
        activeSession: "🟢 सक्रिय खाते",
        loginTitle: "लॉगिन",
        emailLabel: "ईमेल",
        passwordLabel: "पासवर्ड",
        usernameLabel: "वापरकर्ता नाव",
        loginButton: "लॉगिन करा",
        registerTitle: "खाते तयार करा",
        registerButton: "खाते तयार करा",
        passwordHint: "किमान 8 अक्षरे.",
        logoutButton: "लॉगआउट",
        authWait: "कृपया थांबा...",
        loginSuccess: "लॉगिन यशस्वी झाले.",
        accountCreated: "खाते तयार झाले. कृपया लॉगिन करा.",
        loggedOut: "तुम्ही लॉगआउट केले आहे.",
        sessionExpired: "तुमचे सत्र संपले आहे. कृपया पुन्हा लॉगिन करा.",
        loginRequired: "पिकाचे विश्लेषण करण्यासाठी कृपया लॉगिन करा.",
        loggedInAs: "लॉगिन केलेले खाते:",
        yourAccount: "तुमचे खाते",
        farmerUser: "शेतकरी",
        errInvalidLogin: "ईमेल किंवा पासवर्ड चुकीचा आहे.",
        errExists: "हे वापरकर्ता नाव किंवा ईमेल आधीच नोंदणीकृत आहे.",
        errCheckDetails: "कृपया तुमची माहिती तपासा. वापरकर्ता नाव किमान 3 अक्षरांचे आणि पासवर्ड किमान 8 अक्षरांचा असावा; सहज ओळखता येणारा पासवर्ड वापरू नका.",
        errAuthFailed: "ओळख पडताळणी अयशस्वी झाली.",
        errConnection: "सर्व्हरशी संपर्क साधता आला नाही.",
        errTooManyAttempts: "खूप वेळा चुकीचा प्रयत्न झाला. कृपया काही मिनिटे थांबून पुन्हा प्रयत्न करा.",
        errImageTooLarge: "फोटो खूप मोठा आहे. कृपया लहान फोटो निवडा (जास्तीत जास्त 5 MB).",
        weatherTagline: "🌦️ हवामान माहिती",
        weatherTitle: "तुमच्या शेतासाठी हवामान",
        weatherDescription: "तुमचे गाव किंवा शहर शोधा आणि हवामानाचा अंदाज व सोपे शेती सल्ले पहा.",
        weatherSearchLabel: "गाव किंवा शहर",
        weatherSearchPlaceholder: "उदा: कोल्हापूर",
        weatherSearchButton: "शोधा",
        weatherLoginRequired: "हवामान माहिती पाहण्यासाठी कृपया लॉगिन करा.",
        weatherShortQuery: "कृपया किमान 2 अक्षरे लिहा.",
        weatherSearching: "शोधत आहे...",
        weatherNoPlaces: "ठिकाण सापडले नाही? जवळच्या तालुक्याचे किंवा जिल्ह्याचे नाव टाकून पहा (उदा. Kolhapur, Baramati, Nashik).",
        weatherChoose: "तुमचे ठिकाण निवडा:",
        weatherLoading: "हवामान लोड होत आहे...",
        weatherError: "हवामान लोड करता आले नाही. कृपया पुन्हा प्रयत्न करा.",
        weatherNow: "सध्या",
        weatherHumidity: "आर्द्रता",
        weatherWind: "वारा",
        weatherRain: "पाऊस",
        weatherForecastTitle: "पुढील 5 दिवस",
        weatherAdviceTitle: "शेती सल्ले",
        weatherNoAdvice: "विशेष हवामान इशारे नाहीत. शेतीची नेहमीची कामे सुरू ठेवता येतील.",
        weatherToday: "आज",
        weatherTomorrow: "उद्या",
        weatherNote: "ही सर्वसाधारण माहिती आहे. अचूक सल्ल्यासाठी तुमच्या स्थानिक कृषी कार्यालयाशी (कृषी विज्ञान केंद्र) संपर्क साधा. फवारणी करताना नेहमी औषधाच्या लेबलवरील सूचना पाळा.",
        weatherCredit: "हवामान माहिती: Open-Meteo.com",
        wx_clear: "स्वच्छ आकाश",
        wx_cloudy: "ढगाळ",
        wx_fog: "धुके",
        wx_drizzle: "भुरभुर पाऊस",
        wx_rain: "पाऊस",
        wx_showers: "पावसाच्या सरी",
        wx_thunder: "मेघगर्जनेसह पाऊस",
        wx_snow: "बर्फवृष्टी",
        advice_heavy_rain: "जोरदार पावसाची शक्यता आहे. फवारणी आणि खत देणे पुढे ढकला आणि शेतातील पाण्याचा निचरा होईल याची खात्री करा.",
        advice_rain_expected: "लवकरच पाऊस पडण्याची शक्यता आहे. पावसाच्या आधी फवारणी किंवा खत देणे टाळा.",
        advice_heat: "पुढील दिवस खूप उष्ण राहतील. सकाळी लवकर किंवा संध्याकाळी पाणी द्या आणि जमिनीवर आच्छादन (मल्चिंग) करा.",
        advice_cold: "पुढील रात्री थंड राहतील. कोवळ्या रोपांचे संरक्षण करा आणि संध्याकाळी उशिरा पाणी देणे टाळा.",
        advice_strong_wind: "जोरदार वारा अपेक्षित आहे. वाऱ्याच्या दिवशी फवारणी करू नका आणि उंच पिकांना आधार द्या.",
        advice_humid_disease: "दमट हवामानामुळे बुरशीजन्य रोगांचा धोका वाढतो. पानांवर डाग किंवा बुरशी दिसते का ते नियमित तपासा.",
        advice_dry_spell: "पुढील 5 दिवस पाऊस कमी किंवा नाही. सिंचनाचे नियोजन करा.",
        advice_good_spray_window: "फवारणीसाठी चांगला दिवस: वारा कमी आणि पाऊस नाही.",
        recordsTagline: "📚 तुमच्या नोंदी",
        historyTitle: "अलीकडील पीक विश्लेषणे",
        historyDescription: "तुम्ही आधी जतन केलेल्या पीक शिफारसी पहा.",
        refreshHistory: "यादी रीफ्रेश करा",
        filterLabel: "भाषेनुसार फिल्टर करा:",
        allLanguages: "सर्व भाषा",
        historyLogin: "जतन केलेली विश्लेषणे पाहण्यासाठी लॉगिन करा.",
        historyLoading: "जतन केलेली विश्लेषणे लोड होत आहेत...",
        historyNone: "अजून कोणतेही विश्लेषण जतन केलेले नाही.",
        historyNoneLanguage: "या भाषेतील कोणतेही विश्लेषण आढळले नाही.",
        historyShowing: "एकूण {total} पैकी {shown} जतन केलेल्या नोंदी दाखवत आहे.",
        historyError: "यादी लोड करता आली नाही. कृपया रीफ्रेश करून पहा.",
        historyProblem: "समस्या:",
        historyLanguage: "भाषा:",
        historyDate: "तारीख:",
        historyView: "शिफारसी पहा",
        notSpecified: "नमूद केलेले नाही",
        dateUnavailable: "तारीख उपलब्ध नाही",
        voiceStart: "🎙️ समस्या बोला",
        voiceStop: "⏹️ बोलणे थांबवा",
        voiceListening: "🔴 ऐकत आहे... कृपया आता बोला",
        voiceNotSupported: "या ब्राउझरमध्ये व्हॉइस इनपुट उपलब्ध नाही. कृपया Chrome किंवा Edge वापरा.",
        voicePermissionDenied: "मायक्रोफोन परवानगी नाकारली गेली. कृपया ब्राउझरमध्ये मायक्रोफोन सुरू करा.",
        voiceNoSpeech: "कोणताही आवाज आला नाही. कृपया पुन्हा प्रयत्न करा.",
        voiceError: "व्हॉइस इनपुटमध्ये अडचण आली. कृपया पुन्हा प्रयत्न करा.",
        ttsPlay: "🔊 मार्गदर्शन ऐका",
        ttsStop: "⏹️ आवाज थांबवा",
        ttsNotSupported: "या ब्राउझरमध्ये ऑडिओ प्लेबॅक उपलब्ध नाही.",
        shareWhatsApp: "व्हॉट्सॲपवर पाठवा",
        helplineTagline: "📞 २४/७ तज्ज्ञ सहाय्य",
        helplineTitle: "शेतकरी मदत क्रमांक (हेल्पलाइन)",
        helplineDescription: "तातडीच्या मदतीसाठी कृषी शास्त्रज्ञ आणि शासकीय सल्लागारांशी थेट संपर्क साधा.",
        kccTitle: "किसान कॉल सेंटर (KCC)",
        kccDesc: "भारत सरकारची मोफत टोल-फ्री हेल्पलाइन. सकाळी ६ ते रात्री १० पर्यंत २२ भाषांमध्ये मोफत मार्गदर्शन.",
        pmfbyTitle: "पीक विमा हेल्पलाइन (PMFBY)",
        pmfbyDesc: "पीक नुकसान भरपाई, दुष्काळ किंवा अतिवृष्टी विमा दावे आणि पॉलिसी माहितीसाठी.",
        kvkTitle: "कृषी विज्ञान केंद्र (KVK)",
        kvkDesc: "जिल्हास्तरीय कृषी संशोधन केंद्र - बियाणे, कीड प्रादुर्भाव आणि माती परीक्षण सल्ल्यासाठी.",
        callNow: "📞 आता कॉल करा",
        findKvk: "🌐 तुमचे KVK शोधा",
        fertTagline: "🧪 स्मार्ट खत व्यवस्थापन",
        fertTitle: "खताचे प्रमाण कॅल्क्युलेटर",
        fertDescription: "ICAR च्या मार्गदर्शक तत्त्वांवर आधारित युरिया, DAP, पोटॅश आणि सेंद्रिय खतांच्या शिफारसित गोण्यांचे प्रमाण मोजा. प्रत्यक्ष प्रमाण माती परीक्षणानुसार बदलावे.",
        fertCropLabel: "पिकाचे नाव (कोणतेही पीक)",
        fertCropPlaceholder: "निवडा किंवा कोणत्याही पिकाचे नाव लिहा (उदा. हळद, ड्रॅगन फ्रूट, कांदा)...",
        fertCropHint: "💡 सर्व पिकांसाठी लागू: भाजीपाला, फळबागा, फुले, नगदी पिके, मसाले, तृणधान्ये व कडधान्ये.",
        fertAreaLabel: "शेताचे क्षेत्रफळ",
        fertUnitLabel: "क्षेत्रफळ एकक",
        unitAcre: "एकर (Acres)",
        unitGuntha: "गुंठे (Gunthas - १/४० एकर)",
        unitHectare: "हेक्टर (Hectares)",
        fertCalculateBtn: "🧪 खताचे प्रमाण मोजा",
        fertCalculating: "हिशोब करत आहे...",
        listenDosage: "डोस ऐका",
        shareDosage: "व्हॉट्सॲपवर पाठवा",
        fertBagsTitle: "🛍️ खरेदी करावयाच्या खतांच्या शिफारसित गोण्या (अंदाजे प्रमाण):",
        bagsLabel: "गोण्या",
        pureNutrients: "शुद्ध अन्नद्रव्ये (नत्र-स्फुरद-पालाश):",
        microNutrient: "सूक्ष्म / दुय्यम अन्नद्रव्य:",
        fertScheduleTitle: "📅 खत देण्याचे वेळापत्रक (विभागून द्या):",
        organicBoostersTitle: "सेंद्रिय खते व जमीन आरोग्य सुधारक",
        fertDisclaimer: "⚠️ हे प्रमाण ICAR आणि कृषी विद्यापीठांच्या शिफारशींवर आधारित आहे. अचूक खत व्यवस्थापनासाठी जवळच्या कृषी विज्ञान केंद्रात (KVK) दर २ वर्षांनी माती परीक्षण अवश्य करा.",
        fertError: "कृपया योग्य क्षेत्रफळ प्रविष्ट करा.",
        fertSuccess: "खताचे प्रमाण यशस्वीरीत्या मोजले.",
        fymTonnesText: "शिफारस केलेले शेणखत / कंपोस्ट: {tonnes} टन",
        guestModeBanner: "🌱 अतिथी मोड: मोफत तात्काळ विश्लेषण, खात्याची गरज नाही. इतिहास जतन करण्यासाठी खाली लॉगिन करा.",
        guestResultNotice: "🌱 तुम्ही अतिथी मोडमध्ये आहात — हे निदान इतिहासात जतन केलेले नाही.",
        aiCautionTitle: "⚠️ महत्त्वाची सावधगिरीची सूचना",
        aiCautionBody: "हे AI आधारित मार्गदर्शन आहे. फोटोद्वारे रोगनिदान आणि कीटकनाशक शिफारसी पूर्णपणे अचूक नसतील. कोणतीही रासायनिक औषध फवारणी करण्यापूर्वी जवळच्या कृषी विज्ञान केंद्रातील (KVK) शास्त्रज्ञ किंवा कृषी अधिकाऱ्यांचा सल्ला अवश्य घ्या.",
        privacyTitle: "गोपनीयता आणि डेटा नियंत्रण",
        privacyNotice: "तुमच्या पिकाची माहिती आणि विश्लेषण इतिहास तुमच्या खात्यात सुरक्षित साठवला जातो. व्हॉइस इनपुट ब्राऊझरची स्पीच सेवा वापरते (जी ऑडिओ डिव्हाइस/गुगलच्या स्पीच सर्व्हरवर पाठवते). तुम्ही तुमचा इतिहास किंवा खाते कधीही हटवू शकता.",
        clearHistoryButton: "🗑️ संपूर्ण इतिहास हटवा",
        deleteAccountButton: "⚠️ खाते कायमचे हटवा",
        confirmClearHistory: "तुम्हाला खरोखर सर्व जतन केलेले पीक विश्लेषण हटवायचे आहे का?",
        confirmDeleteAccount: "तुम्हाला तुमचे KrishiAI खाते आणि सर्व डेटा कायमचा हटवायचा आहे का? ही क्रिया पूर्ववत केली जाऊ शकत नाही.",
        historyClearedSuccess: "सर्व पीक विश्लेषण इतिहास यशस्वीपणे हटवला आहे.",
        accountDeletedSuccess: "तुमचे खाते आणि सर्व डेटा कायमचा हटवला गेला आहे."
    },

    hi: {
        languageLabel: "भाषा",
        tagline: "🌾 AI आधारित कृषि",
        heroTitle: "आपका AI कृषि",
        heroTitleAccent: "सहायक",
        heroDescription: "खेती के लिए सरल मार्गदर्शन, फसल की समस्याओं का विश्लेषण, रोगों की पहचान और मौसम से जुड़ी जानकारी प्राप्त करें।",
        heroButton: "फसल की समस्या का विश्लेषण करें →",
        featureImageTitle: "फसल की तस्वीर अपलोड करें",
        featureImageDescription: "विश्लेषण के लिए फसल की तस्वीर अपलोड करें।",
        featureAITitle: "AI मार्गदर्शन",
        featureAIDescription: "खेती के लिए सरल सुझाव प्राप्त करें।",
        featureWeatherTitle: "मौसम की जानकारी",
        featureWeatherDescription: "मौसम से फसल को होने वाले संभावित नुकसान को समझें।",
        featureLanguageTitle: "कई भाषाएँ",
        featureLanguageDescription: "मराठी, हिंदी और अंग्रेज़ी का समर्थन।",
        analysisTagline: "🔍 फसल विश्लेषण",
        analysisTitle: "अपनी फसल की समस्या बताएँ",
        analysisDescription: "अपनी फसल में क्या समस्या है, KrishiAI को बताएँ।",
        cropLabel: "फसल का नाम",
        cropPlaceholder: "उदाहरण: मूंगफली, गन्ना, कपास, टमाटर...",
        cropFieldHint: "💡 आप किसी भी फसल का नाम हिंदी, मराठी या अंग्रेजी में लिख सकते हैं (उदा. मूंगफली, कपास, गन्ना, चना, टमाटर)।",
        problemLabel: "समस्या का वर्णन करें",
        problemPlaceholder: "उदाहरण: टमाटर के पत्ते पीले हो रहे हैं...",
        imageLabel: "फसल की तस्वीर अपलोड करें",
        analyzeButton: "🤖 KrishiAI से विश्लेषण करें",
        footer: "🌱 KrishiAI — भारतीय किसानों के लिए AI आधारित कृषि सहायता",
        missingTitle: "⚠️ जानकारी अधूरी है",
        missingMessage: "कृपया फसल का नाम लिखें, समस्या का विवरण दें या फोटो अपलोड करें।",
        loadingTitle: "🤖 KrishiAI विश्लेषण कर रहा है...",
        loadingMessage: "कृपया प्रतीक्षा करें। आपकी फसल की जानकारी का विश्लेषण हो रहा है।",
        resultTitle: "🌱 KrishiAI फसल विश्लेषण",
        cropResult: "फसल",
        imageResult: "तस्वीर",
        guidanceTitle: "🤖 AI कृषि मार्गदर्शन",
        warningTitle: "🌱 KrishiAI",
        disclaimer: "यह AI द्वारा तैयार की गई जानकारी केवल मार्गदर्शन के लिए है। इसे योग्य कृषि विशेषज्ञ की सलाह का विकल्प न मानें।",
        errorTitle: "❌ विश्लेषण विफल",
        connectionError: "वर्तमान में जानकारी प्राप्त करने में रुकावट आ रही है। कृपया 1 मिनट बाद पुनः प्रयास करें अथवा किसान हेल्पलाइन से संपर्क करें।",
        serverError: "",
        busyTitle: "कृषि सलाहकार सहायक अस्थायी रूप से व्यस्त है",
        busyMessage: "वर्तमान में AI सेवा पर अधिक लोड होने के कारण संपर्क नहीं हो पा रहा है। कृपया 1 मिनट बाद पुनः प्रयास करें या किसान हेल्पलाइन पर संपर्क करें।",
        busyHelplineNotice: "फसल समस्या के तुरंत समाधान हेतु सरकारी कृषि वैज्ञानिकों से निशुल्क बात करें:",
        busyCallKcc: "किसान कॉल सेंटर पर कॉल करें (टोल-फ्री 1800-180-1551)",
        noAnalysis: "AI सेवा से कोई विश्लेषण प्राप्त नहीं हुआ।",

        authTagline: "आपका खाता",
        authTitle: "लॉगिन करें या खाता बनाएँ",
        authDescription: "अपने फसल विश्लेषण सहेजने और देखने के लिए साइन इन करें।",
        authTitleLoggedIn: "स्वागत है, {username}! 👨‍🌾",
        authDescLoggedIn: "आपका खाता सक्रिय है। आपके फसल विश्लेषण सुरक्षित रूप से सहेजे जा रहे हैं।",
        activeSession: "🟢 सक्रिय खाता",
        loginTitle: "लॉगिन",
        emailLabel: "ईमेल",
        passwordLabel: "पासवर्ड",
        usernameLabel: "उपयोगकर्ता नाम",
        loginButton: "लॉगिन करें",
        registerTitle: "खाता बनाएँ",
        registerButton: "खाता बनाएँ",
        passwordHint: "कम से कम 8 अक्षर।",
        logoutButton: "लॉगआउट",
        authWait: "कृपया प्रतीक्षा करें...",
        loginSuccess: "लॉगिन सफल रहा।",
        accountCreated: "खाता बन गया। कृपया लॉगिन करें।",
        loggedOut: "आप लॉगआउट हो गए हैं।",
        sessionExpired: "आपका सत्र समाप्त हो गया है। कृपया फिर से लॉगिन करें।",
        loginRequired: "फसल का विश्लेषण करने के लिए कृपया लॉगिन करें।",
        loggedInAs: "लॉगिन किया हुआ खाता:",
        yourAccount: "आपका खाता",
        farmerUser: "किसान",
        errInvalidLogin: "ईमेल या पासवर्ड गलत है।",
        errExists: "यह उपयोगकर्ता नाम या ईमेल पहले से पंजीकृत है।",
        errCheckDetails: "कृपया अपनी जानकारी जाँचें। उपयोगकर्ता नाम कम से कम 3 अक्षर का और पासवर्ड कम से कम 8 अक्षर का होना चाहिए; आसानी से अनुमान लगाया जा सकने वाला पासवर्ड न रखें।",
        errAuthFailed: "पहचान की पुष्टि नहीं हो सकी।",
        errConnection: "सर्वर से संपर्क नहीं हो पाया।",
        errTooManyAttempts: "बहुत बार गलत प्रयास हो चुका है। कृपया कुछ मिनट रुककर फिर से प्रयास करें।",
        errImageTooLarge: "फ़ोटो बहुत बड़ी है। कृपया छोटी फ़ोटो चुनें (अधिकतम 5 MB)।",
        weatherTagline: "🌦️ मौसम की जानकारी",
        weatherTitle: "आपके खेत का मौसम",
        weatherDescription: "अपना गाँव या शहर खोजें और मौसम का पूर्वानुमान व आसान खेती सलाह देखें।",
        weatherSearchLabel: "गाँव या शहर",
        weatherSearchPlaceholder: "जैसे: कोल्हापुर",
        weatherSearchButton: "खोजें",
        weatherLoginRequired: "मौसम की जानकारी देखने के लिए कृपया लॉगिन करें।",
        weatherShortQuery: "कृपया कम से कम 2 अक्षर लिखें।",
        weatherSearching: "खोज रहे हैं...",
        weatherNoPlaces: "स्थान नहीं मिला। अपने निकटतम तहसील या जिले का नाम लिखकर खोजें (जैसे: Kolhapur, Baramati, Bhopal)।",
        weatherChoose: "अपनी जगह चुनें:",
        weatherLoading: "मौसम लोड हो रहा है...",
        weatherError: "मौसम लोड नहीं हो सका। कृपया फिर से प्रयास करें।",
        weatherNow: "अभी",
        weatherHumidity: "नमी",
        weatherWind: "हवा",
        weatherRain: "बारिश",
        weatherForecastTitle: "अगले 5 दिन",
        weatherAdviceTitle: "खेती की सलाह",
        weatherNoAdvice: "कोई विशेष मौसम चेतावनी नहीं है। खेती के सामान्य काम जारी रख सकते हैं।",
        weatherToday: "आज",
        weatherTomorrow: "कल",
        weatherNote: "यह सामान्य जानकारी है। सटीक सलाह के लिए अपने स्थानीय कृषि कार्यालय (कृषि विज्ञान केंद्र) से संपर्क करें। छिड़काव करते समय हमेशा दवा के लेबल के निर्देश मानें।",
        weatherCredit: "मौसम डेटा: Open-Meteo.com",
        wx_clear: "साफ़ आसमान",
        wx_cloudy: "बादल",
        wx_fog: "कोहरा",
        wx_drizzle: "हल्की फुहार",
        wx_rain: "बारिश",
        wx_showers: "बौछारें",
        wx_thunder: "बिजली के साथ बारिश",
        wx_snow: "बर्फ़बारी",
        advice_heavy_rain: "भारी बारिश की संभावना है। छिड़काव और खाद डालना टालें और खेत से पानी निकलने की व्यवस्था रखें।",
        advice_rain_expected: "जल्द बारिश की संभावना है। बारिश से ठीक पहले छिड़काव या खाद डालने से बचें।",
        advice_heat: "आने वाले दिन बहुत गर्म रहेंगे। सुबह जल्दी या शाम को सिंचाई करें और मिट्टी पर मल्चिंग करें।",
        advice_cold: "आने वाली रातें ठंडी रहेंगी। छोटे पौधों को बचाएँ और शाम को देर से पानी देने से बचें।",
        advice_strong_wind: "तेज़ हवा चलने की संभावना है। हवा वाले दिन छिड़काव न करें और ऊँची फसलों को सहारा दें।",
        advice_humid_disease: "नमी भरे मौसम में फफूँदी रोगों का खतरा बढ़ जाता है। पत्तियों पर धब्बे या फफूँद की नियमित जाँच करें।",
        advice_dry_spell: "अगले 5 दिन बारिश बहुत कम या नहीं होगी। सिंचाई की योजना बनाएँ।",
        advice_good_spray_window: "छिड़काव के लिए अच्छा दिन: हवा कम और बारिश नहीं।",
        recordsTagline: "📚 आपके रिकॉर्ड",
        historyTitle: "हाल के फसल विश्लेषण",
        historyDescription: "पहले सहेजी गई अपनी फसल सिफारिशें देखें।",
        refreshHistory: "सूची रीफ़्रेश करें",
        filterLabel: "भाषा के अनुसार फ़िल्टर करें:",
        allLanguages: "सभी भाषाएँ",
        historyLogin: "सहेजे गए विश्लेषण देखने के लिए लॉगिन करें।",
        historyLoading: "सहेजे गए विश्लेषण लोड हो रहे हैं...",
        historyNone: "अभी तक कोई विश्लेषण सहेजा नहीं गया है।",
        historyNoneLanguage: "इस भाषा में कोई विश्लेषण नहीं मिला।",
        historyShowing: "कुल {total} में से {shown} सहेजे गए रिकॉर्ड दिखा रहे हैं।",
        historyError: "सूची लोड नहीं हो सकी। कृपया रीफ़्रेश करके देखें।",
        historyProblem: "समस्या:",
        historyLanguage: "भाषा:",
        historyDate: "तारीख:",
        historyView: "सिफारिशें देखें",
        notSpecified: "उल्लेख नहीं किया गया",
        dateUnavailable: "तारीख उपलब्ध नहीं",
        voiceStart: "🎙️ समस्या बोलें",
        voiceStop: "⏹️ बोलना रोकें",
        voiceListening: "🔴 सुन रहा हूँ... कृपया अब बोलें",
        voiceNotSupported: "इस ब्राउज़र में वॉइस इनपुट उपलब्ध नहीं है। कृपया Chrome या Edge का उपयोग करें।",
        voicePermissionDenied: "माइक्रोफ़ोन की अनुमति नहीं मिली। कृपया ब्राउज़र सेटिंग्स में माइक्रोफ़ोन चालू करें।",
        voiceNoSpeech: "कोई आवाज़ नहीं मिली। कृपया पुनः प्रयास करें।",
        voiceError: "वॉइस इनपुट में त्रुटि आई। कृपया पुनः प्रयास करें।",
        ttsPlay: "🔊 मार्गदर्शन सुनें",
        ttsStop: "⏹️ आवाज़ रोकें",
        ttsNotSupported: "इस ब्राउज़र में ऑडियो प्लेबैक उपलब्ध नहीं है।",
        shareWhatsApp: "व्हाट्सएप पर साझा करें",
        helplineTagline: "📞 24/7 विशेषज्ञ सहायता",
        helplineTitle: "आपातकालीन किसान हेल्पलाइन",
        helplineDescription: "तत्काल आपातकालीन सहायता के लिए कृषि विशेषज्ञों और सरकारी सलाहकारों से सीधे बात करें।",
        kccTitle: "किसान कॉल सेंटर (KCC)",
        kccDesc: "भारत सरकार की टोल-फ्री हेल्पलाइन। सुबह 6 से रात 10 बजे तक 22 भाषाओं में निःशुल्क मार्गदर्शन।",
        pmfbyTitle: "फसल बीमा हेल्पलाइन (PMFBY)",
        pmfbyDesc: "फसल नुकसान मुआवजे, सूखा/बाढ़ बीमा दावों और पॉलिसी की स्थिति के लिए।",
        kvkTitle: "कृषि विज्ञान केंद्र (KVK)",
        kvkDesc: "जिला कृषि अनुसंधान केंद्र - बीज परीक्षण, कीट प्रकोप और मृदा स्वास्थ्य सलाह के लिए।",
        callNow: "📞 अभी कॉल करें",
        findKvk: "🌐 अपना KVK खोजें",
        fertTagline: "🧪 स्मार्ट पोषण प्रबंधन",
        fertTitle: "खाद और खुराक कैलकुलेटर",
        fertDescription: "ICAR दिशानिर्देशों के अनुसार अपनी फसल के लिए यूरिया, डीएपी, पोटाश और जैविक खाद के अनुशंसित बोरों का अनुमान लगाएँ। सटीक मात्रा मृदा परीक्षण पर निर्भर करती है।",
        fertCropLabel: "फसल का नाम (कोई भी फसल)",
        fertCropPlaceholder: "चुनें या किसी भी फसल का नाम लिखें (जैसे हल्दी, ड्रैगन फ्रूट, टमाटर)...",
        fertCropHint: "💡 सभी फसलों के लिए उपयोगी: सब्जियां, फल, फूल, नकदी फसलें, मसाले, अनाज व दलहन।",
        fertAreaLabel: "खेत का आकार / क्षेत्रफल",
        fertUnitLabel: "इकाई",
        unitAcre: "एकड़ (Acres)",
        unitGuntha: "गुंठा (Gunthas)",
        unitHectare: "हेक्टेयर (Hectares)",
        fertCalculateBtn: "🧪 खाद की मात्रा निकालें",
        fertCalculating: "गणना की जा रही है...",
        listenDosage: "खुराक सुनें",
        shareDosage: "व्हाट्सएप पर भेजें",
        fertBagsTitle: "🛍️ खरीदे जाने वाले उर्वरक के अनुशंसित बोरे (अनुमानित मात्रा):",
        bagsLabel: "बोरे",
        pureNutrients: "शुद्ध पोषक तत्व (N-P-K):",
        microNutrient: "सूक्ष्म / द्वितीयक पोषक तत्व:",
        fertScheduleTitle: "📅 खाद देने की समय सारणी (विभाजित खुराक):",
        organicBoostersTitle: "जैविक खाद व मिट्टी स्वास्थ्य सुधारक",
        fertDisclaimer: "⚠️ यह सिफारिश ICAR और कृषि विश्वविद्यालयों के मानकों पर आधारित है। सटीक खेती के लिए निकटतम कृषि विज्ञान केंद्र (KVK) पर हर 2 वर्ष में मिट्टी की जाँच अवश्य कराएँ।",
        fertError: "कृपया सही क्षेत्रफल दर्ज करें।",
        fertSuccess: "खाद की खुराक की गणना सफल रही।",
        fymTonnesText: "अनुशंसित गोबर की खाद / कंपोस्ट: {tonnes} टन",
        guestModeBanner: "🌱 गेस्ट मोड: निःशुल्क त्वरित विश्लेषण, खाते की आवश्यकता नहीं। इतिहास सुरक्षित रखने के लिए नीचे लॉगिन करें।",
        guestResultNotice: "🌱 आप गेस्ट मोड में हैं — यह निदान इतिहास में सहेजा नहीं गया है।",
        aiCautionTitle: "⚠️ महत्वपूर्ण सावधानी सलाह",
        aiCautionBody: "यह AI-जनरेटेड सलाह है। फोटो से रोग पहचान और कीटनाशक सुझाव शत-प्रतिशत सही नहीं हो सकते। किसी भी रासायनिक कीटनाशक के छिड़काव से पहले अपने स्थानीय कृषि विज्ञान केंद्र (KVK) या कृषि विशेषज्ञ से पुष्टि अवश्य करें।",
        privacyTitle: "गोपनीयता और डेटा नियंत्रण",
        privacyNotice: "आपकी फसल का विवरण और निदान इतिहास आपके निजी खाते में सुरक्षित संग्रहीत रहता है। वॉयस इनपुट ब्राउज़र की वाक् पहचान सेवा का उपयोग करता है (जो ऑडियो को डिवाइस/गूगल के स्पीच सर्वर पर भेजता है)। आप कभी भी अपना इतिहास या खाता हटा सकते हैं।",
        clearHistoryButton: "🗑️ सारा इतिहास हटाएं",
        deleteAccountButton: "⚠️ खाता हमेशा के लिए हटाएं",
        confirmClearHistory: "क्या आप वाकई अपने सभी सहेजे गए फसल विश्लेषण हटाना चाहते हैं?",
        confirmDeleteAccount: "क्या आप वाकई अपना KrishiAI खाता और सारा डेटा हमेशा के लिए हटाना चाहते हैं? यह क्रिया वापस नहीं ली जा सकती।",
        historyClearedSuccess: "सभी फसल विश्लेषण इतिहास सफलतापूर्वक हटा दिया गया है।",
        accountDeletedSuccess: "आपका खाता और सारा डेटा हमेशा के लिए हटा दिया गया है।"
    }
};


// Translate all marked website text.
function setLanguage(language) {
    const selectedLanguage = translations[language]
        ? language
        : "en";

    const dictionary = translations[selectedLanguage];

    document.documentElement.lang = selectedLanguage;

    document.querySelectorAll("[data-i18n]").forEach(element => {
        const key = element.dataset.i18n;

        if (dictionary[key] !== undefined) {
            element.textContent = dictionary[key];
        }
    });

    document.querySelectorAll("[data-i18n-placeholder]").forEach(element => {
        const key = element.dataset.i18nPlaceholder;

        if (dictionary[key] !== undefined) {
            element.placeholder = dictionary[key];
        }
    });

    document.title = {
        en: "KrishiAI - AI Farming Assistant",
        mr: "KrishiAI - AI शेती सहाय्यक",
        hi: "KrishiAI - AI कृषि सहायक"
    }[selectedLanguage];

    if (typeof stopReadAloud === "function") {
        stopReadAloud();
    }
    if (typeof stopVoiceInput === "function") {
        stopVoiceInput();
    }

    return selectedLanguage;
}


function getLanguage() {
    return document.getElementById("language").value;
}


function t(key) {
    const language = getLanguage();
    return translations[language]?.[key] || translations.en[key] || key;
}


function scrollToAnalyzer() {
    // Logged-out visitors are taken to the login section first.
    const targetId = authToken ? "analyzer" : "authSection";

    document.getElementById(targetId).scrollIntoView({
        behavior: "smooth",
        block: "start"
    });
}


// Change the complete static UI immediately.


function refreshDynamicText() {
    setAuthStatus("");
    setWeatherStatus("");
    updateAuthUI();
    renderWeather();
    if (typeof lastFertilizerData !== "undefined" && lastFertilizerData) {
        renderFertilizerResult(lastFertilizerData);
    }

    const historyStatus = document.getElementById("historyStatus");
    const records = window.krishiAIHistoryRecords || [];

    if (!authToken) {
        historyStatus.textContent = t("historyLogin");
    } else if (records.length) {
        renderAnalysisHistory(records);
    } else {
        historyStatus.textContent = t("historyNone");
    }
}

// ---- Voice Input (Speech-to-Text) & Audio (Text-to-Speech) -------------
let recognition = null;
let isVoiceListening = false;
let voiceBaseText = "";

function stopVoiceInput() {
    if (recognition && isVoiceListening) {
        try {
            recognition.stop();
        } catch (e) {
            // Ignore
        }
    }
    resetVoiceUI();
}

function resetVoiceUI() {
    isVoiceListening = false;
    const voiceButton = document.getElementById("voiceButton");
    const voiceButtonText = document.getElementById("voiceButtonText");
    const voiceStatus = document.getElementById("voiceStatus");

    if (voiceButton) {
        voiceButton.classList.remove("listening");
    }
    if (voiceButtonText) {
        voiceButtonText.textContent = t("voiceStart");
    }
    if (voiceStatus && voiceStatus.classList.contains("listening")) {
        voiceStatus.classList.add("hidden");
        voiceStatus.classList.remove("listening");
    }
}

function toggleVoiceInput() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const voiceButton = document.getElementById("voiceButton");
    const voiceButtonText = document.getElementById("voiceButtonText");
    const voiceStatus = document.getElementById("voiceStatus");
    const problemInput = document.getElementById("problem");

    if (!SpeechRecognition) {
        if (voiceStatus) {
            voiceStatus.textContent = t("voiceNotSupported");
            voiceStatus.classList.remove("hidden", "listening");
        }
        return;
    }

    if (isVoiceListening) {
        stopVoiceInput();
        return;
    }

    stopReadAloud();

    try {
        recognition = new SpeechRecognition();
    } catch (err) {
        if (voiceStatus) {
            voiceStatus.textContent = t("voiceNotSupported");
            voiceStatus.classList.remove("hidden", "listening");
        }
        return;
    }

    const langMap = {
        en: "en-IN",
        mr: "mr-IN",
        hi: "hi-IN"
    };
    recognition.lang = langMap[getLanguage()] || "en-IN";
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    recognition.continuous = false;

    voiceBaseText = problemInput.value.trim();

    recognition.onstart = () => {
        isVoiceListening = true;
        if (voiceButton) voiceButton.classList.add("listening");
        if (voiceButtonText) voiceButtonText.textContent = t("voiceStop");
        if (voiceStatus) {
            voiceStatus.textContent = t("voiceListening");
            voiceStatus.classList.remove("hidden");
            voiceStatus.classList.add("listening");
        }
    };

    recognition.onresult = (event) => {
        let transcript = "";
        for (let i = 0; i < event.results.length; i++) {
            transcript += event.results[i][0].transcript;
        }
        const separator = voiceBaseText ? " " : "";
        problemInput.value = voiceBaseText + separator + transcript;
    };

    recognition.onerror = (event) => {
        console.warn("Speech recognition error:", event.error);
        if (voiceStatus) {
            if (event.error === "not-allowed" || event.error === "service-not-allowed") {
                voiceStatus.textContent = t("voicePermissionDenied");
            } else if (event.error === "no-speech") {
                voiceStatus.textContent = t("voiceNoSpeech");
            } else {
                voiceStatus.textContent = t("voiceError");
            }
            voiceStatus.classList.remove("hidden", "listening");
        }
        resetVoiceUI();
    };

    recognition.onend = () => {
        resetVoiceUI();
    };

    try {
        recognition.start();
    } catch (e) {
        console.warn("Recognition start failed:", e);
        resetVoiceUI();
    }
}

// ---- Audio Read-Aloud (Text-to-Speech) -----------------------------------
let currentUtterance = null;
let currentTTSButton = null;

function stripMarkdown(text) {
    if (!text) return "";
    return text
        .replace(/\\([*#_`~[\]()])/g, "$1")
        .replace(/^#{1,6}\s+/gm, "")
        .replace(/\*\*([^*]+)\*\*/g, "$1")
        .replace(/\*([^*]+)\*/g, "$1")
        .replace(/__([^_]+)__/g, "$1")
        .replace(/_([^_]+)_/g, "$1")
        .replace(/`([^`]+)`/g, "$1")
        .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
        .replace(/^\s*[-*+]\s+/gm, "")
        .replace(/^\s*\d+\.\s+/gm, "")
        .replace(/^\s*---+\s*$/gm, "")
        .replace(/\n{2,}/g, ". ")
        .replace(/\n/g, " ")
        .trim();
}

function resetTTSUI() {
    if (currentTTSButton) {
        currentTTSButton.classList.remove("speaking");
        const label = currentTTSButton.querySelector("#ttsButtonText") || currentTTSButton;
        if (label === currentTTSButton) {
            currentTTSButton.textContent = `🔊 ${t("ttsPlay")}`;
        } else {
            label.textContent = t("ttsPlay");
        }
        currentTTSButton = null;
    }
    const mainTTS = document.getElementById("ttsButton");
    const mainTTSText = document.getElementById("ttsButtonText");
    if (mainTTS) mainTTS.classList.remove("speaking");
    if (mainTTSText) mainTTSText.textContent = t("ttsPlay");
    currentUtterance = null;
}

function stopReadAloud() {
    if ("speechSynthesis" in window) {
        window.speechSynthesis.cancel();
    }
    resetTTSUI();
}

function toggleReadAloud(rawText, langCode, triggerButton = null) {
    if (!("speechSynthesis" in window)) {
        alert(t("ttsNotSupported"));
        return;
    }

    const button = triggerButton || document.getElementById("ttsButton");

    if (window.speechSynthesis.speaking) {
        const wasCurrentButton = currentTTSButton === button;
        stopReadAloud();
        if (wasCurrentButton) {
            return;
        }
    }

    const textToSpeak = stripMarkdown(rawText);
    if (!textToSpeak) return;

    stopVoiceInput();
    window.speechSynthesis.cancel();

    const langMap = {
        en: "en-IN",
        mr: "mr-IN",
        hi: "hi-IN"
    };
    const targetLang = langMap[langCode] || langMap[getLanguage()] || "en-IN";

    const utterance = new SpeechSynthesisUtterance(textToSpeak);
    utterance.lang = targetLang;
    utterance.rate = 0.92;

    const voices = window.speechSynthesis.getVoices();
    if (voices && voices.length > 0) {
        const langPrefix = targetLang.split("-")[0].toLowerCase();
        const matched = voices.find(v => v.lang.toLowerCase().startsWith(langPrefix))
                     || voices.find(v => v.lang.toLowerCase().includes(langPrefix));
        if (matched) {
            utterance.voice = matched;
        }
    }

    currentTTSButton = button;

    utterance.onstart = () => {
        if (button) {
            button.classList.add("speaking");
            const label = button.querySelector("#ttsButtonText") || button;
            if (label === button) {
                button.textContent = `⏹️ ${t("ttsStop")}`;
            } else {
                label.textContent = t("ttsStop");
            }
        }
    };

    utterance.onend = () => {
        resetTTSUI();
    };

    utterance.onerror = (e) => {
        console.warn("TTS playback error:", e);
        resetTTSUI();
    };

    currentUtterance = utterance;
    window.speechSynthesis.speak(utterance);
}

function playHistoryAudio(index, event) {
    const records = window.krishiAIFilteredRecords || window.krishiAIHistoryRecords || [];
    const record = records[index];
    if (!record || !record.analysis) return;
    const btn = event ? event.currentTarget : null;
    toggleReadAloud(record.analysis, record.language, btn);
}

function shareOnWhatsApp(customAnalysis, customCrop) {
    const crop = customCrop || document.getElementById("crop").value.trim() || t("notSpecified");
    const rawAnalysis = customAnalysis || window.currentAnalysisResult || "";
    const cleanAnalysis = stripMarkdown(rawAnalysis);

    let snippet = cleanAnalysis;
    if (snippet.length > 650) {
        snippet = snippet.substring(0, 647) + "...";
    }

    const message = `🌱 *KrishiAI Crop Analysis*\n\n` +
        `🌾 *${t("cropResult")}:* ${crop}\n\n` +
        `💡 *${t("guidanceTitle")}:*\n${snippet}\n\n` +
        `📱 _KrishiAI — AI Farming Assistant for Indian Farmers_`;

    const url = `https://api.whatsapp.com/send?text=${encodeURIComponent(message)}`;
    window.open(url, "_blank", "noopener,noreferrer");
}

function shareHistoryWhatsApp(index) {
    const records = window.krishiAIFilteredRecords || window.krishiAIHistoryRecords || [];
    const record = records[index];
    if (!record || !record.analysis) return;
    shareOnWhatsApp(record.analysis, record.crop);
}

// ---- Weather insights ---------------------------------------------------
let weatherPlaces = [];   // latest place search results
let selectedPlace = null; // the farmer's chosen village/town
let lastWeather = null;   // kept so the text can be re-translated instantly

const WEATHER_ICONS = {
    clear: "☀️",
    cloudy: "⛅",
    fog: "🌫️",
    drizzle: "🌦️",
    rain: "🌧️",
    showers: "🌧️",
    thunder: "⛈️",
    snow: "❄️"
};

// Group the WMO weather codes used by Open-Meteo.
function weatherGroup(code) {
    if (code <= 1) return "clear";
    if (code <= 3) return "cloudy";
    if (code === 45 || code === 48) return "fog";
    if (code >= 51 && code <= 57) return "drizzle";
    if (code >= 61 && code <= 67) return "rain";
    if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
    if (code >= 80 && code <= 82) return "showers";
    if (code >= 95) return "thunder";
    return "cloudy";
}

function placeLabel(place) {
    return [place.name, place.admin1].filter(Boolean).join(", ");
}

// The chosen place is remembered per user on this device.
function placeStorageKey() {
    return currentUser?.id ? `krishiai_place_${currentUser.id}` : null;
}

function savePlace(place) {
    const key = placeStorageKey();

    if (!key) return;

    try {
        localStorage.setItem(key, JSON.stringify(place));
    } catch (error) {
        // Remembering the place is optional.
    }
}

function loadSavedPlace() {
    const key = placeStorageKey();

    if (!key) return null;

    try {
        return JSON.parse(localStorage.getItem(key) || "null");
    } catch (error) {
        return null;
    }
}

function setWeatherStatus(message) {
    document.getElementById("weatherStatus").textContent = message || "";
}

function formatWeatherDate(isoDate, index) {
    if (index === 0) return t("weatherToday");
    if (index === 1) return t("weatherTomorrow");

    const locale = { en: "en-IN", mr: "mr-IN", hi: "hi-IN" }[getLanguage()] || "en-IN";

    return new Date(`${isoDate}T00:00:00`).toLocaleDateString(locale, {
        weekday: "short",
        day: "numeric",
        month: "short"
    });
}

function renderWeatherPlaces() {
    const list = document.getElementById("weatherPlaces");

    list.innerHTML = weatherPlaces.map((place, index) => {
        const details = [place.admin1, place.country].filter(Boolean).join(", ");

        return `
            <button type="button" class="weather-place" data-index="${index}">
                <strong>${escapeHtml(place.name)}</strong>
                ${details ? `<span>${escapeHtml(details)}</span>` : ""}
            </button>
        `;
    }).join("");
}

function renderWeather() {
    const box = document.getElementById("weatherResult");

    if (!lastWeather || !selectedPlace) {
        box.innerHTML = "";
        box.classList.add("hidden");
        return;
    }

    const { current, daily, advice } = lastWeather;
    const group = weatherGroup(current.code);

    const forecast = daily.map((day, index) => {
        const dayGroup = weatherGroup(day.code);

        return `
            <div class="weather-day">
                <div class="weather-day-name">${escapeHtml(formatWeatherDate(day.date, index))}</div>
                <div class="weather-day-icon" title="${escapeHtml(t("wx_" + dayGroup))}">${WEATHER_ICONS[dayGroup]}</div>
                <div><strong>${Math.round(day.temp_max)}°</strong> / ${Math.round(day.temp_min)}°</div>
                <div>🌧 ${Math.round(day.rain_mm)} mm</div>
            </div>
        `;
    }).join("");

    const tips = advice.length
        ? advice.map(item => {
            const dayIndex = daily.findIndex(day => day.date === item.date);
            const when = dayIndex >= 0
                ? `<strong>${escapeHtml(formatWeatherDate(item.date, dayIndex))}:</strong> `
                : "";

            return `<li>${when}${escapeHtml(t("advice_" + item.code))}</li>`;
        }).join("")
        : `<li>${escapeHtml(t("weatherNoAdvice"))}</li>`;

    box.innerHTML = `
        <h3>${escapeHtml(placeLabel(selectedPlace))}</h3>

        <div class="weather-now">
            <div class="weather-now-icon">${WEATHER_ICONS[group]}</div>
            <div>
                <div class="weather-now-temp">${Math.round(current.temperature)}°C</div>
                <div>${escapeHtml(t("weatherNow"))}: ${escapeHtml(t("wx_" + group))}</div>
                <div class="weather-now-details">
                    ${escapeHtml(t("weatherHumidity"))}: ${Math.round(current.humidity)}% ·
                    ${escapeHtml(t("weatherWind"))}: ${Math.round(current.wind_kmh)} km/h ·
                    ${escapeHtml(t("weatherRain"))}: ${current.rain_mm} mm
                </div>
            </div>
        </div>

        <h3>${escapeHtml(t("weatherForecastTitle"))}</h3>
        <div class="weather-forecast">${forecast}</div>

        <h3>${escapeHtml(t("weatherAdviceTitle"))}</h3>
        <ul class="weather-advice">${tips}</ul>

        <p class="weather-note">${escapeHtml(t("weatherNote"))}</p>
    `;

    box.classList.remove("hidden");
}

async function searchPlaces() {
    const input = document.getElementById("weatherSearch");
    const query = input.value.trim();

    if (query.length < 2) {
        setWeatherStatus(t("weatherShortQuery"));
        return;
    }

    setWeatherStatus(t("weatherSearching"));

    try {
        const headers = {};
        if (authToken) {
            headers["Authorization"] = `Bearer ${authToken}`;
        }

        const response = await fetch(
            `${API_URL}/weather/places?q=${encodeURIComponent(query)}&language=${getLanguage()}`,
            { headers }
        );

        if (response.status === 401) {
            handleSessionExpired();
            return;
        }

        if (!response.ok) {
            throw new Error(`Request failed: ${response.status}`);
        }

        const data = await response.json();

        weatherPlaces = data.places || [];
        renderWeatherPlaces();

        setWeatherStatus(
            weatherPlaces.length ? t("weatherChoose") : t("weatherNoPlaces")
        );
    } catch (error) {
        console.error("Place search failed:", error);
        setWeatherStatus(t("weatherError"));
    }
}

async function loadWeather() {
    if (!selectedPlace) return;

    setWeatherStatus(t("weatherLoading"));

    try {
        const headers = {};
        if (authToken) {
            headers["Authorization"] = `Bearer ${authToken}`;
        }

        const response = await fetch(
            `${API_URL}/weather?lat=${selectedPlace.latitude}&lon=${selectedPlace.longitude}`,
            { headers }
        );

        if (response.status === 401) {
            handleSessionExpired();
            return;
        }

        if (!response.ok) {
            throw new Error(`Request failed: ${response.status}`);
        }

        lastWeather = await response.json();
        setWeatherStatus("");
        renderWeather();
    } catch (error) {
        console.error("Weather request failed:", error);
        lastWeather = null;
        renderWeather();
        setWeatherStatus(t("weatherError"));
    }
}

function selectPlace(index) {
    const place = weatherPlaces[index];

    if (!place) return;

    selectedPlace = place;
    savePlace(place);

    weatherPlaces = [];
    renderWeatherPlaces();

    document.getElementById("weatherSearch").value = placeLabel(place);

    loadWeather();
}

// Show the farmer's saved place again after login or a page refresh.
function restoreWeather() {
    selectedPlace = loadSavedPlace();

    if (selectedPlace) {
        document.getElementById("weatherSearch").value = placeLabel(selectedPlace);
        loadWeather();
    }
}

// Remove all weather data from the page (used when logging out).
function clearWeather() {
    selectedPlace = null;
    lastWeather = null;
    weatherPlaces = [];

    document.getElementById("weatherSearch").value = "";
    document.getElementById("weatherPlaces").innerHTML = "";
    document.getElementById("weatherStatus").textContent = "";

    renderWeather();
}

document.getElementById("weatherSearchButton").addEventListener("click", searchPlaces);

document.getElementById("weatherSearch").addEventListener("keydown", event => {
    if (event.key === "Enter") {
        event.preventDefault();
        searchPlaces();
    }
});

document.getElementById("weatherPlaces").addEventListener("click", event => {
    const button = event.target.closest(".weather-place");

    if (button) {
        selectPlace(Number(button.dataset.index));
    }
});
// ---- End weather insights -----------------------------------------------

function syncLanguageButtons() {
    const current = getLanguage();

    document.querySelectorAll(".language-button").forEach(button => {
        const active = button.dataset.lang === current;

        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
    });
}

// The big language buttons drive the (hidden) select element.
document.querySelectorAll(".language-button").forEach(button => {
    button.addEventListener("click", () => {
        const select = document.getElementById("language");

        if (select.value !== button.dataset.lang) {
            select.value = button.dataset.lang;
            select.dispatchEvent(new Event("change"));
        }
    });
});

document.getElementById("language").addEventListener("change", event => {
    setLanguage(event.target.value);
    refreshDynamicText();
    syncLanguageButtons();

    try {
        localStorage.setItem("krishiai_language", event.target.value);
    } catch (error) {
        // Remembering the language is optional.
    }

    document.getElementById("crop").value = "";
    document.getElementById("problem").value = "";

    document.getElementById("image").value = "";
    document.getElementById("cameraInput").value = "";

    showImagePreview(null);

    const result = document.getElementById("result");
    result.innerHTML = "";
    result.classList.add("hidden");
});



// Set the initial UI language.
try {
    const savedLanguage = localStorage.getItem("krishiai_language");

    if (savedLanguage && translations[savedLanguage]) {
        document.getElementById("language").value = savedLanguage;
    }
} catch (error) {
    // Ignore: fall back to the default language.
}

setLanguage(getLanguage());
syncLanguageButtons();
restoreLoginSession();
restoreWeather();


async function analyzeCrop() {
    stopReadAloud();
    stopVoiceInput();

    const crop = document.getElementById("crop").value.trim();
    const problem = document.getElementById("problem").value.trim();
    const language = getLanguage();
    const imageInput = document.getElementById("image");
    const result = document.getElementById("result");

    if (!crop && !problem && !selectedImage) {
        result.classList.remove("hidden");

        result.innerHTML = `
            <h3>${escapeHtml(t("missingTitle"))}</h3>
            <p>${escapeHtml(t("missingMessage"))}</p>
        `;

        return;
    }

    await imageReady; // photo compression must be finished

    let cleanCrop = crop;
    if (cleanCrop.includes(" / ")) {
        cleanCrop = cleanCrop.split(" / ")[0].trim();
    } else if (cleanCrop.includes("/")) {
        cleanCrop = cleanCrop.split("/")[0].trim();
    }

    const formData = new FormData();
    formData.append("crop", cleanCrop);
    formData.append("problem", problem);
    formData.append("language", language);

    if (lastWeather && lastWeather.current) {
        const placeStr = selectedPlace ? `${placeLabel(selectedPlace)}: ` : "";
        let weatherSummary = `${placeStr}Temp ${Math.round(lastWeather.current.temperature)}°C, Humidity ${Math.round(lastWeather.current.humidity)}%, Rain ${lastWeather.current.rain_mm}mm, Wind ${Math.round(lastWeather.current.wind_kmh)}km/h, Condition: ${lastWeather.current.weather_group || ""}`;
        if (weatherSummary.length > 800) {
            weatherSummary = weatherSummary.substring(0, 800);
        }
        formData.append("weather_context", weatherSummary);
    }

    if (selectedImage) {
        formData.append("image", selectedImage);
    }

    result.classList.remove("hidden");

    result.innerHTML = `
        <h3>${escapeHtml(t("loadingTitle"))}</h3>
        <p>${escapeHtml(t("loadingMessage"))}</p>
    `;

    try {
        const headers = {};
        if (authToken) {
            headers["Authorization"] = `Bearer ${authToken}`;
        }

        const response = await fetch(`${API_URL}/analyze`, {
            method: "POST",
            headers,
            body: formData
        });

        if (response.status === 401) {
            handleSessionExpired();
            return;
        }

        if (response.status === 413) {
            result.innerHTML = `
                <h3>${escapeHtml(t("errorTitle"))}</h3>
                <p>${escapeHtml(t("errImageTooLarge"))}</p>
            `;
            return;
        }

        if (response.status === 429) {
            const errData = await response.json().catch(() => ({}));
            result.innerHTML = `
                <h3>${escapeHtml(t("errorTitle"))}</h3>
                <p>⚠️ ${escapeHtml(getErrorMessage(errData, "Daily analysis quota reached. Please try again tomorrow."))}</p>
            `;
            return;
        }

        if (response.status === 503) {
            const errData = await response.json().catch(() => ({}));
            result.innerHTML = `
                <div class="service-busy-card">
                    <div class="busy-header">
                        <span class="busy-icon">🌾</span>
                        <div>
                            <h3>${escapeHtml(t("busyTitle"))}</h3>
                            <p>${escapeHtml(getErrorMessage(errData, t("busyMessage")))}</p>
                        </div>
                    </div>
                    <div class="busy-helpline-box">
                        <p><strong>${escapeHtml(t("busyHelplineNotice"))}</strong></p>
                        <a href="tel:18001801551" class="primary-button helpline-call-btn">
                            📞 ${escapeHtml(t("busyCallKcc"))}
                        </a>
                    </div>
                    <hr>
                    <small>⚠️ ${escapeHtml(t("disclaimer"))}</small>
                </div>
            `;
            return;
        }

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                getErrorMessage(data, data.error || "Analysis request failed")
            );
        }

        let imageMessage = "";

        if (data.image) {
            imageMessage = `
                <p>
                    📷 <strong>${escapeHtml(t("imageResult"))}:</strong>
                    ${escapeHtml(data.image.filename)}
                </p>
            `;
        }

        const cautionBannerHtml = `
            <div class="ai-caution-banner">
                <span class="caution-icon">⚠️</span>
                <div class="caution-content">
                    <strong>${escapeHtml(t("aiCautionTitle"))}</strong>
                    <p>${escapeHtml(t("aiCautionBody"))}</p>
                </div>
            </div>
        `;

        if (data.error) {
            result.innerHTML = `
                ${cautionBannerHtml}
                <h3>${escapeHtml(t("warningTitle"))}</h3>
                <p>
                    <strong>${escapeHtml(t("cropResult"))}:</strong>
                    ${escapeHtml(data.crop)}
                </p>
                ${imageMessage}
                <div class="warning">
                    ⚠️ ${escapeHtml(data.analysis)}
                </div>
                <hr>
                <small>${escapeHtml(data.disclaimer || t("disclaimer"))}</small>
            `;
            return;
        }

        window.currentAnalysisResult = data.analysis;
        window.currentAnalysisLang = data.language_code || getLanguage();

        let guestNoticeHtml = "";
        if (!authToken || data.is_guest) {
            guestNoticeHtml = `
                <div class="guest-notice-box">
                    ${escapeHtml(t("guestResultNotice"))}
                    <a href="#authSection" onclick="document.getElementById('authSection').scrollIntoView({behavior:'smooth'}); return false;">
                        ${escapeHtml(t("loginTitle"))} / ${escapeHtml(t("registerTitle"))}
                    </a>
                </div>
            `;
        }

        result.innerHTML = `
            ${cautionBannerHtml}
            <div class="result-top-bar">
                <h3>${escapeHtml(t("resultTitle"))}</h3>
                <div class="result-actions">
                    <button type="button" id="ttsButton" class="tts-button" onclick="toggleReadAloud(window.currentAnalysisResult, window.currentAnalysisLang)" title="${escapeHtml(t("ttsPlay"))}">
                        <span class="tts-icon">🔊</span>
                        <span id="ttsButtonText">${escapeHtml(t("ttsPlay"))}</span>
                    </button>
                    <button type="button" id="whatsappShareButton" class="whatsapp-share-button" onclick="shareOnWhatsApp()" title="${escapeHtml(t("shareWhatsApp"))}">
                        <span class="share-icon">📲</span>
                        <span>${escapeHtml(t("shareWhatsApp"))}</span>
                    </button>
                </div>
            </div>
            <p>
                <strong>${escapeHtml(t("cropResult"))}:</strong>
                ${escapeHtml(data.crop)}
            </p>
            ${imageMessage}
            <h4>${escapeHtml(t("guidanceTitle"))}</h4>
            <div class="analysis">
                ${formatAnalysis(data.analysis)}
            </div>
            ${guestNoticeHtml}
            <hr>
            <small>⚠️ ${escapeHtml(data.disclaimer || t("disclaimer"))}</small>
        `;

        if (authToken && data.saved_to_history) {
            loadAnalysisHistory().catch(() => {});
        }

    } catch (error) {
        console.error("KrishiAI Analysis Error:", error);

        result.innerHTML = `
            <div class="service-busy-card">
                <div class="busy-header">
                    <span class="busy-icon">🌾</span>
                    <div>
                        <h3>${escapeHtml(t("busyTitle"))}</h3>
                        <p>${escapeHtml(t("busyMessage"))}</p>
                    </div>
                </div>
                <div class="busy-helpline-box">
                    <p><strong>${escapeHtml(t("busyHelplineNotice"))}</strong></p>
                    <a href="tel:18001801551" class="primary-button helpline-call-btn">
                        📞 ${escapeHtml(t("busyCallKcc"))}
                    </a>
                </div>
                <hr>
                <small>⚠️ ${escapeHtml(t("disclaimer"))}</small>
            </div>
        `;
    }
}


/*
 * Format Gemini's Markdown-style response.
 */
function formatAnalysis(text) {
    if (!text) {
        return `<p>${escapeHtml(t("noAnalysis"))}</p>`;
    }

    let html = escapeHtml(text);

    // Remove backslashes placed before Markdown markers.
    html = html.replace(/\\(?=[#*_-])/g, "");

    // Headings.
    html = html.replace(/^### (.*)$/gm, "<h4>$1</h4>");
    html = html.replace(/^## (.*)$/gm, "<h3>$1</h3>");
    html = html.replace(/^# (.*)$/gm, "<h2>$1</h2>");

    // Bold and italic.
    html = html.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");

    // Horizontal rules.
    html = html.replace(/^\s*---+\s*$/gm, "<hr>");

    // Simple bullet lists.
    html = html.replace(
        /(?:^|\n)(?:\*|-)\s+(.*?)(?=\n|$)/g,
        "<li>$1</li>"
    );

    html = html.replace(
        /((?:<li>.*?<\/li>\s*)+)/g,
        "<ul>$1</ul>"
    );

    // Numbered list items.
    html = html.replace(
        /(?:^|\n)\s*\d+\.\s+(.*?)(?=\n|$)/g,
        "<p>$1</p>"
    );

    // New lines.
    html = html.replace(/\n/g, "<br>");

    // Clean breaks around block elements.
    html = html.replace(/<br>\s*(<h[234]>|<ul>|<hr>)/g, "$1");
    html = html.replace(/(<\/h[234]>|<\/ul>|<hr>)\s*<br>/g, "$1");

    return html;
}


/*
 * Escape untrusted text before inserting it into HTML.
 */
function escapeHtml(text) {
    if (text === null || text === undefined) {
        return "";
    }

    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}



async function loadAnalysisHistory() {
    const historyStatus = document.getElementById("historyStatus");
    const historyList = document.getElementById("historyList");
    const refreshButton = document.getElementById("refreshHistory");

        if (!authToken) {
        historyStatus.textContent = t("historyLogin");
        historyList.innerHTML = "";
        window.krishiAIHistoryRecords = [];
        refreshButton.disabled = false;
        return;
    }

    historyStatus.textContent = t("historyLoading");
    refreshButton.disabled = true;

    try {
        const response = await fetch(`${API_URL}/history?limit=20`, {
        headers: {
        "Authorization": `Bearer ${authToken}`
    }
});

        if (response.status === 401) {
            handleSessionExpired();
            return;
        }

        if (!response.ok) {
            throw new Error(`Request failed: ${response.status}`);
        }

        const data = await response.json();

	const records = Array.isArray(data)
    	    ? data
    	    : data.history || data.analyses || data.records || [];

	window.krishiAIHistoryRecords = records;

	if (!records.length) {
    	    historyStatus.textContent = t("historyNone");
    	    historyList.innerHTML = "";
    	    return;
	}

        renderAnalysisHistory(records);

    } catch (error) {
        console.error("Could not load analysis history:", error);
        historyStatus.textContent = t("historyError");
    } finally {
        refreshButton.disabled = false;
    }
}

document.getElementById("refreshHistory").addEventListener(
    "click",
    loadAnalysisHistory
);

loadAnalysisHistory();



function renderAnalysisHistory(records) {
    const historyStatus = document.getElementById("historyStatus");
    const historyList = document.getElementById("historyList");
    const selectedLanguage = document.getElementById("historyLanguageFilter").value;

    const filteredRecords = selectedLanguage === "all"
        ? records
        : records.filter(record =>
            (record.language || "en").toLowerCase() === selectedLanguage
        );

    if (!filteredRecords.length) {
        historyStatus.textContent = t("historyNoneLanguage");
        historyList.innerHTML = "";
        return;
    }

    historyStatus.textContent = t("historyShowing")
        .replace("{shown}", filteredRecords.length)
        .replace("{total}", records.length);

    window.krishiAIFilteredRecords = filteredRecords;

    historyList.innerHTML = filteredRecords.map((record, index) => {
        const crop = escapeHtml(record.crop || t("notSpecified"));
        const problem = escapeHtml(record.problem || t("notSpecified"));

        const languageNames = {
            en: "English",
            mr: "मराठी (Marathi)",
            hi: "हिन्दी (Hindi)"
        };

        const languageCode = (record.language || "en").toLowerCase();
        const language = escapeHtml(
            languageNames[languageCode] || languageCode
        );

        const date = record.created_at
            ? new Date(record.created_at).toLocaleString()
            : t("dateUnavailable");

        const analysis = record.analysis || "";

        return `
            <article class="history-card">
                <h3>${crop}</h3>
                <p><strong>${escapeHtml(t("historyProblem"))}</strong> ${problem}</p>
                <p><strong>${escapeHtml(t("historyLanguage"))}</strong> ${language}</p>
                <p><strong>${escapeHtml(t("historyDate"))}</strong> ${escapeHtml(date)}</p>
                <details>
                    <summary>${escapeHtml(t("historyView"))}</summary>
                    <div class="history-actions-bar">
                        <button type="button" class="history-tts-button" onclick="playHistoryAudio(${index}, event)" title="${escapeHtml(t("ttsPlay"))}">
                            🔊 ${escapeHtml(t("ttsPlay"))}
                        </button>
                        <button type="button" class="history-action-button" onclick="shareHistoryWhatsApp(${index})" title="${escapeHtml(t("shareWhatsApp"))}">
                            📲 ${escapeHtml(t("shareWhatsApp"))}
                        </button>
                    </div>
                    <div class="history-analysis">${formatAnalysis(analysis)}</div>
                </details>
            </article>
        `;
    }).join("");
}

document.getElementById("historyLanguageFilter").addEventListener(
    "change",
    () => renderAnalysisHistory(window.krishiAIHistoryRecords || [])
);

async function confirmClearHistory() {
    if (!authToken) return;
    if (!confirm(t("confirmClearHistory"))) return;

    try {
        const response = await fetch(`${API_URL}/history`, {
            method: "DELETE",
            headers: { Authorization: `Bearer ${authToken}` }
        });
        if (response.ok) {
            window.krishiAIHistoryRecords = [];
            renderAnalysisHistory([]);
            setAuthStatus(t("historyClearedSuccess"));
            alert(t("historyClearedSuccess"));
        } else {
            const data = await response.json().catch(() => ({}));
            alert(getErrorMessage(data, "Could not clear history."));
        }
    } catch (err) {
        console.error("Failed to clear history:", err);
        alert(t("connectionError"));
    }
}

async function confirmDeleteAccount() {
    if (!authToken) return;
    if (!confirm(t("confirmDeleteAccount"))) return;

    try {
        const response = await fetch(`${API_URL}/auth/me`, {
            method: "DELETE",
            headers: { Authorization: `Bearer ${authToken}` }
        });
        if (response.ok) {
            clearSession(t("accountDeletedSuccess"));
            alert(t("accountDeletedSuccess"));
        } else {
            const data = await response.json().catch(() => ({}));
            alert(getErrorMessage(data, "Could not delete account."));
        }
    } catch (err) {
        console.error("Failed to delete account:", err);
        alert(t("connectionError"));
    }
}

// ---- Fertilizer & Dosage Calculator -------------------------------------
let lastFertilizerData = null;

async function calculateFertilizer() {
    const crop = document.getElementById("fertCrop").value.trim();
    const areaInput = document.getElementById("fertArea").value;
    const unit = document.getElementById("fertUnit").value;
    const statusEl = document.getElementById("fertStatus");
    const button = document.getElementById("fertCalculateButton");
    const buttonText = document.getElementById("fertButtonText");

    if (!crop) {
        statusEl.textContent = t("fertCropPlaceholder") || "Please select or type a crop name.";
        return;
    }

    const area = parseFloat(areaInput);
    if (!area || isNaN(area) || area <= 0) {
        statusEl.textContent = t("fertError");
        return;
    }

    statusEl.textContent = "";
    button.disabled = true;
    if (buttonText) buttonText.textContent = t("fertCalculating");

    try {
        const response = await fetch(`${API_URL}/fertilizer/calculate`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ crop, area, unit })
        });

        const data = await response.json();

        if (!response.ok) {
            statusEl.textContent = data.detail || t("fertError");
            return;
        }

        lastFertilizerData = data;
        renderFertilizerResult(data);

        const resultEl = document.getElementById("fertResult");
        resultEl.classList.remove("hidden");
        resultEl.style.display = "block";
        resultEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
        statusEl.textContent = t("errConnection");
    } finally {
        button.disabled = false;
        if (buttonText) buttonText.textContent = t("fertCalculateBtn");
    }
}

function renderFertilizerResult(data) {
    if (!data) return;

    const lang = getLanguage();
    const cropName = data.crop_name[lang] || data.crop_name.en || data.crop_key;
    const unitMap = {
        acre: t("unitAcre"),
        guntha: t("unitGuntha"),
        hectare: t("unitHectare")
    };
    const unitName = unitMap[data.input_unit] || data.input_unit;

    // Header Badge
    document.getElementById("fertResultIcon").textContent = data.icon || "🌾";
    document.getElementById("fertResultTitle").textContent = `${cropName}`;
    document.getElementById("fertResultArea").textContent =
        `${data.input_area} ${unitName} (${data.acres} ${t("unitAcre")})`;

    // Commercial Bags
    const urea = data.commercial_bags.urea;
    const dap = data.commercial_bags.dap;
    const mop = data.commercial_bags.mop;

    document.getElementById("fertUreaBags").textContent = urea.bags;
    document.getElementById("fertUreaKg").textContent = `${urea.weight_kg} kg total (${urea.grade})`;

    document.getElementById("fertDapBags").textContent = dap.bags;
    document.getElementById("fertDapKg").textContent = `${dap.weight_kg} kg total (${dap.grade})`;

    document.getElementById("fertMopBags").textContent = mop.bags;
    document.getElementById("fertMopKg").textContent = `${mop.weight_kg} kg total (${mop.grade})`;

    // Pure Nutrients
    const npk = data.pure_nutrients_kg;
    document.getElementById("fertNpkText").textContent =
        `N: ${npk.nitrogen_n} kg | P₂O₅: ${npk.phosphorus_p} kg | K₂O: ${npk.potassium_k} kg`;

    const special = data.special_nutrient[lang] || data.special_nutrient.en || "";
    document.getElementById("fertSpecialNutrient").textContent = special || t("notSpecified");

    // Split Schedule
    const scheduleContainer = document.getElementById("fertScheduleList");
    scheduleContainer.innerHTML = (data.stages || []).map((stage, i) => {
        const text = stage[lang] || stage.en || "";
        return `
            <div class="fert-stage-item">
                <span class="fert-stage-num">${i + 1}</span>
                <p class="fert-stage-desc">${escapeHtml(text)}</p>
            </div>
        `;
    }).join("");

    // Organic & FYM
    const fymText = t("fymTonnesText").replace("{tonnes}", data.organic_fym_tonnes);
    document.getElementById("fertFymText").textContent = `🚜 ${fymText}`;

    const organicAdvice = data.organic_tips[lang] || data.organic_tips.en || "";
    document.getElementById("fertOrganicTips").textContent = organicAdvice;
}

function readFertilizerAloud() {
    if (!lastFertilizerData) return;
    const data = lastFertilizerData;
    const lang = getLanguage();
    const cropName = data.crop_name[lang] || data.crop_name.en || data.crop_key;
    const urea = data.commercial_bags.urea;
    const dap = data.commercial_bags.dap;
    const mop = data.commercial_bags.mop;
    const fym = data.organic_fym_tonnes;

    let speechText = "";
    if (lang === "mr") {
        speechText = `${cropName} पिकासाठी ${data.input_area} एकरासाठी खताची मात्रा: ` +
            `युरिया ${urea.bags} गोण्या, एकूण ${urea.weight_kg} किलो. ` +
            `डीएपी ${dap.bags} गोण्या, एकूण ${dap.weight_kg} किलो. ` +
            `पोटॅश ${mop.bags} गोण्या, एकूण ${mop.weight_kg} किलो. ` +
            `तसेच शेणखत ${fym} टन वापरण्याची शिफारस आहे. ` +
            (data.organic_tips.mr || "");
    } else if (lang === "hi") {
        speechText = `${cropName} फसल के लिए ${data.input_area} एकड़ हेतु खाद की खुराक: ` +
            `यूरिया ${urea.bags} बोरे, कुल ${urea.weight_kg} किलोग्राम। ` +
            `डीएपी ${dap.bags} बोरे, कुल ${dap.weight_kg} किलोग्राम। ` +
            `पोटाश ${mop.bags} बोरे, कुल ${mop.weight_kg} किलोग्राम। ` +
            `साथ ही गोबर की खाद ${fym} टन डालने की सलाह है। ` +
            (data.organic_tips.hi || "");
    } else {
        speechText = `Fertilizer dosage for ${cropName}, ${data.input_area} ${data.input_unit}: ` +
            `Urea: ${urea.bags} bags, total ${urea.weight_kg} kilograms. ` +
            `DAP: ${dap.bags} bags, total ${dap.weight_kg} kilograms. ` +
            `Potash: ${mop.bags} bags, total ${mop.weight_kg} kilograms. ` +
            `Recommended Farm Yard Manure: ${fym} tonnes. ` +
            (data.organic_tips.en || "");
    }

    const btn = document.getElementById("fertAudioBtn");
    toggleReadAloud(speechText, lang, btn);
}

function shareFertilizerWhatsApp() {
    if (!lastFertilizerData) return;
    const data = lastFertilizerData;
    const lang = getLanguage();
    const cropName = data.crop_name[lang] || data.crop_name.en || data.crop_key;
    const urea = data.commercial_bags.urea;
    const dap = data.commercial_bags.dap;
    const mop = data.commercial_bags.mop;
    const special = data.special_nutrient[lang] || data.special_nutrient.en || "";
    const fym = data.organic_fym_tonnes;

    let stagesText = (data.stages || []).map((s, idx) => `${idx + 1}. ${s[lang] || s.en}`).join("\n");

    const message =
        `🧪 *KrishiAI Fertilizer Calculator Recommendation*\n\n` +
        `🌾 *${t("cropResult")}:* ${cropName} (${data.input_area} ${data.input_unit} / ${data.acres} Acres)\n` +
        `-----------------------------------------\n` +
        `🛍️ *${t("fertBagsTitle")}*\n` +
        `• *Urea (45kg):* ${urea.bags} Bags (${urea.weight_kg} kg)\n` +
        `• *DAP (50kg):* ${dap.bags} Bags (${dap.weight_kg} kg)\n` +
        `• *MOP Potash (50kg):* ${mop.bags} Bags (${mop.weight_kg} kg)\n` +
        `• *FYM / Compost:* ${fym} Tonnes\n` +
        (special ? `• *Special Nutrient:* ${special}\n\n` : `\n`) +
        `📅 *${t("fertScheduleTitle")}*\n${stagesText}\n\n` +
        `🌱 *Organic Tip:* ${data.organic_tips[lang] || data.organic_tips.en}\n\n` +
        `📱 _Calculated via KrishiAI — AI Farming Assistant_`;

    const url = `https://api.whatsapp.com/send?text=${encodeURIComponent(message)}`;
    window.open(url, "_blank", "noopener,noreferrer");
}

// Register PWA service worker for offline support
if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        navigator.serviceWorker.register("./sw.js").catch(err => {
            console.debug("PWA Service Worker registration skipped:", err);
        });
    });
}