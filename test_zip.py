import streamlit as st

st.set_page_config(page_title="ZIP TEST")
st.title("ZIP TEST - toto je nový súbor")
st.warning("Ak vidíš tento text, beží správny nový Streamlit súbor.")

uploaded_zip = st.file_uploader(
    "Vyber iba ZIP súbor",
    type=["zip"]
)

if uploaded_zip is not None:
    st.success(f"Nahraný ZIP: {uploaded_zip.name}")
