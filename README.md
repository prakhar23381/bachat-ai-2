# Bachat AI: Local Privacy-First ML Engine

Bachat AI is a continuous-learning, 100% on-device local financial classification engine built with Python, FastAPI, and Scikit-Learn.

## 🚀 Getting Started

Since this repository contains exactly what you need to build the backend (and no pre-written user interface), follow these steps to deploy the API stack:

### 1. Setup the Environment
```bash
# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the engine libraries
pip install -r requirements.txt
```

### 2. Environment Variables (Critical for Privacy)
In `api/server.py`, the backend relies on an AES-256 Fernet key to encrypt your personal data natively inside your `transactions.db` SQLite file. 

Feel free to modify the `KEY` parameter in `api/server.py` with your own highly secure 32-character base64 URL-safe string. Keep in mind: **If you lose this key, you lose your database data forever.**

### 3. Boot the Server
```bash
# Start the FastAPI engine
./start.sh
```

### 🧠 How It Works
* The API runs locally on `http://localhost:8000`.
* The `TransactionEngine` (`transaction_ml/system.py`) holds an internal `SGDClassifier`. It exposes the `.correct` and `.bootstrap` pipes so your local vectorizer continuously refines its cryptographic matrix every time you use the app.
* No data leaves your machine. 
* Have fun building a beautiful UI on top of this!
