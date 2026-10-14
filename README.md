# 🌱 KrishiAI – AI-Powered Agricultural Assistant

Hi everyone! 👋

I'm building **KrishiAI** as part of the **TrainWithShubham DevOps Hackathon**. The idea behind this project is to use artificial intelligence and modern DevOps practices to empower Indian farmers with simple, accessible crop health guidance, weather insights, and agricultural advisory in their local language.

As a developer and DevOps learner, I wanted to build something that solves a genuine real-world problem while giving me hands-on practice with containerization, API design, reverse proxies, and database integration.

---

## 🚜 Why I Built KrishiAI

In rural India, farmers frequently face unexpected crop diseases, pest attacks, and sudden weather shifts. Getting timely expert advice can be difficult due to distance, language barriers, or limited agricultural extension services.

Many farmers also find typing long problem descriptions on mobile keyboards challenging or have spotty internet in the fields.

I designed KrishiAI with **simplicity and accessibility at the center**:
1. A farmer can simply **speak** their problem in **मराठी (Marathi)**, **हिन्दी (Hindi)**, or **English**, or upload a crop photo.
2. KrishiAI identifies the disease, suggests organic and chemical remedies, and explains preventive measures.
3. The farmer can **listen** to the advice read aloud, check their local weather risks, and share the guidance with fellow farmers on **WhatsApp with one click**.

---

## ✨ Features

### 🌿 Core Agricultural & AI Capabilities
* **📷 Photo & Text Crop Analysis:** Upload a photo from the gallery or camera, or describe symptoms to get instant AI-powered diagnoses via **Google Gemini API**.
* **🎙️ Voice Input (Speech-to-Text):** Farmers don't need to type — they can tap the microphone button and speak in **Marathi, Hindi, or English**.
* **🔊 Audio Read-Aloud (Text-to-Speech):** A "Listen to Advice" button speaks the complete diagnosis and remedies in clear, natural regional language.
* **🌐 True Trilingual Support:** The entire UI and AI guidance seamlessly switch between **English**, **मराठी (Marathi)**, and **हिन्दी (Hindi)**.
* **🌦️ Live Farm Weather & Advisories:** Search any Indian village or town to get live temperature, humidity, rainfall forecasts, and automated farming tips (spraying windows, fungal disease alerts).
* **📲 1-Click WhatsApp Share:** Instantly share formatted advice with neighbors or village farmer WhatsApp groups.
* **🧪 Smart Fertilizer & Dosage Calculator:** Calculates exact commercial fertilizer bags to purchase (Neem-coated Urea 45kg, DAP 50kg, MOP 50kg) and Farm Yard Manure (FYM) based on ICAR guidelines. Supports Indian land units (**Acres**, **Gunthas**, **Hectares**) with split application schedules, audio playback, and 1-click WhatsApp sharing for local fertilizer dealers.
* **📞 Emergency Kisan Helpline Directory:** 1-tap call access to the Government of India's toll-free **Kisan Call Center (1800-180-1551)** and Crop Insurance Helpline (**14447**).

### ⚙️ User & Data Features
* **👤 User Authentication:** Secure signup and login powered by **Argon2** password hashing and **JWT** session tokens.
* **📚 Saved History:** Automatically saves personal crop analyses to PostgreSQL so farmers can revisit past treatments anytime.
* **📱 Progressive Web App (PWA):** Equipped with a Web App Manifest and Service Worker — farmers can install KrishiAI to their Android home screen and view saved records offline in the field.

---

## 🛠️ Tech Stack

| Layer | Technologies |
| :--- | :--- |
| **Frontend** | HTML5, CSS3, JavaScript (Vanilla ES6+), Web Speech API, Service Workers (PWA) |
| **Backend** | Python 3.12, FastAPI, Pydantic, Pwdlib (Argon2), PyJWT, Uvicorn |
| **AI Integration** | Google Gemini API (`gemini-3.8-flash`) |
| **External APIs** | Open-Meteo Weather & Geocoding APIs (Open-source, no key required) |
| **Database** | PostgreSQL 16 (Relational storage for users & crop analysis records) |
| **DevOps & Cloud** | Docker, Docker Compose, Nginx (Reverse Proxy & Static Server), Git & GitHub |

---

## 🏗️ System Architecture

