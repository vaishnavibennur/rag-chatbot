import hmac
import hashlib
import json
import os
import re
import secrets

import streamlit as st


USERS_PATH = os.getenv("USERS_PATH", os.path.join("data", "users.json"))
PASSWORD_ITERATIONS = 260000


def get_auth_config():
    try:
        secrets_username = st.secrets.get("APP_USERNAME", None)
        secrets_password = st.secrets.get("APP_PASSWORD", None)
    except Exception:
        secrets_username = None
        secrets_password = None

    username = os.getenv("APP_USERNAME") or secrets_username or "admin"
    password = os.getenv("APP_PASSWORD") or secrets_password or "admin"

    return username, password


def load_users():
    if not os.path.exists(USERS_PATH):
        return {}

    try:
        with open(USERS_PATH, "r", encoding="utf-8") as users_file:
            return json.load(users_file)
    except (json.JSONDecodeError, OSError):
        return {}


def save_users(users):
    os.makedirs(os.path.dirname(USERS_PATH), exist_ok=True)

    with open(USERS_PATH, "w", encoding="utf-8") as users_file:
        json.dump(users, users_file, indent=2)


def normalize_username(username):
    return username.strip().lower()


def validate_registration(username, password, confirm_password):
    username = normalize_username(username)

    if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,32}", username):
        return None, "Username must be 3-32 characters using letters, numbers, dot, dash, or underscore."

    if len(password) < 6:
        return None, "Password must be at least 6 characters."

    if password != confirm_password:
        return None, "Passwords do not match."

    return username, None


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)

    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()

    return {
        "salt": salt,
        "hash": password_hash,
        "iterations": PASSWORD_ITERATIONS,
    }


def verify_password(password, password_record):
    salt = password_record.get("salt", "")
    expected_hash = password_record.get("hash", "")
    iterations = int(password_record.get("iterations", PASSWORD_ITERATIONS))

    actual_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()

    return hmac.compare_digest(actual_hash, expected_hash)


def register_user(username, password, confirm_password):
    username, error = validate_registration(username, password, confirm_password)

    if error:
        return False, error

    users = load_users()

    if username in users:
        return False, "This username is already registered."

    users[username] = {
        "password": hash_password(password),
    }
    save_users(users)

    return True, "Account created. You can sign in now."


def authenticate(username, password):
    username = normalize_username(username)
    users = load_users()

    if username in users:
        return verify_password(password, users[username].get("password", {}))

    expected_username, expected_password = get_auth_config()
    expected_username = normalize_username(expected_username)

    return hmac.compare_digest(username, expected_username) and hmac.compare_digest(
        password,
        expected_password,
    )


def is_authenticated():
    return bool(st.session_state.get("authenticated"))


def render_login():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

        html, body, [class*="css"] {
            font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }

        .stApp {
            background: #f7f8fb;
        }

        .block-container {
            max-width: 480px;
            padding-top: 10vh;
        }

        .login-card {
            background: white;
            border: 1px solid #e2e8f0;
            border-radius: 16px;
            padding: 28px 28px 22px 28px;
            margin-bottom: 16px;
            box-shadow: 0 18px 45px rgba(15, 23, 42, 0.06);
        }

        .login-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: #eff6ff;
            color: #1d4ed8;
            border: 1px solid #bfdbfe;
            border-radius: 999px;
            padding: 6px 10px;
            font-size: 12px;
            font-weight: 600;
            margin-bottom: 18px;
        }

        .login-card h1 {
            font-size: 30px;
            line-height: 1.12;
            margin: 0 0 10px 0;
            color: #111827;
            letter-spacing: 0;
        }

        .login-card p {
            color: #64748b;
            margin: 0;
            font-size: 15px;
            line-height: 1.55;
        }

        .login-footnote {
            color: #94a3b8;
            font-size: 12px;
            text-align: center;
            margin-top: 18px;
        }

        .stTabs [data-baseweb="tab-list"] {
            gap: 6px;
            background: #eef2f7;
            border-radius: 12px;
            padding: 4px;
            margin-bottom: 14px;
        }

        .stTabs [data-baseweb="tab"] {
            border-radius: 9px;
            padding: 8px 14px;
            color: #475569;
            font-weight: 600;
        }

        .stTabs [aria-selected="true"] {
            background: white;
            color: #111827;
            box-shadow: 0 6px 16px rgba(15, 23, 42, 0.06);
        }

        .stTextInput input {
            border-radius: 10px;
            border: 1px solid #dbe3ef;
            padding: 10px 12px;
        }

        .stTextInput input:focus {
            border-color: #2563eb;
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
        }

        .stButton button,
        .stFormSubmitButton button {
            border-radius: 10px;
            border: 1px solid #2563eb;
            background: #2563eb;
            color: white;
            font-weight: 600;
            min-height: 42px;
        }

        .stButton button:hover,
        .stFormSubmitButton button:hover {
            border-color: #1d4ed8;
            background: #1d4ed8;
            color: white;
        }

        div[data-testid="stAlert"] {
            border-radius: 10px;
        }
        </style>
        <div class="login-card">
            <div class="login-badge">Document AI Workspace</div>
            <h1>Welcome back</h1>
            <p>Sign in or create an account to access your document knowledge assistant.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    sign_in_tab, register_tab = st.tabs(["Sign in", "Register"])

    with sign_in_tab:
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", width="stretch")

        if submitted:
            if authenticate(username, password):
                st.session_state.authenticated = True
                st.session_state.username = normalize_username(username)
                st.rerun()

            st.error("Invalid username or password")

    with register_tab:
        with st.form("register_form"):
            new_username = st.text_input("Choose username")
            new_password = st.text_input("Choose password", type="password")
            confirm_password = st.text_input("Confirm password", type="password")
            register_submitted = st.form_submit_button("Create account", width="stretch")

        if register_submitted:
            created, message = register_user(
                new_username,
                new_password,
                confirm_password,
            )

            if created:
                st.success(message)
            else:
                st.error(message)

    st.markdown(
        '<div class="login-footnote">Your account is stored locally on this machine.</div>',
        unsafe_allow_html=True,
    )


def require_login():
    if is_authenticated():
        return

    render_login()
    st.stop()


def logout_button():
    if st.sidebar.button("Sign out", width="stretch"):
        st.session_state.authenticated = False
        st.session_state.username = ""
        st.rerun()
