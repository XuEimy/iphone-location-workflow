#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
echo 'Installed. Follow README.md to pair your iPhone, mount the developer image, and choose coordinates.'