```text
[ Farmer / Mobile Device ]
            │
            ▼ (HTTP / Port 8081)
┌───────────────────────────────────────────────┐
│              Nginx (Frontend)                 │
│  - Serves PWA Static Files & Audio UI         │
│  - Reverse Proxies /api/ requests             │
└───────────────────────┬───────────────────────┘
                        │
                        ▼ (Internal Docker Network)
┌───────────────────────────────────────────────┐
│             FastAPI (Backend)                 │
│  - Auth (Argon2 + JWT)                        │
│  - Gemini Prompt Engine & Image Preprocessing │
│  - Weather Aggregator (Open-Meteo)            │
└───────────────┬───────────────────────┬───────┘
                │                       │
                ▼                       ▼
      ┌──────────────────┐    ┌──────────────────┐
      │  Google Gemini   │    │  PostgreSQL 16   │
      │  Vision / Text   │    │  User Accounts & │
      │  AI Analysis     │    │  Crop History    │
      └──────────────────┘    └──────────────────┘
```

---

## 🚀 How to Run the Project Locally

### Prerequisites
* [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
* A [Google Gemini API Key](https://aistudio.google.com/)

---

### Method 1: Running with Docker Compose (Recommended)

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/KrishiAI.git
   cd KrishiAI
   ```

2. **Configure Environment Variables:**
   Copy the example environment file inside `backend/`:
   ```bash
   cp backend/.env.example backend/.env
   ```
   Open `backend/.env` and add your Gemini API key:
   ```env
   AI_API_KEY=your_gemini_api_key_here
   JWT_SECRET_KEY=your_secure_random_jwt_secret_key
   ```
   *(Note: `DATABASE_URL` is automatically configured by Docker Compose).*

3. **Start the application:**
   ```bash
   docker compose up --build
   ```

4. **Access the application:**
   * 🌐 **Frontend (Web App):** [http://localhost:8081](http://localhost:8081)
   * 📡 **Backend API Docs (Swagger):** [http://localhost:8000/docs](http://localhost:8000/docs)
   * 🗄️ **PostgreSQL Database:** `localhost:5432`

---

### Method 2: Running Locally (Without Docker)

<details>
<summary>Click here for manual local setup instructions</summary>

#### 1. Setup PostgreSQL
Ensure PostgreSQL is running locally and create a database named `krishiai`:
```sql
CREATE DATABASE krishiai;
CREATE USER krishiai WITH PASSWORD 'krishiai_dev_password';
GRANT ALL PRIVILEGES ON DATABASE krishiai TO krishiai;
```

#### 2. Setup Backend
```bash
cd backend
python -m venv venv
# Activate virtual environment:
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

pip install -r requirements.txt
```

Create `backend/.env`:
```env
AI_API_KEY=your_gemini_api_key_here
DATABASE_URL=postgresql://krishiai:krishiai_dev_password@localhost:5432/krishiai
JWT_SECRET_KEY=your_secure_random_jwt_secret_key
```

Run the FastAPI server:
```bash
uvicorn main:app --reload --port 8000
```

#### 3. Setup Frontend
Serve the `frontend/` folder with any local web server (e.g. VS Code Live Server, or Python HTTP server):
```bash
cd frontend
python -m http.server 8081
```
</details>

---

## ☁️ DevOps Journey & What I'm Learning

Building KrishiAI has given me practical experience with real-world DevOps practices:

* **Containerization:** Writing clean `Dockerfile`s and configuring `.dockerignore` to optimize container sizes and security.
* **Multi-Container Orchestration:** Using `compose.yaml` to connect a web server, application backend, and persistent database with health checks.
* **Reverse Proxying:** Configuring Nginx to route API traffic seamlessly and handle large crop photo uploads (`client_max_body_size`).
* **Microservice Security:** Protecting database credentials, managing environment variables, and implementing rate limiting and JWT auth.
* **CI/CD & Cloud Roadmap:** Setting up GitHub Actions workflows and planning deployment to AWS (Elastic Beanstalk / ECS with CloudWatch monitoring).

---

## 🗺️ Roadmap & Next Steps

- [x] Multi-language crop disease analysis (Gemini AI)
- [x] Regional Voice Input & Text-to-Speech playback
- [x] Real-time village weather alerts & spray advisories
- [x] PostgreSQL database & user analysis history
- [x] 1-Click WhatsApp advice sharing & Kisan Helplines
- [x] Docker & Nginx multi-container setup
- [x] Progressive Web App (PWA) with offline shell
- [ ] Automated CI/CD pipeline using GitHub Actions
- [ ] Automated test suite with `pytest`
- [ ] Cloud deployment to AWS Elastic Beanstalk / ECS
- [ ] Application monitoring with AWS CloudWatch / Prometheus

---

## 🙌 Acknowledgements

* A big thank you to **TrainWithShubham** and the hackathon community for this incredible learning platform.
* Weather data powered by [Open-Meteo](https://open-meteo.com/).
* AI capabilities powered by [Google Gemini](https://ai.google.dev/).

---

*If you have suggestions, feedback, or ideas to make KrishiAI better for farmers, feel free to open an issue or connect!* 🌱