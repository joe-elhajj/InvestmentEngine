#!/bin/bash
cd "/Users/joeelhajj/Projects/Investment Engine"
source .venv/bin/activate
exec uvicorn app.main:app --host 127.0.0.1 --port 8000
