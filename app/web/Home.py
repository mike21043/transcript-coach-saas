import streamlit as st
import requests, os

API_URL = os.getenv("API_URL", "http://localhost:8000")

st.title("Transcript Coach SaaS")

st.header("Upload Audio")
file = st.file_uploader("Choose an audio file", type=["mp3", "wav", "m4a"])
if st.button("Upload", disabled=(file is None)):
    if file is None:
        st.warning("Please choose a file first.")
    else:
        with st.spinner("Uploading..."):
            resp = requests.post(f"{API_URL}/upload", files={"file": (file.name, file.getvalue())})
        if resp.ok:
            st.success(f"Uploaded. Job ID: {resp.json()['job_id']}")
        else:
            st.error(f"Upload failed: {resp.text}")

st.header("Transcripts")
try:
    resp = requests.get(f"{API_URL}/transcripts", timeout=10)
    resp.raise_for_status()
    transcripts = resp.json().get("transcripts", [])
except Exception as e:
    st.error(f"Could not fetch transcripts: {e}")
    transcripts = []

if transcripts:
    choice = st.selectbox("Pick a transcript to view", transcripts, index=0)
    if st.button("Open transcript"):
        t = requests.get(f"{API_URL}/transcripts/{choice}", timeout=15)
        if t.ok:
            st.text_area("Transcript", t.text, height=400)
        else:
            st.error("Could not fetch transcript content.")
else:
    st.info("No transcripts yet. Upload an audio file to start.")
