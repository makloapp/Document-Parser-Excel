import streamlit as st
import tempfile
import subprocess
import json
import sys
from pathlib import Path

st.set_page_config(page_title="OCR dokladov", layout="centered")

st.title("OCR spracovanie dokladov")
st.write("Nahraj JPG, PNG, TIFF, WEBP alebo PDF súbor. Aplikácia z neho vytvorí Excel.")

uploaded_file = st.file_uploader(
    "Vyber súbor",
    type=["jpg", "jpeg", "png", "pdf", "tif", "tiff", "webp"]
)

if uploaded_file is not None:
    st.info(f"Nahraný súbor: {uploaded_file.name}")

    if st.button("Spracovať doklad"):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            input_path = tmpdir / uploaded_file.name
            output_path = tmpdir / "vystup.xlsx"

            input_path.write_bytes(uploaded_file.getbuffer())

            with st.spinner("Spracúvam OCR..."):
                result = subprocess.run(
                    [sys.executable, "scripts/ocr_process.py", str(input_path), str(output_path)],
                    capture_output=True,
                    text=True
                )

            if result.returncode != 0:
                st.error("Spracovanie zlyhalo.")
                st.subheader("Chyba")
                st.code(result.stderr)
            else:
                st.success("Doklad bol spracovaný.")

                if result.stdout.strip():
                    try:
                        data = json.loads(result.stdout)
                        st.subheader("Extrahované údaje")
                        st.json(data)
                    except Exception:
                        st.subheader("Výstup programu")
                        st.code(result.stdout)

                if output_path.exists():
                    st.download_button(
                        label="Stiahnuť Excel",
                        data=output_path.read_bytes(),
                        file_name="vystup.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                else:
                    st.warning("Excel súbor sa nevytvoril.")