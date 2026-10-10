
const API_URL = "/api";

let selectedImage = null;
let previewUrl = null;

function showImagePreview(file) {
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

    selectedImage = file;
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
        cropPlaceholder: "Example: Tomato",
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
        connectionError: "Unable to connect to the KrishiAI analysis service.",
        serverError: "Please make sure the backend server is running.",
        noAnalysis: "No analysis was returned by the AI service."
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
        cropPlaceholder: "उदाहरण: टोमॅटो",
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
        connectionError: "KrishiAI विश्लेषण सेवेशी संपर्क साधता आला नाही.",
        serverError: "कृपया बॅकएंड सर्व्हर सुरू आहे का ते तपासा.",
        noAnalysis: "AI सेवेकडून कोणतेही विश्लेषण मिळाले नाही."
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
        cropPlaceholder: "उदाहरण: टमाटर",
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
        connectionError: "KrishiAI विश्लेषण सेवा से संपर्क नहीं हो पाया।",
        serverError: "कृपया जाँचें कि बैकएंड सर्वर चालू है या नहीं।",
        noAnalysis: "AI सेवा से कोई विश्लेषण प्राप्त नहीं हुआ।"
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
    document.getElementById("analyzer").scrollIntoView({
        behavior: "smooth",
        block: "start"
    });
}


// Change the complete static UI immediately.


document.getElementById("language").addEventListener("change", event => {
    setLanguage(event.target.value);

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
setLanguage(getLanguage());


async function analyzeCrop() {
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


    const formData = new FormData();
    formData.append("crop", crop);
    formData.append("problem", problem);
    formData.append("language", language);

    if (selectedImage) {
        formData.append("image", selectedImage);
    }

    result.classList.remove("hidden");
    result.innerHTML = `
        <h3>${escapeHtml(t("loadingTitle"))}</h3>
        <p>${escapeHtml(t("loadingMessage"))}</p>
    `;

    try {
        const response = await fetch(`${API_URL}/analyze`, {
            method: "POST",
            body: formData
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail || data.error || "Analysis request failed"
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

        if (data.error) {
            result.innerHTML = `
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

        result.innerHTML = `
            <h3>${escapeHtml(t("resultTitle"))}</h3>
            <p>
                <strong>${escapeHtml(t("cropResult"))}:</strong>
                ${escapeHtml(data.crop)}
            </p>
            ${imageMessage}
            <h4>${escapeHtml(t("guidanceTitle"))}</h4>
            <div class="analysis">
                ${formatAnalysis(data.analysis)}
            </div>
            <hr>
            <small>⚠️ ${escapeHtml(data.disclaimer || t("disclaimer"))}</small>
        `;

    } catch (error) {
        console.error("KrishiAI Analysis Error:", error);

        result.innerHTML = `
            <h3>${escapeHtml(t("errorTitle"))}</h3>
            <p>${escapeHtml(t("connectionError"))}</p>
            <small>${escapeHtml(t("serverError"))}</small>
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
