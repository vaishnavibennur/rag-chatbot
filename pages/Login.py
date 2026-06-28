import streamlit as st
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from auth import is_authenticated, render_login


st.set_page_config(page_title="Login", page_icon="Login", layout="centered")

if is_authenticated():
    st.success("You are already signed in.")
    st.page_link("app.py", label="Open Knowledge Assistant")
else:
    render_login()
