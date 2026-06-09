#!/usr/bin/env bash
cd /home/runner/workspace

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
PIP_USER=false python -m pip install -r requirements.txt

exec python -m streamlit run app.py --server.port 8501 --server.address 0.0.0.0
